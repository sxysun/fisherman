import Foundation

final class StatusPoller: @unchecked Sendable {
    private let state: AppState
    private let controlPort: String
    private let config: ConfigManager
    private var timer: Timer?
    private let interval: TimeInterval = 3.0
    private let session: URLSession
    private var pollCycleCount: Int = 0

    init(state: AppState, controlPort: String, config: ConfigManager) {
        self.state = state
        self.controlPort = controlPort
        self.config = config

        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 2.0
        config.httpMaximumConnectionsPerHost = 4
        self.session = URLSession(configuration: config)
    }

    func start() {
        timer = Timer.scheduledTimer(withTimeInterval: interval, repeats: true) { [weak self] _ in
            self?.poll()
        }
        // Fire immediately
        poll()
    }

    func stop() {
        timer?.invalidate()
        timer = nil
    }

    private func poll() {
        pollFisherman()
        pollAllActivity()
        if pollCycleCount % 5 == 0 {
            pollRelayFriendActivity()
            pollPublishedFriendPreviews()
        }

        // Poll history every 20 cycles (~60s at 3s interval)
        pollCycleCount += 1
        if pollCycleCount % 20 == 0 {
            pollAllHistory()
        }
    }

    private func pollFisherman() {
        guard let url = URL(string: "http://127.0.0.1:\(controlPort)/status") else { return }
        var request = URLRequest(url: url)
        request.timeoutInterval = 2.0

        session.dataTask(with: request) { [weak self] data, response, error in
            var fishermanStatus: [String: Any]? = nil
            if let data, error == nil,
               let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            {
                fishermanStatus = json
            }
            DispatchQueue.main.async {
                self?.state.update(
                    fishermanStatus: fishermanStatus
                )
            }
        }.resume()
    }

    // MARK: - Multi-user activity polling

    /// Thread-safe collector for concurrent activity polling results.
    private final class ActivityCollector: @unchecked Sendable {
        private let lock = NSLock()
        private var items: [UserActivity] = []

        func append(_ item: UserActivity) {
            lock.lock()
            items.append(item)
            lock.unlock()
        }

        func sorted() -> [UserActivity] {
            lock.lock()
            let result = items.sorted { a, b in
                if a.id == "me" { return true }
                if b.id == "me" { return false }
                return a.name < b.name
            }
            lock.unlock()
            return result
        }
    }

    private func pollAllActivity() {
        let group = DispatchGroup()
        let collector = ActivityCollector()

        // Poll own server
        group.enter()
        pollOwnActivity(
            id: "me",
            name: "me"
        ) { activity in
            if let activity { collector.append(activity) }
            group.leave()
        }

        group.notify(queue: .main) { [weak self] in
            guard let self else { return }
            var activities = collector.sorted()

            // Preserve history from previous state
            activities = activities.map { user in
                var updated = user
                if let existing = self.state.allActivity.first(where: { $0.id == user.id }) {
                    updated.history = existing.history
                    updated.sessionStart = existing.sessionStart
                }
                return updated
            }

            self.commitActivities(activities, preservingRelay: true)
        }
    }

