import Foundation

final class ProcessManager: @unchecked Sendable {
    private var screenpipeProcess: Process?
    private var fishermanProcess: Process?
    private let controlPort: String
    private let restartDelay: TimeInterval = 3.0
    private var stopped = false
    private var cleanupTimer: Timer?
    private var watchdogTimer: Timer?
    private var consecutiveHealthFailures: Int = 0
    private var fishermanStartedAt: Date?
    private let screenpipeDataDir = NSHomeDirectory() + "/.fisherman/screenpipe-data"

    // Watchdog: if /status on the daemon's control port times out this many
    // consecutive times, we assume the asyncio loop is wedged and SIGKILL
    // the process so FishermanMenu's termination handler respawns it.
    private let watchdogIntervalSec: TimeInterval = 5.0
    private let watchdogTimeoutSec: TimeInterval = 2.0
    private let watchdogFailureThreshold: Int = 4
    // Grace period after launch before the watchdog starts. The daemon
    // imports pyobjc + starts screenpipe polling, so it can legitimately
    // take ~15s to answer /status the first time.
    private let watchdogStartupGraceSec: TimeInterval = 25.0

    init(controlPort: String) {
        self.controlPort = controlPort
    }

    // MARK: - Start

    func startAll() {
        stopped = false
        startScreenpipe()
        startCleanupTimer()
        startWatchdog()
        // Delay fisherman launch to let screenpipe bind its port
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 2.0) { [weak self] in
            self?.startFisherman()
        }
    }

    // MARK: - Screenpipe

    private func startScreenpipe() {
        // Skip if screenpipe is already running (e.g. from terminal)
        if isScreenpipeAlive() {
            NSLog("[Fisherman] screenpipe already running on :3030, adopting")
            return
        }

        guard let binary = findScreenpipe() else {
            NSLog("[Fisherman] screenpipe binary not found")
            return
        }

        // Keep screenpipe data under our own directory, not ~/.screenpipe
        let fm = FileManager.default
        if !fm.fileExists(atPath: screenpipeDataDir) {
            try? fm.createDirectory(atPath: screenpipeDataDir, withIntermediateDirectories: true)
        }

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: binary)
        proc.arguments = [
            "--fps", "1",
            "--disable-audio",
            "--disable-telemetry",
            "--use-pii-removal",
            "--data-dir", screenpipeDataDir,
            "--auto-destruct-pid", "\(ProcessInfo.processInfo.processIdentifier)",
        ]
        proc.environment = buildEnvironment()

        let log = logFileHandle(name: "screenpipe")
        proc.standardOutput = log
        proc.standardError = log

        proc.terminationHandler = { [weak self] process in
            guard let self, !self.stopped else { return }
            NSLog("[Fisherman] screenpipe exited with code \(process.terminationStatus), restarting in \(self.restartDelay)s")
            DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + self.restartDelay) { [weak self] in
                self?.startScreenpipe()
            }
        }

        do {
            try proc.run()
            screenpipeProcess = proc
            NSLog("[Fisherman] launched screenpipe pid=\(proc.processIdentifier)")
        } catch {
            NSLog("[Fisherman] failed to launch screenpipe: \(error)")
        }
    }

    // MARK: - Fisherman daemon

    private func startFisherman() {
        // Skip if fisherman is already running
        let port = Int(controlPort) ?? 7892
        if isFishermanAlive(port: port) {
            NSLog("[Fisherman] fisherman daemon already running on :\(port), adopting")
            fishermanStartedAt = Date()
            return
        }

        let projectDir = findProjectDir()
        NSLog("[Fisherman] using project dir: \(projectDir)")

        // Prefer launching directly via the project's .venv python. Launching
        // via `uv run` re-syncs deps on every launch and has wedged the
        // daemon mid-import (pyobjc) more than once — the process is alive
        // but hung forever, which FishermanMenu can't detect through exit
        // status. Only fall back to `uv run` if the venv is missing (fresh
        // install), in which case we still want a working daemon.
        let proc = Process()
        if let venvPython = findVenvPython(projectDir: projectDir) {
            proc.executableURL = URL(fileURLWithPath: venvPython)
            proc.arguments = ["-m", "fisherman", "start"]
            NSLog("[Fisherman] launching daemon via venv python: \(venvPython)")
        } else if let uvPath = findUV() {
            NSLog("[Fisherman] .venv missing — falling back to `uv run` (will sync first)")
            proc.executableURL = URL(fileURLWithPath: uvPath)
            proc.arguments = ["run", "python", "-m", "fisherman", "start"]
        } else {
            NSLog("[Fisherman] no .venv and no uv — cannot start daemon")
            return
        }
        proc.currentDirectoryURL = URL(fileURLWithPath: projectDir)

        var env = buildEnvironment()
        env["FISH_CAPTURE_BACKEND"] = "screenpipe"
        env["FISH_SCREENPIPE_POLL_INTERVAL"] = "3"
        proc.environment = env

        let log = logFileHandle(name: "fisherman")
        proc.standardOutput = log
        proc.standardError = log

        proc.terminationHandler = { [weak self] process in
            guard let self, !self.stopped else { return }
            NSLog("[Fisherman] fisherman daemon exited with code \(process.terminationStatus), restarting in \(self.restartDelay)s")
            DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + self.restartDelay) { [weak self] in
                self?.startFisherman()
            }
        }

        do {
            try proc.run()
            fishermanProcess = proc
            fishermanStartedAt = Date()
            consecutiveHealthFailures = 0
            NSLog("[Fisherman] launched fisherman daemon pid=\(proc.processIdentifier)")
        } catch {
            NSLog("[Fisherman] failed to launch fisherman daemon: \(error)")
        }
    }

    // MARK: - Restart fisherman

    func restartFisherman() {
        NSLog("[Fisherman] restarting fisherman daemon for config change")
        terminate(fishermanProcess)
        fishermanProcess = nil
        fishermanStartedAt = nil
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 1.0) { [weak self] in
            self?.startFisherman()
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
        // Startup grace: pyobjc + screenpipe handshake can take ~15s.
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

        if let proc = fishermanProcess, proc.isRunning {
            // SIGKILL directly — .terminate() sends SIGTERM which a wedged
            // Python process stuck in a C import may never handle.
            kill(proc.processIdentifier, SIGKILL)
        }
        // Also kill any orphaned `uv run` wrapper or stray python from prior
        // launches that might still hold the control port.
        _ = runShell("/usr/bin/pkill", ["-KILL", "-f", "uv run python -m fisherman"])
        _ = runShell("/usr/bin/pkill", ["-KILL", "-f", "python.*-m fisherman start"])

        fishermanProcess = nil
        // terminationHandler on the old Process will fire and respawn; but
        // if we SIGKILLed an adopted process (no handler), schedule respawn.
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + restartDelay) { [weak self] in
            guard let self, !self.stopped, self.fishermanProcess == nil else { return }
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
        cleanupTimer?.invalidate()
        cleanupTimer = nil
        watchdogTimer?.invalidate()
        watchdogTimer = nil
        terminate(screenpipeProcess)
        terminate(fishermanProcess)
        screenpipeProcess = nil
        fishermanProcess = nil
        fishermanStartedAt = nil
    }

    private func terminate(_ process: Process?) {
        guard let process, process.isRunning else { return }
        process.terminate()
        // Wait up to 2s, then SIGKILL
        let deadline = Date().addingTimeInterval(2.0)
        while process.isRunning && Date() < deadline {
            Thread.sleep(forTimeInterval: 0.1)
        }
        if process.isRunning {
            kill(process.processIdentifier, SIGKILL)
        }
    }

    // MARK: - Screenpipe data cleanup

    /// Screenpipe saves MP4 chunks + SQLite DB even though we only need the
    /// live OCR API. This timer deletes video files older than 5 minutes and
    /// caps the SQLite DB by deleting old data periodically.
    private func startCleanupTimer() {
        // Run every 60 seconds on main run loop
        DispatchQueue.main.async { [weak self] in
            self?.cleanupTimer = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in
                self?.cleanupScreenpipeData()
            }
        }
    }

    private func cleanupScreenpipeData() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            let dataDir = self.screenpipeDataDir + "/data"
            let fm = FileManager.default

            guard let files = try? fm.contentsOfDirectory(atPath: dataDir) else { return }
            let cutoff = Date().addingTimeInterval(-900) // 15 minutes ago

            var deleted = 0
            for file in files where file.hasSuffix(".mp4") {
                let path = dataDir + "/" + file
                guard let attrs = try? fm.attributesOfItem(atPath: path),
                      let modified = attrs[.modificationDate] as? Date,
                      modified < cutoff
                else { continue }

                try? fm.removeItem(atPath: path)
                deleted += 1
            }

            if deleted > 0 {
                NSLog("[Fisherman] cleaned up \(deleted) old screenpipe video files")
            }
        }
    }
}
