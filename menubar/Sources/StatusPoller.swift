import Foundation

final class StatusPoller: @unchecked Sendable {
    private let state: AppState
    private let controlPort: String
    private let config: ConfigManager
    private var timer: Timer?
    private let interval: TimeInterval = 3.0
    private let session: URLSession

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
            self?.state.allActivity = collector.sorted()
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
}
