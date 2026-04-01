import Foundation
import Observation

@Observable
final class ConfigManager {
    var serverURL: String = "ws://localhost:9999/ingest"
    var authToken: String = ""
    var controlPort: String = "7892"

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
            } else if let value = extractValue(trimmed, key: "FISH_AUTH_TOKEN") {
                authToken = value
                knownKeyLines["FISH_AUTH_TOKEN"] = i
            } else if let value = extractValue(trimmed, key: "FISH_CONTROL_PORT") {
                controlPort = value
                knownKeyLines["FISH_CONTROL_PORT"] = i
            } else {
                passthroughLines.append((index: i, line: line))
            }
        }
    }

    func save() {
        // Read existing file to preserve structure
        let existingLines: [String]
        if let contents = try? String(contentsOfFile: envPath, encoding: .utf8) {
            existingLines = contents.components(separatedBy: "\n")
        } else {
            existingLines = []
        }

        var outputLines = existingLines
        var keysWritten = Set<String>()

        // Update known keys in-place
        let updates: [(String, String)] = [
            ("FISH_SERVER_URL", serverURL),
            ("FISH_AUTH_TOKEN", authToken),
            ("FISH_CONTROL_PORT", controlPort),
        ]

        for (key, value) in updates {
            if let lineIndex = knownKeyLines[key], lineIndex < outputLines.count {
                outputLines[lineIndex] = "\(key)=\(value)"
                keysWritten.insert(key)
            }
        }

        // Append any keys that weren't already in the file
        for (key, value) in updates where !keysWritten.contains(key) {
            // Don't append empty values for optional keys
            if key == "FISH_AUTH_TOKEN" && value.isEmpty { continue }
            outputLines.append("\(key)=\(value)")
        }

        // Remove trailing empty lines, then ensure single trailing newline
        while let last = outputLines.last, last.isEmpty {
            outputLines.removeLast()
        }

        let output = outputLines.joined(separator: "\n") + "\n"

        // Ensure directory exists
        let dir = (envPath as NSString).deletingLastPathComponent
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)

        try? output.write(toFile: envPath, atomically: true, encoding: .utf8)
        NSLog("[Fisherman] saved config to \(envPath)")
    }

    private func extractValue(_ line: String, key: String) -> String? {
        let prefix = "\(key)="
        guard line.hasPrefix(prefix) else { return nil }
        return String(line.dropFirst(prefix.count)).trimmingCharacters(in: .whitespaces)
    }
}
