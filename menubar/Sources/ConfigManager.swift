import CryptoKit
import Foundation
import Observation

enum SharingTier: String, Sendable {
    case low = "low"    // emoji + category only (is this person free?)
    case high = "high"  // full status + history (what are they working on?)
}

struct Friend: Sendable {
    let name: String
    let publicKey: String      // hex
    let serverURL: String      // e.g. "ws://1.2.3.4:9999"
    let activityPort: String   // e.g. "9998"
    var sharingTier: SharingTier = .high
}

struct RelayFriend: Sendable {
    let name: String
    let pubkeyHex: String
    let relayURL: String?
    let addedAt: Double?
}

struct FriendCode: Codable {
    let n: String  // display name
    let k: String  // pubkey hex
    let h: String? // server-direct hostname
    let w: Int?    // server-direct ws port
    let a: Int?    // server-direct activity port
    let g: String? // relay friends-group key
    let r: String? // relay URL
}

@Observable
final class ConfigManager {
    private let defaultServerURL = "ws://localhost:9999/ingest"

    var backendMode: String = "local"
    var backendURL: String = ""
    var statusRelayURL: String = "https://relay.fisherman.teleport.computer"
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
    var relayFriends: [RelayFriend] = []

    /// Lines from .env that we don't manage (comments, unknown keys)
    private var passthroughLines: [(index: Int, line: String)] = []
    /// Track which line indices hold our known keys
    private var knownKeyLines: [String: [Int]] = [:]

    private var envPath: String {
        userEnvPath()
    }

    func load() {
        passthroughLines = []
        knownKeyLines = [:]
        var loadedKeys = Set<String>()
        var needsSave = false

        if let contents = try? String(contentsOfFile: envPath, encoding: .utf8) {
            parseEnv(contents, trackLines: true, fillMissingOnly: false, loadedKeys: &loadedKeys)
        }

        if let projectPath = projectEnvPath(),
           let contents = try? String(contentsOfFile: projectPath, encoding: .utf8)
        {
            needsSave = parseEnv(
                contents,
                trackLines: false,
                fillMissingOnly: true,
                loadedKeys: &loadedKeys
            ) || needsSave
        }

        if !loadedKeys.contains("FISH_BACKEND_MODE") {
            if !serverURL.isEmpty && serverURL != defaultServerURL {
                backendMode = "self_hosted"
                if backendURL.isEmpty { backendURL = serverURL }
            } else {
                backendMode = "local"
            }
            needsSave = true
        }

        normalizeBackendURLs(loadedKeys: loadedKeys, needsSave: &needsSave)

        // Generate key pair on first load if not present anywhere we support.
        if privateKeyHex.isEmpty {
            generateKeyPair()
            needsSave = true
        }

        if needsSave {
            save()
        }
        refreshRelayFriends()
    }

