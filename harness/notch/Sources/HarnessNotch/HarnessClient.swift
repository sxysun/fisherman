import Foundation

struct PendingPayload: Codable {
    let decisionID: String
    let candidateID: String
    let intent: String?
    let message: String
    let ts: String?
    let expiresAtUnix: Double?

    enum CodingKeys: String, CodingKey {
        case decisionID = "decision_id"
        case candidateID = "candidate_id"
        case intent
        case message
        case ts
        case expiresAtUnix = "expires_at_unix"
    }
}

struct InteractionEvent: Codable {
    let t_ms: Int
    let kind: String     // "approach", "leave_proximity", "hover_start", "hover_end"
    let target: String?  // button id for hover events: "yes" | "later" | "dismiss"
}

final class HarnessClient {
    private let baseURL: URL
    private let session: URLSession
    private let logURL: URL

    init(baseURLString: String) {
        guard let url = URL(string: baseURLString) else {
            fatalError("invalid HARNESS_URL: \(baseURLString)")
        }
        self.baseURL = url
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 6
        cfg.timeoutIntervalForResource = 8
        cfg.httpShouldUsePipelining = false
        cfg.httpMaximumConnectionsPerHost = 1
        self.session = URLSession(configuration: cfg)
        let home = FileManager.default.homeDirectoryForCurrentUser
        self.logURL = home.appendingPathComponent(".harness/notch.log")
    }

    func getPending(completion: @escaping (PendingPayload?) -> Void) {
        let url = baseURL.appendingPathComponent("pending")
        var req = URLRequest(url: url)
        req.httpMethod = "GET"
        req.setValue("close", forHTTPHeaderField: "Connection")
        session.dataTask(with: req) { data, response, error in
            if let error {
                self.log("pending request failed: \(error.localizedDescription)")
                completion(nil); return
            }
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            if status != 200 {
                self.log("pending request status=\(status)")
                completion(nil); return
            }
            guard let data = data, !data.isEmpty else {
                completion(nil); return
            }
            let trimmed = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            if trimmed == "null" {
                completion(nil); return
            }
            do {
                let payload = try JSONDecoder().decode(PendingPayload.self, from: data)
                self.log("pending decoded decision=\(payload.decisionID)")
                completion(payload)
            } catch {
                let preview = String(data: data, encoding: .utf8)?.prefix(240) ?? ""
                self.log("pending decode failed: \(error); payload=\(preview)")
                completion(nil)
            }
        }.resume()
    }

    func postDeliveryAck(decisionID: String, completion: ((Bool) -> Void)? = nil) {
        postDeliveryAckAttempt(decisionID: decisionID, attemptsRemaining: 3, completion: completion)
    }

    private func postDeliveryAckAttempt(
        decisionID: String,
        attemptsRemaining: Int,
        completion: ((Bool) -> Void)?
    ) {
        let url = baseURL.appendingPathComponent("delivery-ack")
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue("close", forHTTPHeaderField: "Connection")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["id": decisionID])
        session.dataTask(with: req) { [weak self] _, response, error in
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            let ok = error == nil && (200..<300).contains(status)
            if ok {
                self?.log("delivery ack ok decision=\(decisionID)")
                completion?(true)
                return
            }
            self?.log("delivery ack failed decision=\(decisionID) status=\(status) error=\(error?.localizedDescription ?? "none")")
            guard let self, attemptsRemaining > 1 else {
                completion?(false)
                return
            }
            DispatchQueue.global().asyncAfter(deadline: .now() + 0.75) {
                self.postDeliveryAckAttempt(
                    decisionID: decisionID,
                    attemptsRemaining: attemptsRemaining - 1,
                    completion: completion
                )
            }
        }.resume()
    }

    func postOutcome(
        decisionID: String,
        action: String,
        latencyMs: Int,
        interactions: [InteractionEvent] = [],
        completion: ((Bool) -> Void)? = nil
    ) {
        postOutcomeAttempt(
            decisionID: decisionID,
            action: action,
            latencyMs: latencyMs,
            interactions: interactions,
            attemptsRemaining: 3,
            completion: completion
        )
    }

    private func postOutcomeAttempt(
        decisionID: String,
        action: String,
        latencyMs: Int,
        interactions: [InteractionEvent],
        attemptsRemaining: Int,
        completion: ((Bool) -> Void)?
    ) {
        let url = baseURL.appendingPathComponent("outcome")
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue("close", forHTTPHeaderField: "Connection")
        let interactionsJSON: [[String: Any]] = interactions.map { ev in
            var d: [String: Any] = ["t_ms": ev.t_ms, "kind": ev.kind]
            if let t = ev.target { d["target"] = t }
            return d
        }
        let body: [String: Any] = [
            "id": decisionID,
            "user_action": action,
            "latency_ms": latencyMs,
            "interactions": interactionsJSON,
        ]
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        session.dataTask(with: req) { [weak self] _, response, error in
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            let ok = error == nil && (200..<300).contains(status)
            if ok {
                self?.log("outcome ok decision=\(decisionID) action=\(action)")
                completion?(true)
                return
            }
            self?.log("outcome failed decision=\(decisionID) status=\(status) error=\(error?.localizedDescription ?? "none") attempts=\(attemptsRemaining)")
            guard let self = self, attemptsRemaining > 1 else {
                completion?(false)
                return
            }
            DispatchQueue.global().asyncAfter(deadline: .now() + 0.75) {
                self.postOutcomeAttempt(
                    decisionID: decisionID,
                    action: action,
                    latencyMs: latencyMs,
                    interactions: interactions,
                    attemptsRemaining: attemptsRemaining - 1,
                    completion: completion
                )
            }
        }.resume()
    }

    func logEvent(_ message: String) {
        log(message)
    }

    private func log(_ message: String) {
        let stamp = ISO8601DateFormatter().string(from: Date())
        let line = "\(stamp) \(message)\n"
        guard let data = line.data(using: .utf8) else { return }
        do {
            try FileManager.default.createDirectory(
                at: logURL.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            if FileManager.default.fileExists(atPath: logURL.path) {
                let handle = try FileHandle(forWritingTo: logURL)
                try handle.seekToEnd()
                try handle.write(contentsOf: data)
                try handle.close()
            } else {
                try data.write(to: logURL)
            }
        } catch {
            // Logging must never affect notification delivery.
        }
    }
}
