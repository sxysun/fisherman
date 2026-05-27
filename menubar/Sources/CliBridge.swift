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

        // Drain pipes in background threads while the process runs.
        // Without concurrent draining, large outputs (> pipe buffer ~64KB) cause the
        // subprocess to block on write(), making proc.isRunning stay true until timeout.
        final class DataBox: @unchecked Sendable { var data = Data() }
        let outBox = DataBox()
        let errBox = DataBox()
        let readGroup = DispatchGroup()
        readGroup.enter()
        DispatchQueue.global(qos: .background).async {
            outBox.data = outPipe.fileHandleForReading.readDataToEndOfFile()
            readGroup.leave()
        }
        readGroup.enter()
        DispatchQueue.global(qos: .background).async {
            errBox.data = errPipe.fileHandleForReading.readDataToEndOfFile()
            readGroup.leave()
        }

        if readGroup.wait(timeout: .now() + timeout) == .timedOut {
            proc.terminate()
            _ = readGroup.wait(timeout: .now() + 2.0)
            return Result(exitCode: -2,
                          stdout: String(data: outBox.data, encoding: .utf8) ?? "",
                          stderr: "timeout")
        }
        proc.waitUntilExit()
        return Result(exitCode: proc.terminationStatus,
                      stdout: String(data: outBox.data, encoding: .utf8) ?? "",
                      stderr: String(data: errBox.data, encoding: .utf8) ?? "")
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
