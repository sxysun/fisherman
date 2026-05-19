import Foundation

/// Settings/diagnostics API client. Talks to the harness daemon's
/// /dashboard/* endpoints. Decoded as freeform JSON because the config
/// schema is intentionally flexible (TOML tables can grow).
enum HarnessAPI {
    private static func baseURL() -> URL {
        let s = ProcessInfo.processInfo.environment["HARNESS_URL"] ?? "http://127.0.0.1:7893"
        return URL(string: s)!
    }

    private static var session: URLSession = {
        let c = URLSessionConfiguration.ephemeral
        c.timeoutIntervalForRequest = 6
        return URLSession(configuration: c)
    }()

    static func fetchData(window: String = "24h") async -> JSON? {
        var c = URLComponents(url: baseURL().appendingPathComponent("dashboard/data"), resolvingAgainstBaseURL: false)!
        c.queryItems = [URLQueryItem(name: "window", value: window)]
        return await getJSON(url: c.url!)
    }

    static func fetchConfig() async -> JSON? {
        await getJSON(url: baseURL().appendingPathComponent("dashboard/config"))
    }

    static func fetchPolicyState() async -> JSON? {
        await getJSON(url: baseURL().appendingPathComponent("status"))
    }

    static func fetchMetrics(window: String = "24h") async -> JSON? {
        var c = URLComponents(url: baseURL().appendingPathComponent("metrics"), resolvingAgainstBaseURL: false)!
        c.queryItems = [URLQueryItem(name: "window", value: window)]
        return await getJSON(url: c.url!)
    }

    static func saveConfig(_ cfg: JSON) async -> Bool {
        var req = URLRequest(url: baseURL().appendingPathComponent("dashboard/config"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? cfg.toData()
        do {
            let (_, resp) = try await session.data(for: req)
            return (resp as? HTTPURLResponse)?.statusCode == 200
        } catch { return false }
    }

    static func snooze(duration: String) async {
        var c = URLComponents(url: baseURL().appendingPathComponent("snooze"), resolvingAgainstBaseURL: false)!
        c.queryItems = [URLQueryItem(name: "duration", value: duration)]
        var req = URLRequest(url: c.url!); req.httpMethod = "POST"
        _ = try? await session.data(for: req)
    }

    static func unsnooze() async {
        var req = URLRequest(url: baseURL().appendingPathComponent("unsnooze"))
        req.httpMethod = "POST"
        _ = try? await session.data(for: req)
    }

    static func setGoal(_ goal: String, sensitivity: String) async {
        var req = URLRequest(url: baseURL().appendingPathComponent("goal"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let body: [String: Any] = ["goal": goal, "sensitivity": sensitivity]
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        _ = try? await session.data(for: req)
    }

    static func clearGoal() async {
        var req = URLRequest(url: baseURL().appendingPathComponent("goal/clear"))
        req.httpMethod = "POST"
        _ = try? await session.data(for: req)
    }

    private static func getJSON(url: URL) async -> JSON? {
        do {
            let (data, resp) = try await session.data(from: url)
            guard (resp as? HTTPURLResponse)?.statusCode == 200 else { return nil }
            let obj = try JSONSerialization.jsonObject(with: data)
            return JSON(any: obj)
        } catch { return nil }
    }
}

/// Lightweight Any-like JSON wrapper to bridge dynamic TOML config and
/// SwiftUI bindings without writing a full Codable schema for every tab.
struct JSON {
    var raw: Any

    init(any: Any) { self.raw = any }

    var dict: [String: Any] {
        get { raw as? [String: Any] ?? [:] }
        set { raw = newValue }
    }
    var list: [Any] { raw as? [Any] ?? [] }
    var string: String { raw as? String ?? "" }
    var int: Int { (raw as? Int) ?? Int(raw as? Double ?? 0) }
    var double: Double { (raw as? Double) ?? Double(raw as? Int ?? 0) }
    var bool: Bool { raw as? Bool ?? false }
    var isNull: Bool { raw is NSNull }

    subscript(key: String) -> JSON {
        get {
            let d = raw as? [String: Any] ?? [:]
            return JSON(any: d[key] ?? NSNull())
        }
        set {
            var d = raw as? [String: Any] ?? [:]
            if newValue.raw is NSNull { d.removeValue(forKey: key) }
            else { d[key] = newValue.raw }
            raw = d
        }
    }

    func toData() throws -> Data {
        try JSONSerialization.data(withJSONObject: raw, options: [.fragmentsAllowed])
    }
}
