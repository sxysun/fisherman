import AppKit
import Foundation

// MARK: - Settings Window Controller

class SettingsWindowController: NSObject, NSWindowDelegate {
    private var window: NSWindow?
    private let projectDir: String

    // Fields
    private var serverURLField: NSTextField!
    private var authTokenField: NSSecureTextField!
    private var captureIntervalField: NSTextField!
    private var jpegQualityField: NSTextField!
    private var maxDimensionField: NSTextField!
    private var controlPortField: NSTextField!

    private var onSave: (() -> Void)?

    init(projectDir: String, onSave: @escaping () -> Void) {
        self.projectDir = projectDir
        self.onSave = onSave
        super.init()
    }

    func showWindow() {
        if let existing = window, existing.isVisible {
            existing.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }

        let w = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 420, height: 340),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        w.title = "Fisherman Settings"
        w.center()
        w.delegate = self
        w.isReleasedWhenClosed = false

        let content = NSView(frame: w.contentView!.bounds)
        content.autoresizingMask = [.width, .height]
        w.contentView = content

        var y: CGFloat = 300

        func addLabel(_ text: String) {
            let label = NSTextField(labelWithString: text)
            label.frame = NSRect(x: 20, y: y, width: 160, height: 20)
            label.font = .systemFont(ofSize: 12, weight: .medium)
            content.addSubview(label)
        }

        func addField(placeholder: String) -> NSTextField {
            let field = NSTextField(frame: NSRect(x: 180, y: y, width: 220, height: 22))
            field.placeholderString = placeholder
            field.font = .systemFont(ofSize: 12)
            content.addSubview(field)
            y -= 36
            return field
        }

        func addSecureField(placeholder: String) -> NSSecureTextField {
            let field = NSSecureTextField(frame: NSRect(x: 180, y: y, width: 220, height: 22))
            field.placeholderString = placeholder
            field.font = .systemFont(ofSize: 12)
            content.addSubview(field)
            y -= 36
            return field
        }

        // Server URL
        addLabel("Server URL:")
        serverURLField = addField(placeholder: "ws://localhost:9999/ingest")

        // Auth Token
        addLabel("Auth Token:")
        authTokenField = addSecureField(placeholder: "optional")

        // Separator
        y -= 4
        let sep = NSBox(frame: NSRect(x: 20, y: y, width: 380, height: 1))
        sep.boxType = .separator
        content.addSubview(sep)
        y -= 16

        let advLabel = NSTextField(labelWithString: "Advanced")
        advLabel.frame = NSRect(x: 20, y: y, width: 100, height: 18)
        advLabel.font = .systemFont(ofSize: 11, weight: .semibold)
        advLabel.textColor = .secondaryLabelColor
        content.addSubview(advLabel)
        y -= 32

        // Capture Interval
        addLabel("Capture Interval (s):")
        captureIntervalField = addField(placeholder: "1.0")

        // JPEG Quality
        addLabel("JPEG Quality:")
        jpegQualityField = addField(placeholder: "60")

        // Max Dimension
        addLabel("Max Dimension:")
        maxDimensionField = addField(placeholder: "960")

        // Control Port
        addLabel("Control Port:")
        controlPortField = addField(placeholder: "7891")

