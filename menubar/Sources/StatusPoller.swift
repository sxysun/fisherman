import Foundation

final class StatusPoller: @unchecked Sendable {
    private let state: AppState
    private let controlPort: String
    private var timer: Timer?
    private let interval: TimeInterval = 3.0
    private let session: URLSession

    init(state: AppState, controlPort: String) {
        self.state = state
        self.controlPort = controlPort

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
                self?.deriveStatus()
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

    private func deriveStatus() {
        // Called after screenpipe poll — fisherman poll will call update() which also derives
        // This just ensures screenpipe-only state changes reflect immediately
        state.update(
            screenpipeOK: state.screenpipeHealthy,
            fishermanStatus: state.fishermanRunning ? [:] : nil
        )
    }
}
