import Foundation

final class ProcessManager: @unchecked Sendable {
    private var fishermanPID: pid_t?
    private let controlPort: String
    private let restartDelay: TimeInterval = 3.0
    private var stopped = false
    private var watchdogTimer: Timer?
    private var consecutiveHealthFailures: Int = 0
    private var fishermanStartedAt: Date?

    // Watchdog: if /status on the daemon's control port times out this many
    // consecutive times, we assume the asyncio loop is wedged and SIGKILL
    // the process so FishermanMenu's termination handler respawns it.
    private let watchdogIntervalSec: TimeInterval = 5.0
    private let watchdogTimeoutSec: TimeInterval = 2.0
    private let watchdogFailureThreshold: Int = 4
    // Grace period after launch before the watchdog starts. The daemon
    // imports pyobjc and may initialize native capture, so it can
    // legitimately take a few seconds to answer /status the first time.
    private let watchdogStartupGraceSec: TimeInterval = 25.0

    init(controlPort: String) {
        self.controlPort = controlPort
    }

    // MARK: - Start

    func startAll() {
        stopped = false
        startWatchdog()
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 0.2) { [weak self] in
            self?.startFisherman()
        }
    }

    // MARK: - Fisherman daemon

    private func startFisherman() {
        if let pid = fishermanPID, kill(pid, 0) == 0 {
            NSLog("[Fisherman] fisherman daemon process already running; waiting for /status")
            return
        }

        // Reaching here means we have no live child of our own. If a daemon is
        // still answering on the port, it's an ORPHAN — left behind when a
        // prior app instance was force-killed without taking its daemon down
        // (notably the "Update Fisherman" flow, which pkills the old menubar).
        // An orphan was reparented to launchd and is NOT our child, so it can't
        // inherit our Screen Recording TCC grant. Adopting it reproduces the
        // endless "would like to record the screen" prompt. Kill it and spawn a
        // fresh child instead so capture inherits the app's grant.
        let port = Int(controlPort) ?? 7892
        if isFishermanAlive(port: port) {
            NSLog("[Fisherman] orphan daemon on :\(port) — replacing with our own child so it inherits the app's Screen Recording grant")
            _ = runShell("/usr/bin/pkill", ["-9", "-f", "-m fisherman start"])
            _ = runShell("/usr/bin/pkill", ["-9", "-f", "uv run python -m fisherman"])
            // Let the control port free up before we bind a fresh daemon.
            Thread.sleep(forTimeInterval: 1.5)
        }

        let projectDir = findProjectDir()
        NSLog("[Fisherman] using project dir: \(projectDir)")

        // Prefer launching directly via the project's .venv python. Launching
        // via `uv run` re-syncs deps on every launch and has wedged the
        // daemon mid-import (pyobjc) more than once — the process is alive
        // but hung forever, which FishermanMenu can't detect through exit
        // status. Only fall back to `uv run` if the venv is missing (fresh
        // install), in which case we still want a working daemon.
        let executable: String
        let arguments: [String]
        if let venvPython = findVenvPython(projectDir: projectDir) {
            executable = venvPython
            arguments = ["-m", "fisherman", "start"]
            NSLog("[Fisherman] launching daemon via venv python: \(venvPython)")
        } else if let uvPath = findUV() {
            NSLog("[Fisherman] .venv missing — falling back to `uv run` (will sync first)")
            executable = uvPath
            arguments = ["run", "python", "-m", "fisherman", "start"]
        } else {
            NSLog("[Fisherman] no .venv and no uv — cannot start daemon")
            return
        }

        var env = buildEnvironment()
        let backend = configuredCaptureBackend()
        env["FISH_CAPTURE_BACKEND"] = backend
        env["FISHERMAN_FORCE_SCREENCAPTURE"] = readEnvValue("FISHERMAN_FORCE_SCREENCAPTURE") ?? "1"

        guard let pid = spawnDaemon(
            executable: executable,
            arguments: arguments,
            environment: env,
            cwd: projectDir,
            logPath: logFilePath(name: "fisherman")
        ) else {
            NSLog("[Fisherman] failed to launch fisherman daemon")
            return
        }

        fishermanPID = pid
        fishermanStartedAt = Date()
        consecutiveHealthFailures = 0
        NSLog("[Fisherman] launched fisherman daemon pid=\(pid)")
        watchForExit(pid: pid)
    }

    /// posix_spawn the daemon as a direct child, redirecting stdio to the log
    /// and chdir'ing into the project dir. The child stays a direct child
    /// (ppid == us) so `waitpid`/`kill` drive its lifecycle. TCC attribution
    /// follows the default responsibility chain (same as NSTask).
    private func spawnDaemon(
        executable: String,
        arguments: [String],
        environment: [String: String],
        cwd: String,
        logPath: String
    ) -> pid_t? {
        var attr: posix_spawnattr_t?
        posix_spawnattr_init(&attr)
        defer { posix_spawnattr_destroy(&attr) }

        var actions: posix_spawn_file_actions_t?
        posix_spawn_file_actions_init(&actions)
        defer { posix_spawn_file_actions_destroy(&actions) }
        // stdout + stderr → daemon log; cwd → project dir.
        posix_spawn_file_actions_addopen(&actions, 1, logPath, O_WRONLY | O_CREAT | O_APPEND, 0o644)
        posix_spawn_file_actions_adddup2(&actions, 1, 2)
        posix_spawn_file_actions_addchdir_np(&actions, cwd)

        let argv: [UnsafeMutablePointer<CChar>?] =
            ([executable] + arguments).map { strdup($0) } + [nil]
        let envp: [UnsafeMutablePointer<CChar>?] =
            environment.map { strdup("\($0.key)=\($0.value)") } + [nil]
        defer {
            for ptr in argv where ptr != nil { free(ptr) }
            for ptr in envp where ptr != nil { free(ptr) }
        }

        var pid: pid_t = 0
        let rc = posix_spawn(&pid, executable, &actions, &attr, argv, envp)
        guard rc == 0 else {
            NSLog("[Fisherman] posix_spawn(disclaimed) failed rc=\(rc) errno=\(errno)")
            return nil
        }
        return pid
    }

    /// Reap the daemon and respawn it on unexpected exit. Replaces
    /// Process.terminationHandler now that we spawn the pid directly.
    private func watchForExit(pid: pid_t) {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            var status: Int32 = 0
            _ = waitpid(pid, &status, 0)
            DispatchQueue.main.async {
                guard let self, !self.stopped, self.fishermanPID == pid else { return }
                NSLog("[Fisherman] fisherman daemon pid=\(pid) exited (status=\(status)), restarting in \(self.restartDelay)s")
                self.fishermanPID = nil
                DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + self.restartDelay) { [weak self] in
                    self?.startFisherman()
                }
            }
        }
    }

    private func configuredCaptureBackend() -> String {
        let raw = readEnvValue("FISH_CAPTURE_BACKEND")?
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
        if let raw, raw == "native" || raw == "swift" {
            return raw
        }
        return "native"
    }

    // MARK: - Restart fisherman

    func restartFisherman() {
        NSLog("[Fisherman] restarting fisherman daemon for config change")
        terminate(fishermanPID)
        fishermanPID = nil
        fishermanStartedAt = nil
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 1.0) { [weak self] in
            self?.startFisherman()
        }
    }

    func repairCaptureStack() {
        let backend = configuredCaptureBackend()
        NSLog("[Fisherman] repairing capture stack for backend=\(backend)")
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self, !self.stopped else { return }
            self.restartFisherman()
        }
    }

    // MARK: - Watchdog (hang detection via /status)

    private func startWatchdog() {
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.watchdogTimer?.invalidate()
            self.watchdogTimer = Timer.scheduledTimer(
                withTimeInterval: self.watchdogIntervalSec,
                repeats: true
            ) { [weak self] _ in
                self?.watchdogTick()
            }
        }
    }

    private func watchdogTick() {
        guard !stopped, let startedAt = fishermanStartedAt else { return }
        // Startup grace: pyobjc import/capture setup can take a few seconds.
        if Date().timeIntervalSince(startedAt) < watchdogStartupGraceSec { return }

        let port = Int(controlPort) ?? 7892
        checkStatus(port: port) { [weak self] ok in
            guard let self, !self.stopped else { return }
            if ok {
                if self.consecutiveHealthFailures > 0 {
                    NSLog("[Fisherman] watchdog: /status responsive again (after \(self.consecutiveHealthFailures) misses)")
                }
                self.consecutiveHealthFailures = 0
                return
            }
            self.consecutiveHealthFailures += 1
            NSLog("[Fisherman] watchdog: /status no response (\(self.consecutiveHealthFailures)/\(self.watchdogFailureThreshold))")

            if self.consecutiveHealthFailures >= self.watchdogFailureThreshold {
                self.killHungDaemon()
            }
        }
    }

    private func checkStatus(port: Int, completion: @Sendable @escaping (Bool) -> Void) {
        guard let url = URL(string: "http://127.0.0.1:\(port)/status") else {
            completion(false); return
        }
        var request = URLRequest(url: url)
        request.timeoutInterval = watchdogTimeoutSec
        request.httpMethod = "GET"

        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = watchdogTimeoutSec
        config.timeoutIntervalForResource = watchdogTimeoutSec
        let session = URLSession(configuration: config)

        session.dataTask(with: request) { data, response, _ in
            guard let http = response as? HTTPURLResponse, http.statusCode == 200,
                  let data,
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  json["running"] != nil
            else { completion(false); return }
            completion(true)
        }.resume()
    }

    private func killHungDaemon() {
        NSLog("[Fisherman] watchdog: daemon wedged — SIGKILL + respawn")
        consecutiveHealthFailures = 0
        fishermanStartedAt = nil

        if let pid = fishermanPID, kill(pid, 0) == 0 {
            // SIGKILL directly — SIGTERM which a wedged Python process stuck
            // in a C import may never handle.
            kill(pid, SIGKILL)
        }
        // Also kill any orphaned `uv run` wrapper or stray python from prior
        // launches that might still hold the control port.
        _ = runShell("/usr/bin/pkill", ["-KILL", "-f", "uv run python -m fisherman"])
        _ = runShell("/usr/bin/pkill", ["-KILL", "-f", "python.*-m fisherman start"])

        fishermanPID = nil
        // The waitForExit reaper on the old pid will fire and respawn; but if
        // we SIGKILLed an adopted process (no reaper), schedule respawn here.
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + restartDelay) { [weak self] in
            guard let self, !self.stopped, self.fishermanPID == nil else { return }
            self.startFisherman()
        }
    }

    @discardableResult
    private func runShell(_ path: String, _ args: [String]) -> Int32 {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: path)
        p.arguments = args
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        do { try p.run(); p.waitUntilExit(); return p.terminationStatus }
        catch { return -1 }
    }

    // MARK: - Pause / Resume

    func togglePause(isPaused: Bool) {
        let endpoint = isPaused ? "resume" : "pause"
        guard let url = URL(string: "http://127.0.0.1:\(controlPort)/\(endpoint)") else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 2.0
        URLSession.shared.dataTask(with: request) { _, _, _ in }.resume()
    }

    // MARK: - Stop

    func stopAll() {
        stopped = true
        watchdogTimer?.invalidate()
        watchdogTimer = nil
        terminate(fishermanPID)
        fishermanPID = nil
        fishermanStartedAt = nil
    }

    private func terminate(_ pid: pid_t?) {
        guard let pid, kill(pid, 0) == 0 else { return }
        kill(pid, SIGTERM)
        // Wait up to 2s, then SIGKILL
        let deadline = Date().addingTimeInterval(2.0)
        while kill(pid, 0) == 0 && Date() < deadline {
            Thread.sleep(forTimeInterval: 0.1)
        }
        if kill(pid, 0) == 0 {
            kill(pid, SIGKILL)
        }
    }

}
