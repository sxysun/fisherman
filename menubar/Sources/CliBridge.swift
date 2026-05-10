import Foundation

/// Lightweight wrapper around the bundled `fisherman` CLI. Used by the
/// advanced settings tabs.
///
/// Resolution order for the CLI:
///   1. /usr/local/bin/fisherman   (created by the symlink installer)
///   2. ~/.fisherman/.venv/bin/fisherman
///   3. fall back to `python -m fisherman` against the project's venv
enum CliBridge {
    struct Result {
        let exitCode: Int32
        let stdout: String
        let stderr: String
    }

    static func fishermanPath() -> String? {
        let candidates = [
            "/usr/local/bin/fisherman",
            (NSHomeDirectory() as NSString).appendingPathComponent(".fisherman/.venv/bin/fisherman"),
        ]
        for c in candidates where FileManager.default.isExecutableFile(atPath: c) {
            return c
        }
        return nil
    }

    /// Run `fisherman <args>` synchronously, returning stdout/stderr.
    /// Falls back to `python -m fisherman` if the entry-point binary is missing.
    static func run(_ args: [String], timeout: TimeInterval = 30) -> Result {
        let proc = Process()
        if let path = fishermanPath() {
            proc.executableURL = URL(fileURLWithPath: path)
            proc.arguments = args
        } else {
            // Fallback: use the dev venv if we can find it
            let projDir = findProjectDir()
            let venvPython = projDir + "/.venv/bin/python"
            if FileManager.default.isExecutableFile(atPath: venvPython) {
                proc.executableURL = URL(fileURLWithPath: venvPython)
                proc.arguments = ["-m", "fisherman"] + args
                var env = ProcessInfo.processInfo.environment
                env["PYTHONPATH"] = projDir
                proc.environment = env
            } else {
                return Result(exitCode: -1, stdout: "", stderr: "fisherman CLI not found")
            }
        }

        let outPipe = Pipe()
        let errPipe = Pipe()
        proc.standardOutput = outPipe
        proc.standardError = errPipe

        do { try proc.run() } catch {
            return Result(exitCode: -1, stdout: "", stderr: "spawn failed: \(error)")
        }

        let deadline = Date().addingTimeInterval(timeout)
        while proc.isRunning && Date() < deadline {
            Thread.sleep(forTimeInterval: 0.05)
        }
        if proc.isRunning {
            proc.terminate()
            return Result(exitCode: -2, stdout: "", stderr: "timeout")
        }

        let stdout = String(data: outPipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        let stderr = String(data: errPipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        return Result(exitCode: proc.terminationStatus, stdout: stdout, stderr: stderr)
    }

    /// Decode `--json` output as `[[String: Any]]`. Returns nil on failure.
    static func runJsonArray(_ args: [String], timeout: TimeInterval = 30) -> [[String: Any]]? {
        let r = run(args, timeout: timeout)
        guard r.exitCode == 0,
              let data = r.stdout.data(using: .utf8),
              let arr = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]]
        else { return nil }
        return arr
    }

    /// Decode `--json` output as `[String: Any]`. Returns nil on failure.
    static func runJsonObject(_ args: [String], timeout: TimeInterval = 30) -> [String: Any]? {
        let r = run(args, timeout: timeout)
        guard r.exitCode == 0,
              let data = r.stdout.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return nil }
        return obj
    }
}
