import CryptoKit
import Foundation
import Observation

struct Friend: Sendable {
    let name: String
    let publicKey: String      // hex
    let serverURL: String      // e.g. "ws://1.2.3.4:9999"
    let activityPort: String   // e.g. "9998"
}

struct FriendCode: Codable {
    let n: String  // display name
    let k: String  // pubkey hex
    let h: String  // hostname
    let w: Int     // ws port (default 9999)
    let a: Int     // activity port (default 9996)
}

@Observable
final class ConfigManager {
    var serverURL: String = "ws://localhost:9999/ingest"
    var controlPort: String = "7892"
    var activityPort: String = "9998"

    // Ed25519 key pair (hex-encoded)
    var privateKeyHex: String = ""
    var publicKeyHex: String = ""

    // Display name for friend codes
    var displayName: String = NSFullUserName().components(separatedBy: " ").first ?? NSUserName()

    // P2P friends
    var friends: [Friend] = []

    /// Lines from .env that we don't manage (comments, unknown keys)
    private var passthroughLines: [(index: Int, line: String)] = []
    /// Track which line indices hold our known keys
    private var knownKeyLines: [String: Int] = [:]

    private var envPath: String {
        findProjectDir() + "/.env"
    }

    func load() {
        passthroughLines = []
        knownKeyLines = [:]

        guard let contents = try? String(contentsOfFile: envPath, encoding: .utf8) else {
            return
        }

        let lines = contents.components(separatedBy: "\n")
        for (i, line) in lines.enumerated() {
            let trimmed = line.trimmingCharacters(in: .whitespaces)

            if let value = extractValue(trimmed, key: "FISH_SERVER_URL") {
                serverURL = value
                knownKeyLines["FISH_SERVER_URL"] = i
            } else if let value = extractValue(trimmed, key: "FISH_CONTROL_PORT") {
                controlPort = value
                knownKeyLines["FISH_CONTROL_PORT"] = i
            } else if let value = extractValue(trimmed, key: "FISH_ACTIVITY_PORT") {
                activityPort = value
                knownKeyLines["FISH_ACTIVITY_PORT"] = i
            } else if let value = extractValue(trimmed, key: "FISH_PRIVATE_KEY") {
                privateKeyHex = value
                knownKeyLines["FISH_PRIVATE_KEY"] = i
                // Derive public key
                if let privData = hexToData(value),
                   let signing = try? Curve25519.Signing.PrivateKey(rawRepresentation: privData)
                {
                    publicKeyHex = signing.publicKey.rawRepresentation
                        .map { String(format: "%02x", $0) }.joined()
                }
            } else if let value = extractValue(trimmed, key: "FISH_DISPLAY_NAME") {
                displayName = value
                knownKeyLines["FISH_DISPLAY_NAME"] = i
            } else if let value = extractValue(trimmed, key: "FISH_FRIENDS") {
                knownKeyLines["FISH_FRIENDS"] = i
                friends = parseFriends(value)
            } else {
                passthroughLines.append((index: i, line: line))
            }
        }

        // Generate key pair on first load if not present
        if privateKeyHex.isEmpty {
            generateKeyPair()
            save()
        }
    }

    func save() {
        let existingLines: [String]
        if let contents = try? String(contentsOfFile: envPath, encoding: .utf8) {
            existingLines = contents.components(separatedBy: "\n")
        } else {
            existingLines = []
        }

        var outputLines = existingLines
        var keysWritten = Set<String>()

        let friendsStr = friends.map { "\($0.name)|\($0.publicKey)|\($0.serverURL)|\($0.activityPort)" }
            .joined(separator: ",")

        let updates: [(String, String)] = [
            ("FISH_SERVER_URL", serverURL),
            ("FISH_CONTROL_PORT", controlPort),
            ("FISH_PRIVATE_KEY", privateKeyHex),
            ("FISH_DISPLAY_NAME", displayName),
            ("FISH_FRIENDS", friendsStr),
        ]

        for (key, value) in updates {
            if let lineIndex = knownKeyLines[key], lineIndex < outputLines.count {
                outputLines[lineIndex] = "\(key)=\(value)"
                keysWritten.insert(key)
            }
        }

        for (key, value) in updates where !keysWritten.contains(key) {
            if key == "FISH_FRIENDS" && value.isEmpty { continue }
            outputLines.append("\(key)=\(value)")
        }

        while let last = outputLines.last, last.isEmpty {
            outputLines.removeLast()
        }

        let output = outputLines.joined(separator: "\n") + "\n"

        let dir = (envPath as NSString).deletingLastPathComponent
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)