    @discardableResult
    private func parseEnv(
        _ contents: String,
        trackLines: Bool,
        fillMissingOnly: Bool,
        loadedKeys: inout Set<String>
    ) -> Bool {
        var loadedAnyManagedValue = false
        let lines = contents.components(separatedBy: "\n")
        for (i, line) in lines.enumerated() {
            let trimmed = line.trimmingCharacters(in: .whitespaces)

            if let value = extractValue(trimmed, key: "FISH_BACKEND_MODE") {
                if shouldLoad("FISH_BACKEND_MODE", fillMissingOnly: fillMissingOnly, loadedKeys: loadedKeys) {
                    backendMode = value
                    loadedKeys.insert("FISH_BACKEND_MODE")
                    loadedAnyManagedValue = true
                }
                if trackLines { knownKeyLines["FISH_BACKEND_MODE", default: []].append(i) }
            } else if let value = extractValue(trimmed, key: "FISH_BACKEND_URL") {
                if shouldLoad("FISH_BACKEND_URL", fillMissingOnly: fillMissingOnly, loadedKeys: loadedKeys) {
                    backendURL = value
                    loadedKeys.insert("FISH_BACKEND_URL")
                    loadedAnyManagedValue = true
                }
                if trackLines { knownKeyLines["FISH_BACKEND_URL", default: []].append(i) }
            } else if let value = extractValue(trimmed, key: "FISH_STATUS_RELAY_URL") {
                if shouldLoad("FISH_STATUS_RELAY_URL", fillMissingOnly: fillMissingOnly, loadedKeys: loadedKeys) {
                    statusRelayURL = value
                    loadedKeys.insert("FISH_STATUS_RELAY_URL")
                    loadedAnyManagedValue = true
                }
                if trackLines { knownKeyLines["FISH_STATUS_RELAY_URL", default: []].append(i) }
            } else if let value = extractValue(trimmed, key: "FISH_LEDGER_URL") {
                if shouldLoad("FISH_LEDGER_URL", fillMissingOnly: fillMissingOnly, loadedKeys: loadedKeys)
                    && shouldLoad("FISH_STATUS_RELAY_URL", fillMissingOnly: fillMissingOnly, loadedKeys: loadedKeys)
                {
                    statusRelayURL = value
                    loadedKeys.insert("FISH_LEDGER_URL")
                    loadedAnyManagedValue = true
                }
                if trackLines { knownKeyLines["FISH_LEDGER_URL", default: []].append(i) }
            } else if let value = extractValue(trimmed, key: "FISH_SERVER_URL") {
                if shouldLoad("FISH_SERVER_URL", fillMissingOnly: fillMissingOnly, loadedKeys: loadedKeys) {
                    serverURL = value
                    loadedKeys.insert("FISH_SERVER_URL")
                    loadedAnyManagedValue = true
                }
                if trackLines { knownKeyLines["FISH_SERVER_URL", default: []].append(i) }
            } else if let value = extractValue(trimmed, key: "FISH_CONTROL_PORT") {
                if shouldLoad("FISH_CONTROL_PORT", fillMissingOnly: fillMissingOnly, loadedKeys: loadedKeys) {
                    controlPort = value
                    loadedKeys.insert("FISH_CONTROL_PORT")
                    loadedAnyManagedValue = true
                }
                if trackLines { knownKeyLines["FISH_CONTROL_PORT", default: []].append(i) }
            } else if let value = extractValue(trimmed, key: "FISH_ACTIVITY_PORT") {
                if shouldLoad("FISH_ACTIVITY_PORT", fillMissingOnly: fillMissingOnly, loadedKeys: loadedKeys) {
                    activityPort = value
                    loadedKeys.insert("FISH_ACTIVITY_PORT")
                    loadedAnyManagedValue = true
                }
                if trackLines { knownKeyLines["FISH_ACTIVITY_PORT", default: []].append(i) }
            } else if let value = extractValue(trimmed, key: "FISH_PRIVATE_KEY") {
                if shouldLoad("FISH_PRIVATE_KEY", fillMissingOnly: fillMissingOnly, loadedKeys: loadedKeys) {
                    privateKeyHex = value
                    loadedKeys.insert("FISH_PRIVATE_KEY")
                    loadedAnyManagedValue = true
                    // Derive public key
                    if let privData = hexToData(value),
                       let signing = try? Curve25519.Signing.PrivateKey(rawRepresentation: privData)
                    {
                        publicKeyHex = signing.publicKey.rawRepresentation
                            .map { String(format: "%02x", $0) }.joined()
                    }
                }
                if trackLines { knownKeyLines["FISH_PRIVATE_KEY", default: []].append(i) }
            } else if let value = extractValue(trimmed, key: "FISH_DISPLAY_NAME") {
                if shouldLoad("FISH_DISPLAY_NAME", fillMissingOnly: fillMissingOnly, loadedKeys: loadedKeys) {
                    displayName = value
                    loadedKeys.insert("FISH_DISPLAY_NAME")
                    loadedAnyManagedValue = true
                }
                if trackLines { knownKeyLines["FISH_DISPLAY_NAME", default: []].append(i) }
            } else if let value = extractValue(trimmed, key: "FISH_FRIENDS") {
                if shouldLoad("FISH_FRIENDS", fillMissingOnly: fillMissingOnly, loadedKeys: loadedKeys) {
                    friends = parseFriends(value)
                    loadedKeys.insert("FISH_FRIENDS")
                    loadedAnyManagedValue = true
                }
                if trackLines { knownKeyLines["FISH_FRIENDS", default: []].append(i) }
            } else {
                if trackLines {
                    passthroughLines.append((index: i, line: line))
                }
            }
        }
        return fillMissingOnly && loadedAnyManagedValue
    }

