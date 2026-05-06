import Foundation

// MARK: - Find binaries

func findUV() -> String? {
    let home = NSHomeDirectory()
    for candidate in [
        "\(home)/.local/bin/uv",
        "\(home)/.cargo/bin/uv",
        "/usr/local/bin/uv",
        "/opt/homebrew/bin/uv",
    ] {
        if FileManager.default.isExecutableFile(atPath: candidate) {
            return candidate
        }
    }
    // Fallback: `which uv`
    return whichBinary("uv")
}

/// Return the project's own .venv python if it exists. Launching the daemon
/// via this path (instead of `uv run python ...`) avoids `uv sync` racing
/// pyobjc imports at startup — a hang we've hit multiple times where the
/// daemon would freeze forever mid-import because uv reinstalled the editable
/// package while the child was already importing pyobjc modules.
func findVenvPython(projectDir: String) -> String? {
    let path = projectDir + "/.venv/bin/python"
    return FileManager.default.isExecutableFile(atPath: path) ? path : nil
}

func findScreenpipe() -> String? {
    let home = NSHomeDirectory()
    for candidate in [
        "\(home)/.local/bin/screenpipe",
        "\(home)/.cargo/bin/screenpipe",
        "/usr/local/bin/screenpipe",
        "/opt/homebrew/bin/screenpipe",
        "/Applications/screenpipe.app/Contents/MacOS/screenpipe",
    ] {
        if FileManager.default.isExecutableFile(atPath: candidate) {
            return candidate
        }
    }
    return whichBinary("screenpipe")
}

private func whichBinary(_ name: String) -> String? {
    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: "/usr/bin/which")
    proc.arguments = [name]
    proc.environment = buildEnvironment()
    let pipe = Pipe()
    proc.standardOutput = pipe
    proc.standardError = FileHandle.nullDevice
    do {
        try proc.run()
        proc.waitUntilExit()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        let path = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return path.isEmpty ? nil : path
    } catch {
        return nil
    }
}

// MARK: - Project directory

func findProjectDir() -> String {
    let fm = FileManager.default

    func isValidProjectDir(_ path: String) -> Bool {
        return fm.fileExists(atPath: path + "/pyproject.toml")
            && fm.fileExists(atPath: path + "/fisherman/daemon.py")
    }

    // 1. Env var override (dev)
    if let envDir = ProcessInfo.processInfo.environment["FISHERMAN_PROJECT_DIR"],
       isValidProjectDir(envDir)
    {
        return envDir
    }

    // 2. Walk up from binary (dev — running from repo checkout)
    let bundlePath = Bundle.main.executablePath ?? ""
    var dir = URL(fileURLWithPath: bundlePath)
    for _ in 0..<6 {
        dir = dir.deletingLastPathComponent()
        let candidate = dir.path
        if isValidProjectDir(candidate) {
            return candidate
        }
    }

    // 3. Dev copy (common location)
    let devDir = NSHomeDirectory() + "/Desktop/suapp/fisherman"
    if isValidProjectDir(devDir) {
        return devDir
    }

    // 4. Installed location
    let installed = NSHomeDirectory() + "/.fisherman"
    if isValidProjectDir(installed) {
        return installed
    }

    return installed
}

// MARK: - .env parsing

func userEnvPath() -> String {
    NSHomeDirectory() + "/.fisherman/.env"
}

func legacyProjectEnvPath() -> String? {
    let legacy = findProjectDir() + "/.env"
    return legacy == userEnvPath() ? nil : legacy
}

func envFilePaths() -> [String] {
    var paths = [userEnvPath()]
    if let legacy = legacyProjectEnvPath() {
        paths.append(legacy)
    }
    return paths
}

