import CryptoKit
import Foundation
import Observation

struct Friend: Sendable {
    let name: String
    let publicKey: String      // hex
    let serverURL: String      // e.g. "ws://1.2.3.4:9999"
    let activityPort: String   // e.g. "9998"
}

@Observable
final class ConfigManager {
    var serverURL: String = "ws://localhost:9999/ingest"
    var controlPort: String = "7892"
    var activityPort: String = "9998"

    // Ed25519 key pair (hex-encoded)
    var privateKeyHex: String = ""
    var publicKeyHex: String = ""

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
        friends.remove(at: index)
        save()
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
