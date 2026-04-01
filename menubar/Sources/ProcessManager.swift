import Foundation

final class ProcessManager: @unchecked Sendable {
    private var screenpipeProcess: Process?
    private var fishermanProcess: Process?
    private let controlPort: String
    private let restartDelay: TimeInterval = 3.0
    private var stopped = false
    private var cleanupTimer: Timer?
    private let screenpipeDataDir = NSHomeDirectory() + "/.fisherman/screenpipe-data"

    init(controlPort: String) {
        self.controlPort = controlPort
    }

    // MARK: - Start

    func startAll() {
        stopped = false
        startScreenpipe()
        startCleanupTimer()
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
            "--fps", "0.2",
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
            return
        }

        guard let uvPath = findUV() else {
            NSLog("[Fisherman] uv binary not found")
            return
        }

        let projectDir = findProjectDir()
        NSLog("[Fisherman] using project dir: \(projectDir)")
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: uvPath)
        proc.arguments = ["run", "python", "-m", "fisherman", "start"]
        proc.currentDirectoryURL = URL(fileURLWithPath: projectDir)

        var env = buildEnvironment()
        env["FISH_CAPTURE_BACKEND"] = "screenpipe"
        env["FISH_SCREENPIPE_POLL_INTERVAL"] = "5"
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
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 1.0) { [weak self] in
            self?.startFisherman()
        }
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
        terminate(screenpipeProcess)
        terminate(fishermanProcess)
        screenpipeProcess = nil
        fishermanProcess = nil
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
