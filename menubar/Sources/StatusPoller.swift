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
        config.httpMaximumConnectionsPerHost = 2
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
        pollActivity()
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

    private func pollActivity() {
        // Build activity API URL from server URL + configurable activity port
        // e.g. ws://host:9999/ingest -> http://host:9998/api/current_activity
        guard let wsURL = URL(string: config.serverURL),
              let host = wsURL.host else { return }

        let scheme = config.serverURL.hasPrefix("wss://") ? "https" : "http"
        let httpURL = "\(scheme)://\(host):\(config.activityPort)/api/current_activity"

        guard let url = URL(string: httpURL) else { return }

        var request = URLRequest(url: url)
        request.timeoutInterval = 5.0

        if !config.authToken.isEmpty {
            request.setValue("Bearer \(config.authToken)", forHTTPHeaderField: "Authorization")
        }

        session.dataTask(with: request) { [weak self] data, response, error in
            guard let self = self,
                  let data = data,
                  error == nil,
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            else { return }

            let category = json["category"] as? String
            let status = json["status"] as? String
            let updatedAt = json["updated_at"] as? String

            DispatchQueue.main.async {
                self.state.activityCategory = category
                self.state.currentActivity = status
                self.state.activityUpdatedAt = updatedAt
            }
        }.resume()
    }
}