    private func pollRelayFriendActivity() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            let friends = self.config.relayFriends
            let result = CliBridge.run(["friend", "status", "--limit", "8"], timeout: 8)
            guard result.exitCode == 0,
                  let data = result.stdout.data(using: .utf8),
                  let rows = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]]
            else { return }

            let now = Date().timeIntervalSince1970
            var eventsByPubkey: [String: [[String: Any]]] = [:]
            for row in rows {
                guard let pubkey = row["pubkey"] as? String else { continue }
                eventsByPubkey[pubkey, default: []].append(row)
            }

            var activeByPubkey: [String: UserActivity] = [:]
            for (pubkey, events) in eventsByPubkey {
                let sorted = events.sorted {
                    ($0["ts"] as? Double ?? 0) > ($1["ts"] as? Double ?? 0)
                }
                guard let newest = sorted.first,
                      let friend = newest["friend"] as? String,
                      let ts = newest["ts"] as? Double,
                      let digest = newest["digest"] as? [String: Any]
                else { continue }

                let category = digest["category"] as? String ?? "idle"
                let emoji = Self.displayEmoji(digest["emoji"] as? String, category: category)
                let status = digest["status"] as? String ?? ""
                let stale = now - ts > 15 * 60
                let eventHistory: [ActivityEntry] = sorted.compactMap { event in
                    guard let eventTs = event["ts"] as? Double,
                          let eventDigest = event["digest"] as? [String: Any]
                    else { return nil }
                    let eventCategory = eventDigest["category"] as? String ?? "idle"
                    return ActivityEntry(
                        emoji: Self.displayEmoji(eventDigest["emoji"] as? String, category: eventCategory),
                        category: eventCategory,
                        status: eventDigest["status"] as? String ?? "",
                        timestamp: Date(timeIntervalSince1970: eventTs)
                    )
                }
                let embeddedHistory = Self.embeddedActivityHistory(from: digest)
                let history = embeddedHistory.isEmpty ? eventHistory : embeddedHistory
                var activity = UserActivity(
                    id: "relay:\(pubkey)",
                    name: friend,
                    emoji: emoji,
                    category: category,
                    status: status,
                    stale: stale,
                    history: history,
                    sessionStart: Self.computeSessionStart(
                        currentCategory: category,
                        history: history
                    )
                )
                activity.inFlow = digest["flow"] as? Bool ?? false
                activeByPubkey[pubkey] = activity
            }

            let activities: [UserActivity]
            if friends.isEmpty {
                activities = Array(activeByPubkey.values)
            } else {
                activities = friends.map { friend in
                    if let active = activeByPubkey[friend.pubkeyHex] {
                        return active
                    }
                    return UserActivity(
                        id: "relay:\(friend.pubkeyHex)",
                        name: friend.name,
                        emoji: "…",
                        category: "waiting",
                        status: "no recent status",
                        stale: true
                    )
                }
            }

            DispatchQueue.main.async { [weak self] in
                self?.commitActivities(activities, preservingRelay: false)
            }
        }
    }

    private func pollPublishedFriendPreviews() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let result = CliBridge.run(["friend", "preview", "--json"], timeout: 4)
            guard result.exitCode == 0,
                  let data = result.stdout.data(using: .utf8),
                  let rows = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]]
            else { return }

            let previews: [PublishedFriendStatus] = rows.compactMap { row in
                let friend = row["friend"] as? String ?? "friend"
                let pubkey = row["pubkey"] as? String ?? friend
                let audience = row["audience"] as? String ?? "friends"
                let published = row["published"] as? Bool ?? false
                let digest = row["digest"] as? [String: Any] ?? [:]
                let ts = row["ts"] as? Double
                return PublishedFriendStatus(
                    id: pubkey,
                    friend: friend,
                    pubkey: pubkey,
                    audience: audience,
                    emoji: Self.displayEmoji(
                        digest["emoji"] as? String,
                        category: digest["category"] as? String ?? "waiting"
                    ),
                    category: digest["category"] as? String ?? "waiting",
                    status: digest["status"] as? String ?? "",
                    flow: digest["flow"] as? Bool ?? false,
                    published: published,
                    timestamp: ts.map { Date(timeIntervalSince1970: $0) }
                )
            }

            DispatchQueue.main.async { [weak self] in
                self?.state.publishedFriendPreviews = previews
            }
        }
    }

    private func commitActivities(_ incoming: [UserActivity], preservingRelay: Bool) {
        var byId: [String: UserActivity] = [:]

        for existing in state.allActivity {
            if preservingRelay || !existing.id.hasPrefix("relay:") {
                byId[existing.id] = existing
            }
        }

        for activity in incoming {
            var updated = activity
            if let existing = byId[activity.id] {
                updated.history = existing.history
                updated.sessionStart = existing.sessionStart
            }
            byId[activity.id] = updated
        }

        state.allActivity = byId.values.sorted { a, b in
            if a.id == "me" { return true }
            if b.id == "me" { return false }
            return a.name < b.name
        }
        updateHangoutSuggestion()
    }

    private func updateHangoutSuggestion() {
        // "idle" counts as low-key for me (I'm free), but idle friends are AFK — not free.
        let myFreeCategories: Set<String> = ["browsing", "news", "reading", "idle"]
        let friendFreeCategories: Set<String> = ["browsing", "news", "reading"]
        let freeFriends = state.allActivity.filter {
            $0.id != "me" && friendFreeCategories.contains($0.category) && !$0.stale
        }
        let meIsLowKey = state.allActivity.first(where: { $0.id == "me" })
            .map { myFreeCategories.contains($0.category) } ?? false
        if meIsLowKey && freeFriends.count >= 1 {
            let names = freeFriends.map { $0.name }.joined(separator: " & ")
            state.hangoutSuggestion = "\(names) also free"
        } else {
            state.hangoutSuggestion = nil
        }
    }

    private func pollSingleActivity(
        id: String,
        name: String,
        serverURL: String,
        activityPort: String,
        completion: @Sendable @escaping (UserActivity?) -> Void
    ) {
        guard let wsURL = URL(string: serverURL),
              let host = wsURL.host else {
            completion(nil)
            return
        }

        let scheme = serverURL.hasPrefix("wss://") ? "https" : "http"
        let portSegment = activityPort.isEmpty ? "" : ":\(activityPort)"
        let httpURL = "\(scheme)://\(host)\(portSegment)/api/current_activity"

        guard let url = URL(string: httpURL) else {
            completion(nil)
            return
        }

        var request = URLRequest(url: url)
        request.timeoutInterval = 5.0

        // Sign with FishKey
        if let authValue = config.signRequest() {
            request.setValue(authValue, forHTTPHeaderField: "Authorization")
        }

        session.dataTask(with: request) { data, response, error in
            guard let data = data,
                  error == nil,
                  let http = response as? HTTPURLResponse,
                  http.statusCode == 200,
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            else {
                completion(nil)
                return
            }

            let emoji = json["emoji"] as? String ?? "❓"
            let category = json["category"] as? String ?? "idle"
            let status = json["status"] as? String ?? ""
            let stale = json["stale"] as? Bool ?? false
            let inFlow = json["flow"] as? Bool ?? false

            var activity = UserActivity(
                id: id,
                name: name,
                emoji: emoji,
                category: category,
                status: status,
                stale: stale
            )
            activity.inFlow = inFlow
            completion(activity)
        }.resume()
    }

    private func pollOwnActivity(
        id: String,
        name: String,
        completion: @Sendable @escaping (UserActivity?) -> Void
    ) {
        pollSingleActivityCandidate(
            id: id,
            name: name,
            serverURL: config.effectiveOwnServerURL,
            activityPorts: config.ownActivityPortCandidates(),
            index: 0,
            completion: completion
        )
    }

    private func pollSingleActivityCandidate(
        id: String,
        name: String,
        serverURL: String,
        activityPorts: [String],
        index: Int,
        completion: @Sendable @escaping (UserActivity?) -> Void
    ) {
        guard index < activityPorts.count else {
            completion(nil)
            return
        }
        pollSingleActivity(
            id: id,
            name: name,
            serverURL: serverURL,
            activityPort: activityPorts[index]
        ) { [weak self] activity in
            if let activity {
                completion(activity)
            } else {
                self?.pollSingleActivityCandidate(
                    id: id,
                    name: name,
                    serverURL: serverURL,
                    activityPorts: activityPorts,
                    index: index + 1,
                    completion: completion
                )
            }
        }
    }

    // MARK: - History polling

    private func pollAllHistory() {
        let group = DispatchGroup()
        let historyCollector = HistoryCollector()

        // Poll own server
        group.enter()
        pollOwnHistory(
            id: "me",
        ) { id, entries in
            historyCollector.set(id: id, entries: entries)
            group.leave()
        }

        group.notify(queue: .main) { [weak self] in
            guard let self else { return }
            let allHistory = historyCollector.all()

            self.state.allActivity = self.state.allActivity.map { user in
                var updated = user
                if let entries = allHistory[user.id] {
                    updated.history = entries
                    // Compute session start: walk history from newest, find where category changed
                    updated.sessionStart = Self.computeSessionStart(
                        currentCategory: user.category,
                        history: entries
                    )
                }
                return updated
            }
        }
    }

    private func pollSingleHistory(
        id: String,
        serverURL: String,
        activityPort: String,
        completion: @Sendable @escaping (String, [ActivityEntry]) -> Void
    ) {
        guard let wsURL = URL(string: serverURL),
              let host = wsURL.host else {
            completion(id, [])
            return
        }

        let scheme = serverURL.hasPrefix("wss://") ? "https" : "http"
        let portSegment = activityPort.isEmpty ? "" : ":\(activityPort)"
        let httpURL = "\(scheme)://\(host)\(portSegment)/api/activity_history?limit=200"

        guard let url = URL(string: httpURL) else {
            completion(id, [])
            return
        }

        var request = URLRequest(url: url)
        request.timeoutInterval = 5.0

        if let authValue = config.signRequest() {
            request.setValue(authValue, forHTTPHeaderField: "Authorization")
        }

        let capturedId = id
        session.dataTask(with: request) { data, response, error in
            guard let data = data,
                  error == nil,
                  let http = response as? HTTPURLResponse,
                  http.statusCode == 200,
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let rawEntries = json["entries"] as? [[String: Any]]
            else {
                completion(capturedId, [])
                return
            }

            let formatter = ISO8601DateFormatter()
            formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]

            let fallbackFormatter = ISO8601DateFormatter()
            fallbackFormatter.formatOptions = [.withInternetDateTime]

            let entries: [ActivityEntry] = rawEntries.compactMap { entry in
                guard let emoji = entry["emoji"] as? String,
                      let category = entry["category"] as? String,
                      let status = entry["status"] as? String,
                      let tsString = entry["timestamp"] as? String
                else { return nil }

                let timestamp = formatter.date(from: tsString)
                    ?? fallbackFormatter.date(from: tsString)
                    ?? Date()

                return ActivityEntry(
                    emoji: emoji,
                    category: category,
                    status: status,
                    timestamp: timestamp
                )
            }

            completion(capturedId, entries)
        }.resume()
    }

    private func pollOwnHistory(
        id: String,
        completion: @Sendable @escaping (String, [ActivityEntry]) -> Void
    ) {
        pollSingleHistoryCandidate(
            id: id,
            serverURL: config.effectiveOwnServerURL,
            activityPorts: config.ownActivityPortCandidates(),
            index: 0,
            completion: completion
        )
    }

    private func pollSingleHistoryCandidate(
        id: String,
        serverURL: String,
        activityPorts: [String],
        index: Int,
        completion: @Sendable @escaping (String, [ActivityEntry]) -> Void
    ) {
        guard index < activityPorts.count else {
            completion(id, [])
            return
        }
        pollSingleHistory(
            id: id,
            serverURL: serverURL,
            activityPort: activityPorts[index]
        ) { [weak self] id, entries in
            if !entries.isEmpty {
                completion(id, entries)
            } else {
                self?.pollSingleHistoryCandidate(
                    id: id,
                    serverURL: serverURL,
                    activityPorts: activityPorts,
                    index: index + 1,
                    completion: completion
                )
            }
        }
    }

    /// Walk history backwards from newest; find where category changed to determine session start.
    private static func computeSessionStart(currentCategory: String, history: [ActivityEntry]) -> Date? {
        guard !history.isEmpty else { return nil }
        // History is newest-first. Walk forward until category differs.
        var sessionStart = history[0].timestamp
        for entry in history {
            if entry.category == currentCategory {
                sessionStart = entry.timestamp
            } else {
                break
            }
        }
        return sessionStart
    }

    private static func embeddedActivityHistory(from digest: [String: Any]) -> [ActivityEntry] {
        guard let rawHistory = digest["history"] as? [[String: Any]] else { return [] }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]

        let fallbackFormatter = ISO8601DateFormatter()
        fallbackFormatter.formatOptions = [.withInternetDateTime]

        return rawHistory.compactMap { entry in
            guard let category = entry["category"] as? String,
                  let status = entry["status"] as? String
            else { return nil }
            let timestamp: Date
            if let ts = entry["ts"] as? Double {
                timestamp = Date(timeIntervalSince1970: ts)
            } else if let ts = entry["timestamp"] as? Double {
                timestamp = Date(timeIntervalSince1970: ts)
            } else if let ts = entry["timestamp"] as? String {
                timestamp = formatter.date(from: ts)
                    ?? fallbackFormatter.date(from: ts)
                    ?? Date()
            } else {
                return nil
            }
            return ActivityEntry(
                emoji: Self.displayEmoji(entry["emoji"] as? String, category: category),
                category: category,
                status: status,
                timestamp: timestamp
            )
        }
    }

    private static func displayEmoji(_ raw: String?, category: String) -> String {
        let fallback: String
        switch category {
        case "coding": fallback = "💻"
        case "debugging": fallback = "🔎"
        case "code review": fallback = "🧾"
        case "reading docs": fallback = "📚"
        case "design": fallback = "🎨"
        case "writing": fallback = "✍️"
        case "chat": fallback = "💬"
        case "email": fallback = "✉️"
        case "meeting": fallback = "📅"
        case "planning": fallback = "📅"
        case "settings": fallback = "🔒"
        case "filling out form": fallback = "📝"
        case "browsing": fallback = "🌐"
        case "news": fallback = "📰"
        case "reading": fallback = "🧠"
        case "gaming": fallback = "🎲"
        case "terminal": fallback = "⌨️"
        case "idle": fallback = "😴"
        default: fallback = "💻"
        }

        let value = (raw ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if value.isEmpty { return fallback }
        switch value.lowercased() {
        case ":crossed_swords:": return "⚔️"
        case ":game_die:": return "🎲"
        case ":video_game:": return "🎮"
        case ":computer:", ":laptop:": return "💻"
        case ":mag:": return "🔎"
        case ":memo:": return "🧾"
        case ":books:": return "📚"
        case ":art:": return "🎨"
        case ":speech_balloon:": return "💬"
        case ":email:": return "✉️"
        case ":calendar:": return "📅"
        case ":lock:": return "🔒"
        case ":pencil:", ":memo2:": return "📝"
        case ":globe_with_meridians:": return "🌐"
        case ":newspaper:": return "📰"
        case ":brain:": return "🧠"
        case ":keyboard:": return "⌨️"
        case ":zzz:": return "😴"
        default: break
        }
        if value.hasPrefix(":") || value.unicodeScalars.allSatisfy({ $0.isASCII }) {
            return fallback
        }
        return value
    }

    /// Thread-safe collector for history polling results.
    private final class HistoryCollector: @unchecked Sendable {
        private let lock = NSLock()
        private var results: [String: [ActivityEntry]] = [:]

        func set(id: String, entries: [ActivityEntry]) {
            lock.lock()
            results[id] = entries
            lock.unlock()
        }

        func all() -> [String: [ActivityEntry]] {
            lock.lock()
            let copy = results
            lock.unlock()
            return copy
        }
    }
}
