import CryptoKit
import Foundation
import Observation

struct RelayFriend: Sendable {
    let name: String
    let pubkeyHex: String
    let encryptionPubkeyHex: String
    let relayURL: String?
    let audience: String
    let policyPrompt: String?
    let addedAt: Double?
}

struct FriendCode: Codable {
    let v: Int     // friend-code version
    let n: String  // display name
    let k: String  // signing pubkey hex
    let x: String  // X25519 encryption pubkey hex
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
    var statusLLMMode: String = "managed"
    var statusLLMBaseURL: String = "https://openrouter.ai/api/v1"
    var statusLLMModel: String = "openai/gpt-4o-mini"

    // Ed25519 key pair (hex-encoded)
    var privateKeyHex: String = ""
    var publicKeyHex: String = ""

    // Display name for friend codes
    var displayName: String = NSFullUserName().components(separatedBy: " ").first ?? NSUserName()

    // Relay/E2EE friends
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
            } else if let value = extractValue(trimmed, key: "FISH_STATUS_LLM_MODE") {
                if shouldLoad("FISH_STATUS_LLM_MODE", fillMissingOnly: fillMissingOnly, loadedKeys: loadedKeys) {
                    statusLLMMode = value
                    loadedKeys.insert("FISH_STATUS_LLM_MODE")
                    loadedAnyManagedValue = true
                }
                if trackLines { knownKeyLines["FISH_STATUS_LLM_MODE", default: []].append(i) }
            } else if let value = extractValue(trimmed, key: "FISH_STATUS_LLM_BASE_URL") {
                if shouldLoad("FISH_STATUS_LLM_BASE_URL", fillMissingOnly: fillMissingOnly, loadedKeys: loadedKeys) {
                    statusLLMBaseURL = value
                    loadedKeys.insert("FISH_STATUS_LLM_BASE_URL")
                    loadedAnyManagedValue = true
                }
                if trackLines { knownKeyLines["FISH_STATUS_LLM_BASE_URL", default: []].append(i) }
            } else if let value = extractValue(trimmed, key: "FISH_STATUS_LLM_MODEL") {
                if shouldLoad("FISH_STATUS_LLM_MODEL", fillMissingOnly: fillMissingOnly, loadedKeys: loadedKeys) {
                    statusLLMModel = value
                    loadedKeys.insert("FISH_STATUS_LLM_MODEL")
                    loadedAnyManagedValue = true
                }
                if trackLines { knownKeyLines["FISH_STATUS_LLM_MODEL", default: []].append(i) }
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

        var updates: [(String, String)] = [
            ("FISH_BACKEND_MODE", backendMode),
            ("FISH_BACKEND_URL", backendURL),
            ("FISH_STATUS_RELAY_URL", statusRelayURL),
            ("FISH_CONTROL_PORT", controlPort),
            ("FISH_STATUS_LLM_MODE", statusLLMMode),
            ("FISH_STATUS_LLM_BASE_URL", statusLLMBaseURL),
            ("FISH_STATUS_LLM_MODEL", statusLLMModel),
            ("FISH_PRIVATE_KEY", privateKeyHex),
            ("FISH_DISPLAY_NAME", displayName),
        ]
        let persistServerURL = (
            backendMode == "self_hosted"
            || (backendMode == "cloud"
                && serverURL.hasPrefix("ws")
                && serverURL != defaultServerURL)
        )
        if persistServerURL {
            updates.append(("FISH_SERVER_URL", serverURL))
        }

        // Track which line indices to delete (duplicates of any tracked key)
        var indicesToDelete = Set<Int>()
        for key in persistServerURL ? [] : ["FISH_SERVER_URL"] {
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
        // Fisherman Cloud exposes /api/* through the public HTTPS gateway.
        // Self-hosted installs still commonly expose activity on a side port.
        if backendMode == "cloud" {
            appendUniquePort("", to: &ports)
        }
        appendUniquePort(activityPort, to: &ports)
        // Historical self-hosted installs used either 9998 (repo default) or
        // 9996 (current EC2/systemd setup). Try both so settings can expose a
        // single backend URL instead of a second "activity port" knob.
        appendUniquePort("9998", to: &ports)
        appendUniquePort("9996", to: &ports)
        return ports
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
                  let pubkey = row["pubkey_hex"] as? String,
                  let encryptionPubkey = row["encryption_pubkey"] as? String
            else { return nil }
            return RelayFriend(
                name: name,
                pubkeyHex: pubkey,
                encryptionPubkeyHex: encryptionPubkey,
                relayURL: row["relay_url"] as? String,
                audience: row["audience"] as? String ?? "friends",
                policyPrompt: row["policy_prompt"] as? String,
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

        let xSeed = HKDF<SHA256>.deriveKey(
            inputKeyMaterial: SymmetricKey(data: seed),
            salt: Data(),
            info: Data("fisherman/x25519/v1".utf8),
            outputByteCount: 32
        )
        let xSeedData = xSeed.withUnsafeBytes { Data($0) }
        guard let xPriv = try? Curve25519.KeyAgreement.PrivateKey(rawRepresentation: xSeedData) else {
            return nil
        }
        let xPubHex = xPriv.publicKey.rawRepresentation.map {
            String(format: "%02x", $0)
        }.joined()

        let relay = statusRelayURL.isEmpty ? nil : statusRelayURL
        let code = FriendCode(v: 2, n: name, k: publicKeyHex, x: xPubHex, r: relay)
        guard let jsonData = try? JSONEncoder().encode(code) else { return nil }

        let base64 = jsonData.base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")

        return "fish:\(base64)"
    }

    func encryptionPublicKeyHex() -> String? {
        guard let seed = hexToData(privateKeyHex) else { return nil }
        let xSeed = HKDF<SHA256>.deriveKey(
            inputKeyMaterial: SymmetricKey(data: seed),
            salt: Data(),
            info: Data("fisherman/x25519/v1".utf8),
            outputByteCount: 32
        )
        let xSeedData = xSeed.withUnsafeBytes { Data($0) }
        guard let xPriv = try? Curve25519.KeyAgreement.PrivateKey(rawRepresentation: xSeedData) else {
            return nil
        }
        return xPriv.publicKey.rawRepresentation.map {
            String(format: "%02x", $0)
        }.joined()
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
                let expected = Self.ingestURL(from: backendURL)
                if loadedKeys.contains("FISH_SERVER_URL") && serverURL == expected {
                    serverURL = expected
                } else {
                    serverURL = defaultServerURL
                }
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
        if port.isEmpty {
            if !ports.contains(port) { ports.append(port) }
            return
        }
        guard Int(port) != nil, !ports.contains(port) else { return }
        ports.append(port)
    }

    private func generateKeyPair() {
        let key = Curve25519.Signing.PrivateKey()
        privateKeyHex = key.rawRepresentation.map { String(format: "%02x", $0) }.joined()
        publicKeyHex = key.publicKey.rawRepresentation.map { String(format: "%02x", $0) }.joined()
        NSLog("[Fisherman] generated new ed25519 key pair, pubkey: \(publicKeyHex)")
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