func readEnvValue(_ key: String) -> String? {
    let prefix = "\(key)="
    for path in envFilePaths() {
        guard let contents = try? String(contentsOfFile: path, encoding: .utf8) else {
            continue
        }
        for line in contents.components(separatedBy: "\n") {
            var trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.hasPrefix("export ") {
                trimmed = String(trimmed.dropFirst("export ".count))
                    .trimmingCharacters(in: .whitespaces)
            }
            if trimmed.hasPrefix(prefix) {
                return String(trimmed.dropFirst(prefix.count))
                    .trimmingCharacters(in: .whitespaces)
            }
        }
    }
    return nil
}

func audioEnabled() -> Bool {
    // Default true. Set FISH_AUDIO_ENABLED=0 in .env to disable.
    if let val = readEnvValue("FISH_AUDIO_ENABLED")?.lowercased() {
        if val == "0" || val == "false" || val == "no" || val == "off" {
            return false
        }
    }
    return true
}

func readControlPort() -> String {
    if let val = readEnvValue("FISH_CONTROL_PORT"), !val.isEmpty {
        return val
    }
    return "7892"
}

// MARK: - Environment for child processes

func buildEnvironment() -> [String: String] {
    let home = NSHomeDirectory()
    let extraPaths = [
        "\(home)/.local/bin",
        "\(home)/.cargo/bin",
        "/usr/local/bin",
        "/opt/homebrew/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    var env = ProcessInfo.processInfo.environment
    let existingPath = env["PATH"] ?? "/usr/bin:/bin"
    let newPath = (extraPaths + existingPath.components(separatedBy: ":"))
        .reduce(into: [String]()) { acc, p in if !acc.contains(p) { acc.append(p) } }
        .joined(separator: ":")
    env["PATH"] = newPath
    return env
}

// MARK: - Service health check

/// Check if the actual fisherman daemon is responding by verifying the JSON
/// body contains expected keys. A plain HTTP status check is not enough —
/// other apps (e.g. Lark) can occupy the same port with a local proxy that
/// returns 400 to everything.
func isFishermanAlive(port: Int) -> Bool {
    return isServiceAlive(port, path: "/status", requiredKey: "running")
}

func isScreenpipeAlive(port: Int = 3030) -> Bool {
    return isServiceAlive(port, path: "/health")
}

private final class ServiceProbeResult: @unchecked Sendable {
    private let lock = NSLock()
    private var value = false

    func markAlive() {
        lock.lock()
        value = true
        lock.unlock()
    }

    func isAlive() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return value
    }
}

private func isServiceAlive(_ port: Int, path: String, requiredKey: String? = nil) -> Bool {
    guard let url = URL(string: "http://127.0.0.1:\(port)\(path)") else { return false }
    var request = URLRequest(url: url)
    request.timeoutInterval = 1.0
    request.httpMethod = "GET"

    let semaphore = DispatchSemaphore(value: 0)
    let result = ServiceProbeResult()

    let config = URLSessionConfiguration.ephemeral
    config.timeoutIntervalForRequest = 1.0
    let session = URLSession(configuration: config)

    session.dataTask(with: request) { data, response, _ in
        guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
            semaphore.signal()
            return
        }
        if let key = requiredKey, let data {
            // Verify the response is actually from our service
            if let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               json[key] != nil
            {
                result.markAlive()
            }
        } else {
            result.markAlive()
        }
        semaphore.signal()
    }.resume()

    _ = semaphore.wait(timeout: .now() + 1.5)
    return result.isAlive()
}

// MARK: - Log file

func logFileHandle(name: String) -> FileHandle {
    let logDir = NSHomeDirectory() + "/.fisherman/logs"
    let fm = FileManager.default
    if !fm.fileExists(atPath: logDir) {
        try? fm.createDirectory(atPath: logDir, withIntermediateDirectories: true)
    }
    let logPath = logDir + "/\(name).log"
    if !fm.fileExists(atPath: logPath) {
        fm.createFile(atPath: logPath, contents: nil)
    }
    guard let handle = FileHandle(forWritingAtPath: logPath) else {
        return .nullDevice
    }
    handle.seekToEndOfFile()
    return handle
}