        // Save button
        let saveButton = NSButton(title: "Save & Restart", target: self, action: #selector(saveSettings))
        saveButton.bezelStyle = .rounded
        saveButton.frame = NSRect(x: 290, y: 12, width: 110, height: 28)
        saveButton.keyEquivalent = "\r"
        content.addSubview(saveButton)

        // Load current values
        loadSettings()

        window = w
        w.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func envFilePath() -> String {
        return projectDir + "/.env"
    }

    private func loadSettings() {
        let path = envFilePath()
        guard let contents = try? String(contentsOfFile: path, encoding: .utf8) else { return }
        let env = parseEnv(contents)

        serverURLField.stringValue = env["FISH_SERVER_URL"] ?? ""
        authTokenField.stringValue = env["FISH_AUTH_TOKEN"] ?? ""
        captureIntervalField.stringValue = env["FISH_CAPTURE_INTERVAL"] ?? ""
        jpegQualityField.stringValue = env["FISH_JPEG_QUALITY"] ?? ""
        maxDimensionField.stringValue = env["FISH_MAX_DIMENSION"] ?? ""
        controlPortField.stringValue = env["FISH_CONTROL_PORT"] ?? ""
    }

    @objc private func saveSettings() {
        // Read existing env to preserve keys we don't edit
        let path = envFilePath()
        let existingContents = (try? String(contentsOfFile: path, encoding: .utf8)) ?? ""
        var env = parseEnv(existingContents)

        // Update with field values (only overwrite if non-empty)
        let fields: [(String, NSTextField)] = [
            ("FISH_SERVER_URL", serverURLField),
            ("FISH_AUTH_TOKEN", authTokenField),
            ("FISH_CAPTURE_INTERVAL", captureIntervalField),
            ("FISH_JPEG_QUALITY", jpegQualityField),
            ("FISH_MAX_DIMENSION", maxDimensionField),
            ("FISH_CONTROL_PORT", controlPortField),
        ]

        for (key, field) in fields {
            let val = field.stringValue.trimmingCharacters(in: .whitespaces)
            if val.isEmpty {
                env.removeValue(forKey: key)
            } else {
                env[key] = val
            }
        }

        // Write back
        var lines: [String] = []
        // Preserve comment lines from original
        for line in existingContents.components(separatedBy: "\n") {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.hasPrefix("#") || trimmed.isEmpty {
                lines.append(line)
            }
        }
        // Write all env keys (skip empty values)
        for (key, value) in env.sorted(by: { $0.key < $1.key }) where !value.isEmpty {
            lines.append("\(key)=\(value)")
        }

        let output = lines.joined(separator: "\n") + "\n"
        try? output.write(toFile: path, atomically: true, encoding: .utf8)

        window?.close()
        onSave?()
    }

    private func parseEnv(_ contents: String) -> [String: String] {
        var result: [String: String] = [:]
        for line in contents.components(separatedBy: "\n") {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.isEmpty || trimmed.hasPrefix("#") { continue }
            if let eqRange = trimmed.range(of: "=") {
                let key = String(trimmed[trimmed.startIndex..<eqRange.lowerBound])
                    .trimmingCharacters(in: .whitespaces)
                let value = String(trimmed[eqRange.upperBound...])
                    .trimmingCharacters(in: .whitespaces)
                result[key] = value
            }
        }
        return result
    }

    func windowWillClose(_ notification: Notification) {
        window = nil
    }
}

// MARK: - App Delegate

class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private var daemonProcess: Process?
    private var pollTimer: Timer?

    private var statusMenuItem: NSMenuItem!
    private var framesSentMenuItem: NSMenuItem!
    private var framesDroppedMenuItem: NSMenuItem!
    private var grantPermissionMenuItem: NSMenuItem!
    private var pauseResumeMenuItem: NSMenuItem!

    private var isPaused = false
    private var controlPort = "7891"
    private var controlURL: String { "http://127.0.0.1:\(controlPort)" }

    private var settingsController: SettingsWindowController?