    private func shouldLoad(
        _ key: String,
        fillMissingOnly: Bool,
        loadedKeys: Set<String>
    ) -> Bool {
        !fillMissingOnly || !loadedKeys.contains(key)
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

        let friendsStr = friends.map { "\($0.name)|\($0.publicKey)|\($0.serverURL)|\($0.activityPort)|\($0.sharingTier.rawValue)" }
            .joined(separator: ",")

        let updates: [(String, String)] = [
            ("FISH_BACKEND_MODE", backendMode),
            ("FISH_BACKEND_URL", backendURL),
            ("FISH_STATUS_RELAY_URL", statusRelayURL),
            ("FISH_CONTROL_PORT", controlPort),
            ("FISH_PRIVATE_KEY", privateKeyHex),
            ("FISH_DISPLAY_NAME", displayName),
            ("FISH_FRIENDS", friendsStr),
        ]

        // Track which line indices to delete (duplicates of any tracked key)
        var indicesToDelete = Set<Int>()
        for key in ["FISH_SERVER_URL"] {
            for index in knownKeyLines[key] ?? [] {
                indicesToDelete.insert(index)
            }
        }

        for (key, value) in updates {
            guard let indices = knownKeyLines[key], !indices.isEmpty else { continue }
            let sorted = indices.sorted()
            // Update the first occurrence
            if sorted[0] < outputLines.count {
                outputLines[sorted[0]] = "\(key)=\(value)"
                keysWritten.insert(key)
            }
            // Mark the rest as duplicates to delete
            for dupIndex in sorted.dropFirst() {
                indicesToDelete.insert(dupIndex)
            }
        }

        // Remove duplicates (in reverse order so indices stay valid)
        for index in indicesToDelete.sorted(by: >) {
            if index < outputLines.count {
                outputLines.remove(at: index)
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
        try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: envPath)
        NSLog("[Fisherman] saved config to \(envPath)")
    }

    var isConfigured: Bool {
        backendMode == "local" || !backendURL.isEmpty || !serverURL.isEmpty
    }

    var effectiveOwnServerURL: String {
        if backendMode == "self_hosted" && !backendURL.isEmpty {
            return Self.ingestURL(from: backendURL)
        }
        if backendMode == "cloud", backendURL.hasPrefix("ws://") || backendURL.hasPrefix("wss://") {
            return Self.ingestURL(from: backendURL)
        }
        return serverURL
    }

    func ownActivityPortCandidates() -> [String] {
        var ports: [String] = []
        appendUniquePort(activityPort, to: &ports)
        // Historical self-hosted installs used either 9998 (repo default) or
        // 9996 (current EC2/systemd setup). Try both so settings can expose a
        // single backend URL instead of a second "activity port" knob.
        appendUniquePort("9998", to: &ports)
        appendUniquePort("9996", to: &ports)
        return ports
    }

    func ownHTTPBaseURLCandidates() -> [String] {
        guard let url = URL(string: effectiveOwnServerURL), let host = url.host else {
            return ["http://localhost:9998"]
        }
        let scheme = effectiveOwnServerURL.hasPrefix("wss://") ? "https" : "http"
        return ownActivityPortCandidates().map { "\(scheme)://\(host):\($0)" }
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

    func toggleFriendTier(name: String) {
        guard let idx = friends.firstIndex(where: { $0.name == name }) else { return }
        let current = friends[idx].sharingTier
        friends[idx].sharingTier = (current == .high) ? .low : .high
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

    func refreshRelayFriends() {
        let path = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".fisherman/friends.json")
        guard let data = try? Data(contentsOf: path) else {
            relayFriends = []
            return
        }

        let raw = try? JSONSerialization.jsonObject(with: data)
        let rows: [[String: Any]]
        if let list = raw as? [[String: Any]] {
            rows = list
        } else if let object = raw as? [String: Any],
                  let list = object["friends"] as? [[String: Any]]
        {
            rows = list
        } else {
            relayFriends = []
            return
        }

        relayFriends = rows.compactMap { row in
            guard let name = row["name"] as? String,
                  let pubkey = row["pubkey_hex"] as? String
            else { return nil }
            return RelayFriend(
                name: name,
                pubkeyHex: pubkey,
                relayURL: row["relay_url"] as? String,
                addedAt: row["added_at"] as? Double
            )
        }
    }

    // MARK: - Friend codes

    /// Generate a relay/E2EE friend code.
    func generateFriendCode(name: String) -> String? {
        guard !publicKeyHex.isEmpty,
              let seed = hexToData(privateKeyHex)
        else { return nil }

        let groupKey = HKDF<SHA256>.deriveKey(
            inputKeyMaterial: SymmetricKey(data: seed),
            salt: Data(),
            info: Data("fisherman/friends-group/v1".utf8),
            outputByteCount: 32
        )
        let groupHex = groupKey.withUnsafeBytes {
            Data($0).map { String(format: "%02x", $0) }.joined()
        }

        let relay = statusRelayURL.isEmpty ? nil : statusRelayURL
        let code = FriendCode(n: name, k: publicKeyHex, h: nil, w: nil, a: nil, g: groupHex, r: relay)
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
        guard code.g == nil else { return }
        let name = overrideName?.isEmpty == false ? overrideName! : code.n
        guard let host = code.h else { return }
        let wsURL = "ws://\(host):\(code.w ?? 9999)"
        let actPort = String(code.a ?? 9998)

        addFriend(name: name, publicKey: code.k, serverURL: wsURL, activityPort: actPort)
        registerFriendOnServer(pubkey: code.k)
    }

    /// POST friend pubkey to own server's /api/friends (owner auth).
    func registerFriendOnServer(pubkey: String) {
        guard let authValue = signRequest() else { return }
        Self.mutateFriendOnServer(
            method: "POST",
            pubkey: pubkey,
            authValue: authValue,
            bases: ownHTTPBaseURLCandidates(),
            index: 0
        )
    }

    /// DELETE friend pubkey from own server's /api/friends.
    private func deregisterFriendOnServer(pubkey: String) {
        guard let authValue = signRequest() else { return }
        Self.mutateFriendOnServer(
            method: "DELETE",
            pubkey: pubkey,
            authValue: authValue,
            bases: ownHTTPBaseURLCandidates(),
            index: 0
        )
    }

    private static func mutateFriendOnServer(
        method: String,
        pubkey: String,
        authValue: String,
        bases: [String],
        index: Int
    ) {
        guard index < bases.count,
              let url = URL(string: "\(bases[index])/api/friends")
        else {
            NSLog("[Fisherman] \(method) friend failed on all activity endpoints")
            return
        }

        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(authValue, forHTTPHeaderField: "Authorization")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["pubkey": pubkey])

        URLSession.shared.dataTask(with: request) { _, response, error in
            if let error {
                NSLog("[Fisherman] \(method) friend failed at \(bases[index]): \(error)")
                Self.mutateFriendOnServer(
                    method: method,
                    pubkey: pubkey,
                    authValue: authValue,
                    bases: bases,
                    index: index + 1
                )
                return
            }
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            if (200..<300).contains(status) {
                NSLog("[Fisherman] \(method) friend response: \(status) at \(bases[index])")
            } else {
                NSLog("[Fisherman] \(method) friend response: \(status) at \(bases[index])")
                Self.mutateFriendOnServer(
                    method: method,
                    pubkey: pubkey,
                    authValue: authValue,
                    bases: bases,
                    index: index + 1
                )
            }
        }.resume()
    }

    // MARK: - Private helpers

    private func normalizeBackendURLs(loadedKeys: Set<String>, needsSave: inout Bool) {
        if backendMode == "self_hosted" {
            if backendURL.isEmpty {
                backendURL = serverURL
                needsSave = true
            }
            if !backendURL.isEmpty
                && (!loadedKeys.contains("FISH_SERVER_URL") || serverURL == defaultServerURL)
            {
                serverURL = Self.ingestURL(from: backendURL)
            }
        } else if backendMode == "cloud" {
            if backendURL.isEmpty {
                backendURL = "https://fisherman.teleport.computer"
                needsSave = true
            }
            if backendURL.hasPrefix("ws://") || backendURL.hasPrefix("wss://") {
                serverURL = Self.ingestURL(from: backendURL)
            } else {
                serverURL = defaultServerURL
            }
        }
    }

    private static func ingestURL(from raw: String) -> String {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard var components = URLComponents(string: trimmed),
              let scheme = components.scheme
        else { return trimmed }

        if scheme == "http" {
            components.scheme = "ws"
        } else if scheme == "https" {
            components.scheme = "wss"
        } else if scheme != "ws" && scheme != "wss" {
            return trimmed
        }

        if components.path.isEmpty || components.path == "/" {
            components.path = "/ingest"
        }
        return components.string ?? trimmed
    }

    private func appendUniquePort(_ raw: String, to ports: inout [String]) {
        let port = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !port.isEmpty, Int(port) != nil, !ports.contains(port) else { return }
        ports.append(port)
    }

    private func generateKeyPair() {
        let key = Curve25519.Signing.PrivateKey()
        privateKeyHex = key.rawRepresentation.map { String(format: "%02x", $0) }.joined()
        publicKeyHex = key.publicKey.rawRepresentation.map { String(format: "%02x", $0) }.joined()
        NSLog("[Fisherman] generated new ed25519 key pair, pubkey: \(publicKeyHex)")
    }

    private func parseFriends(_ value: String) -> [Friend] {
        guard !value.isEmpty else { return [] }
        return value.split(separator: ",").compactMap { entry in
            let parts = entry.split(separator: "|", maxSplits: 4)
            guard parts.count >= 4 else { return nil }
            let tier = parts.count >= 5 ? (SharingTier(rawValue: String(parts[4])) ?? .high) : .high
            return Friend(
                name: String(parts[0]),
                publicKey: String(parts[1]),
                serverURL: String(parts[2]),
                activityPort: String(parts[3]),
                sharingTier: tier
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
