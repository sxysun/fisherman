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

    init(baseURLString: String) {
        guard let url = URL(string: baseURLString) else {
            fatalError("invalid HARNESS_URL: \(baseURLString)")
        }
        self.baseURL = url
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 15
        self.session = URLSession(configuration: cfg)
    }

    func getPending(completion: @escaping (PendingPayload?) -> Void) {
        let url = baseURL.appendingPathComponent("pending")
        session.dataTask(with: url) { data, _, _ in
            guard let data = data, !data.isEmpty else {
                completion(nil); return
            }
            let trimmed = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            if trimmed == "null" {
                completion(nil); return
            }
            do {
                let payload = try JSONDecoder().decode(PendingPayload.self, from: data)
                completion(payload)
            } catch {
                completion(nil)
            }
        }.resume()
    }

    func postDeliveryAck(decisionID: String) {
        let url = baseURL.appendingPathComponent("delivery-ack")
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["id": decisionID])
        session.dataTask(with: req).resume()
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
                completion?(true)
                return
            }
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
}