        try? output.write(toFile: envPath, atomically: true, encoding: .utf8)
        NSLog("[Fisherman] saved config to \(envPath)")
    }

    var isConfigured: Bool {
        !serverURL.isEmpty && serverURL != "ws://localhost:9999/ingest"
    }

    // MARK: - Ed25519 signing

    /// Create FishKey auth header value for HTTP requests.
    func signRequest() -> String? {
        guard let privData = hexToData(privateKeyHex),
              let signingKey = try? Curve25519.Signing.PrivateKey(rawRepresentation: privData)
        else { return nil }

        let timestamp = Int(Date().timeIntervalSince1970)
        let message = "fisherman:\(timestamp)".data(using: .utf8)!
        guard let signature = try? signingKey.signature(for: message) else { return nil }

        let sigHex = signature.withUnsafeBytes {
            Data($0).map { String(format: "%02x", $0) }.joined()
        }
        return "FishKey \(publicKeyHex):\(timestamp):\(sigHex)"
    }

    // MARK: - Friend management

    func addFriend(name: String, publicKey: String, serverURL: String, activityPort: String) {
        let friend = Friend(name: name, publicKey: publicKey, serverURL: serverURL, activityPort: activityPort)
        friends.append(friend)
        save()
    }

    func removeFriend(at index: Int) {
        guard index >= 0, index < friends.count else { return }
        let friend = friends[index]
        friends.remove(at: index)
        save()
        // Also remove from server allow-list
        deregisterFriendOnServer(pubkey: friend.publicKey)
    }

    // MARK: - Friend codes

    /// Generate a friend code encoding this user's connection info.
    func generateFriendCode(name: String) -> String? {
        guard !publicKeyHex.isEmpty else { return nil }

        // Extract host and ws port from serverURL (e.g. "ws://1.2.3.4:9999/ingest")
        guard let url = URL(string: serverURL),
              let host = url.host else { return nil }
        let wsPort = url.port ?? 9999
        let actPort = Int(activityPort) ?? 9998

        let code = FriendCode(n: name, k: publicKeyHex, h: host, w: wsPort, a: actPort)
        guard let jsonData = try? JSONEncoder().encode(code) else { return nil }

        let base64 = jsonData.base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")

        return "fish:\(base64)"
    }

    /// Parse a `fish:<base64url>` friend code string.
    static func parseFriendCode(_ code: String) -> FriendCode? {
        let trimmed = code.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.hasPrefix("fish:") else { return nil }

        var base64 = String(trimmed.dropFirst(5))
            .replacingOccurrences(of: "-", with: "+")
            .replacingOccurrences(of: "_", with: "/")

        // Restore padding
        let remainder = base64.count % 4
        if remainder > 0 { base64 += String(repeating: "=", count: 4 - remainder) }

        guard let data = Data(base64Encoded: base64) else { return nil }
        return try? JSONDecoder().decode(FriendCode.self, from: data)
    }

    /// Add a friend from a parsed code, optionally overriding the display name.
    func addFriendFromCode(_ code: FriendCode, overrideName: String? = nil) {
        let name = overrideName?.isEmpty == false ? overrideName! : code.n
        let wsURL = "ws://\(code.h):\(code.w)"
        let actPort = String(code.a)

        addFriend(name: name, publicKey: code.k, serverURL: wsURL, activityPort: actPort)
        registerFriendOnServer(pubkey: code.k)
    }

    /// POST friend pubkey to own server's /api/friends (owner auth).
    func registerFriendOnServer(pubkey: String) {
        guard let authValue = signRequest() else { return }

        // Build HTTP URL from WS URL
        let httpBase = httpBaseURL()
        guard let url = URL(string: "\(httpBase)/api/friends") else { return }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(authValue, forHTTPHeaderField: "Authorization")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["pubkey": pubkey])

        URLSession.shared.dataTask(with: request) { data, response, error in
            if let error = error {
                NSLog("[Fisherman] register friend failed: \(error)")
                return
            }
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            NSLog("[Fisherman] register friend response: \(status)")
        }.resume()
    }

    /// DELETE friend pubkey from own server's /api/friends.
    private func deregisterFriendOnServer(pubkey: String) {
        guard let authValue = signRequest() else { return }

        let httpBase = httpBaseURL()
        guard let url = URL(string: "\(httpBase)/api/friends") else { return }

        var request = URLRequest(url: url)
        request.httpMethod = "DELETE"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(authValue, forHTTPHeaderField: "Authorization")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["pubkey": pubkey])

        URLSession.shared.dataTask(with: request) { _, response, error in
            if let error = error {
                NSLog("[Fisherman] deregister friend failed: \(error)")
                return
            }
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            NSLog("[Fisherman] deregister friend response: \(status)")
        }.resume()
    }

    /// Derive HTTP API base URL from the WS server URL.
    private func httpBaseURL() -> String {
        // serverURL is like "ws://host:9999/ingest" — HTTP API is on activityPort or 9998
        guard let url = URL(string: serverURL), let host = url.host else {
            return "http://localhost:9998"
        }
        // The HTTP API runs on HTTP_API_PORT (default 9998) which is stored as activityPort
        let scheme = serverURL.hasPrefix("wss://") ? "https" : "http"
        let port = Int(activityPort) ?? 9998
        return "\(scheme)://\(host):\(port)"
    }

    // MARK: - Private helpers

    private func generateKeyPair() {
        let key = Curve25519.Signing.PrivateKey()
        privateKeyHex = key.rawRepresentation.map { String(format: "%02x", $0) }.joined()
        publicKeyHex = key.publicKey.rawRepresentation.map { String(format: "%02x", $0) }.joined()
        NSLog("[Fisherman] generated new ed25519 key pair, pubkey: \(publicKeyHex)")
    }

    private func parseFriends(_ value: String) -> [Friend] {
        guard !value.isEmpty else { return [] }
        return value.split(separator: ",").compactMap { entry in
            let parts = entry.split(separator: "|", maxSplits: 3)
            guard parts.count == 4 else { return nil }
            return Friend(
                name: String(parts[0]),
                publicKey: String(parts[1]),
                serverURL: String(parts[2]),
                activityPort: String(parts[3])
            )
        }
    }

    private func extractValue(_ line: String, key: String) -> String? {
        let prefix = "\(key)="
        guard line.hasPrefix(prefix) else { return nil }
        return String(line.dropFirst(prefix.count)).trimmingCharacters(in: .whitespaces)
    }

    private func hexToData(_ hex: String) -> Data? {
        guard hex.count % 2 == 0, !hex.isEmpty else { return nil }
        var data = Data(capacity: hex.count / 2)
        var index = hex.startIndex
        while index < hex.endIndex {
            let nextIndex = hex.index(index, offsetBy: 2)
            guard let byte = UInt8(hex[index..<nextIndex], radix: 16) else { return nil }
            data.append(byte)
            index = nextIndex
        }
        return data
    }
}