    // MARK: Lifecycle

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)

        // Read control port from env
        let projDir = findProjectDir()
        let envPath = projDir + "/.env"
        if let contents = try? String(contentsOfFile: envPath, encoding: .utf8) {
            for line in contents.components(separatedBy: "\n") {
                let trimmed = line.trimmingCharacters(in: .whitespaces)
                if trimmed.hasPrefix("FISH_CONTROL_PORT=") {
                    controlPort = String(trimmed.dropFirst("FISH_CONTROL_PORT=".count))
                        .trimmingCharacters(in: .whitespaces)
                }
            }
        }

        // Status bar item
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        setIcon(color: .gray)

        // Build menu
        let menu = NSMenu()

        statusMenuItem = NSMenuItem(title: "Starting daemon...", action: nil, keyEquivalent: "")
        statusMenuItem.isEnabled = false
        menu.addItem(statusMenuItem)

        framesSentMenuItem = NSMenuItem(title: "Frames sent: --", action: nil, keyEquivalent: "")
        framesSentMenuItem.isEnabled = false
        menu.addItem(framesSentMenuItem)

        framesDroppedMenuItem = NSMenuItem(title: "Dropped: --", action: nil, keyEquivalent: "")
        framesDroppedMenuItem.isEnabled = false
        menu.addItem(framesDroppedMenuItem)

        grantPermissionMenuItem = NSMenuItem(title: "Grant Screen Recording...", action: #selector(openScreenRecordingSettings), keyEquivalent: "")
        grantPermissionMenuItem.target = self
        grantPermissionMenuItem.isHidden = true
        menu.addItem(grantPermissionMenuItem)

        menu.addItem(.separator())

        pauseResumeMenuItem = NSMenuItem(title: "Pause", action: #selector(togglePause), keyEquivalent: "p")
        pauseResumeMenuItem.target = self
        menu.addItem(pauseResumeMenuItem)

        let settingsItem = NSMenuItem(title: "Settings...", action: #selector(openSettings), keyEquivalent: ",")
        settingsItem.target = self
        menu.addItem(settingsItem)

        menu.addItem(.separator())

        let quitItem = NSMenuItem(title: "Quit Fisherman", action: #selector(quit), keyEquivalent: "q")
        quitItem.target = self
        menu.addItem(quitItem)

        statusItem.menu = menu

        // Launch daemon
        startDaemon()

        // Poll status every 2s
        pollTimer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
            self?.pollStatus()
        }
        // Fire immediately
        pollStatus()
    }

    func applicationWillTerminate(_ notification: Notification) {
        stopDaemon()
    }

    // MARK: Daemon management

    private func startDaemon() {
        guard let uvPath = findUV() else {
            statusMenuItem.title = "Error: uv not found"
            setIcon(color: .red)
            return
        }

        let projectDir = findProjectDir()

        let process = Process()
        process.executableURL = URL(fileURLWithPath: uvPath)
        process.arguments = ["run", "python", "-m", "fisherman", "start"]
        process.currentDirectoryURL = URL(fileURLWithPath: projectDir)

        // Log to ~/.fisherman/logs/daemon.log
        let logDir = NSHomeDirectory() + "/.fisherman/logs"
        let fm = FileManager.default
        if !fm.fileExists(atPath: logDir) {
            try? fm.createDirectory(atPath: logDir, withIntermediateDirectories: true)
        }
        let logPath = logDir + "/daemon.log"
        let logFile = FileHandle.forWritingOrCreate(at: logPath)
        process.standardOutput = logFile
        process.standardError = logFile

        // Build a robust PATH for when launched from Finder (.app)
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
        process.environment = env

        do {
            try process.run()
            daemonProcess = process
        } catch {
            statusMenuItem.title = "Error: \(error.localizedDescription)"
            setIcon(color: .red)
        }
    }

    private func stopDaemon() {
        guard let process = daemonProcess, process.isRunning else { return }
        process.terminate()
        // Wait up to 2s
        let deadline = Date().addingTimeInterval(2.0)
        while process.isRunning && Date() < deadline {
            Thread.sleep(forTimeInterval: 0.1)
        }
        if process.isRunning {
            // Force kill
            kill(process.processIdentifier, SIGKILL)
        }
        daemonProcess = nil
    }

    private func restartDaemon() {
        stopDaemon()

        // Re-read control port
        let projDir = findProjectDir()
        let envPath = projDir + "/.env"
        if let contents = try? String(contentsOfFile: envPath, encoding: .utf8) {
            for line in contents.components(separatedBy: "\n") {
                let trimmed = line.trimmingCharacters(in: .whitespaces)
                if trimmed.hasPrefix("FISH_CONTROL_PORT=") {
                    controlPort = String(trimmed.dropFirst("FISH_CONTROL_PORT=".count))
                        .trimmingCharacters(in: .whitespaces)
                }
            }
        }

        startDaemon()
    }

    private func findUV() -> String? {
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
        return nil
    }

    private func findProjectDir() -> String {
        // 1. Env var override (dev)
        if let envDir = ProcessInfo.processInfo.environment["FISHERMAN_PROJECT_DIR"],
           FileManager.default.fileExists(atPath: envDir + "/pyproject.toml") {
            return envDir
        }

        // 2. Installed location
        let installed = NSHomeDirectory() + "/.fisherman"
        if FileManager.default.fileExists(atPath: installed + "/pyproject.toml") {
            return installed
        }

        // 3. Walk up from binary (dev — running from repo checkout)
        // Binary could be at menubar/.build/release/FishermanMenu
        // or inside .app: Fisherman.app/Contents/MacOS/FishermanMenu
        let bundlePath = Bundle.main.executablePath ?? ""
        var dir = URL(fileURLWithPath: bundlePath)
        for _ in 0..<6 {
            dir = dir.deletingLastPathComponent()
            let candidate = dir.path
            if FileManager.default.fileExists(atPath: candidate + "/pyproject.toml") {
                return candidate
            }
        }

        // Fallback
        return NSHomeDirectory() + "/.fisherman"
    }

    // MARK: Status polling

    private func pollStatus() {
        guard let url = URL(string: "\(controlURL)/status") else { return }
        var request = URLRequest(url: url)
        request.timeoutInterval = 2.0

        URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            DispatchQueue.main.async {
                self?.handleStatusResponse(data: data, error: error)
            }
        }.resume()
    }

    private func handleStatusResponse(data: Data?, error: Error?) {
        guard let data, error == nil,
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            // Daemon not responding
            if let process = daemonProcess, !process.isRunning {
                statusMenuItem.title = "Daemon not running"
            } else {
                statusMenuItem.title = "Daemon starting..."
            }
            framesSentMenuItem.title = "Frames sent: --"
            framesDroppedMenuItem.title = "Dropped: --"
            setIcon(color: .red)
            return
        }

        let paused = json["paused"] as? Bool ?? false
        let connected = json["connected"] as? Bool ?? false
        let framesSent = json["frames_sent"] as? Int ?? 0
        let framesDropped = json["frames_dropped"] as? Int ?? 0
        let errorStr = json["error"] as? String

        isPaused = paused
        framesSentMenuItem.title = "Frames sent: \(framesSent)"
        framesDroppedMenuItem.title = "Dropped: \(framesDropped)"

        if let error = errorStr, error == "screen_recording_not_granted" {
            statusMenuItem.title = "No screen recording permission"
            grantPermissionMenuItem.isHidden = false
            setIcon(color: .systemOrange)
        } else {
            grantPermissionMenuItem.isHidden = true
            if paused {
                statusMenuItem.title = "Paused"
                pauseResumeMenuItem.title = "Resume"
                setIcon(color: .systemYellow)
            } else if connected {
                statusMenuItem.title = "Connected"
                pauseResumeMenuItem.title = "Pause"
                setIcon(color: .systemGreen)
            } else {
                statusMenuItem.title = "Server disconnected"
                pauseResumeMenuItem.title = "Pause"
                setIcon(color: .red)
            }
        }
    }

    // MARK: Actions

    @objc private func togglePause() {
        let endpoint = isPaused ? "resume" : "pause"
        guard let url = URL(string: "\(controlURL)/\(endpoint)") else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 2.0
        URLSession.shared.dataTask(with: request) { [weak self] _, _, _ in
            // Next poll will update state
            DispatchQueue.main.async {
                self?.pollStatus()
            }
        }.resume()
    }

    @objc private func openScreenRecordingSettings() {
        // macOS 13+ uses the new System Settings URL scheme
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture") {
            NSWorkspace.shared.open(url)
        }
    }

    @objc private func openSettings() {
        let projDir = findProjectDir()
        if settingsController == nil {
            settingsController = SettingsWindowController(projectDir: projDir) { [weak self] in
                self?.restartDaemon()
            }
        }
        settingsController?.showWindow()
    }

    @objc private func quit() {
        stopDaemon()
        NSApp.terminate(nil)
    }

    // MARK: Icon

    private func setIcon(color: NSColor) {
        let size = NSSize(width: 18, height: 18)
        let image = NSImage(size: size, flipped: false) { rect in
            color.setFill()
            let circle = NSBezierPath(ovalIn: rect.insetBy(dx: 3, dy: 3))
            circle.fill()
            return true
        }
        image.isTemplate = false
        statusItem.button?.image = image
    }
}

// MARK: - FileHandle helper

extension FileHandle {
    static func forWritingOrCreate(at path: String) -> FileHandle {
        let fm = FileManager.default
        if !fm.fileExists(atPath: path) {
            fm.createFile(atPath: path, contents: nil)
        }
        return FileHandle(forWritingAtPath: path) ?? .nullDevice
    }
}

// MARK: - Main

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
