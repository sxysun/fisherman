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
        pollScreenpipe()
        pollFisherman()
        pollAllActivity()

        // Poll history every 20 cycles (~60s at 3s interval)
        pollCycleCount += 1
        if pollCycleCount % 20 == 0 {
            pollAllHistory()
        }
    }

    private func pollScreenpipe() {
        guard let url = URL(string: "http://127.0.0.1:3030/health") else { return }
        var request = URLRequest(url: url)
        request.timeoutInterval = 2.0

        session.dataTask(with: request) { [weak self] data, response, error in
            let ok: Bool
            if let http = response as? HTTPURLResponse {
                ok = (200..<300).contains(http.statusCode)
            } else {
                ok = false
            }
            DispatchQueue.main.async {
                self?.state.screenpipeHealthy = ok
            }
        }.resume()
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
                    screenpipeOK: self?.state.screenpipeHealthy ?? false,
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
        pollSingleActivity(
            id: "me",
            name: "me",
            serverURL: config.serverURL,
            activityPort: config.activityPort
        ) { activity in
            if let activity { collector.append(activity) }
            group.leave()
        }

        // Poll each friend's server
        for friend in config.friends {
            group.enter()
            pollSingleActivity(
                id: friend.name,
                name: friend.name,
                serverURL: friend.serverURL,
                activityPort: friend.activityPort
            ) { activity in
                if let activity { collector.append(activity) }
                group.leave()
            }
        }

        group.notify(queue: .main) { [weak self] in
            guard let self else { return }
            var activities = collector.sorted()

            // Compute working-together: compare each friend's category to "me"
            let myCategory = activities.first(where: { $0.id == "me" })?.category
            if let myCategory {
                activities = activities.map { user in
                    if user.id == "me" { return user }
                    var updated = user
                    updated.isWorkingTogether = (user.category == myCategory && myCategory != "idle")
                    return updated
                }
            }

            // Preserve history from previous state
            activities = activities.map { user in
                var updated = user
                if let existing = self.state.allActivity.first(where: { $0.id == user.id }) {
                    updated.history = existing.history
                    updated.sessionStart = existing.sessionStart
                }
                return updated
            }

            self.state.allActivity = activities
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
        let httpURL = "\(scheme)://\(host):\(activityPort)/api/current_activity"

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

            completion(UserActivity(
                id: id,
                name: name,
                emoji: emoji,
                category: category,
                status: status,
                stale: stale
            ))
        }.resume()
    }

    // MARK: - History polling

    private func pollAllHistory() {
        let group = DispatchGroup()
        let historyCollector = HistoryCollector()

        // Poll own server
        group.enter()
        pollSingleHistory(
            id: "me",
            serverURL: config.serverURL,
            activityPort: config.activityPort
        ) { id, entries in
            historyCollector.set(id: id, entries: entries)
            group.leave()
        }

        // Poll each friend's server
        for friend in config.friends {
            group.enter()
            pollSingleHistory(
                id: friend.name,
                serverURL: friend.serverURL,
                activityPort: friend.activityPort
            ) { id, entries in
                historyCollector.set(id: id, entries: entries)
                group.leave()
            }
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
        let httpURL = "\(scheme)://\(host):\(activityPort)/api/activity_history?limit=10"

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
