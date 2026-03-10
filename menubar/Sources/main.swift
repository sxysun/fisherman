import AppKit
import CoreGraphics
import Foundation
import IOKit.ps
import Vision

// MARK: - Settings Window Controller

class SettingsWindowController: NSObject, NSWindowDelegate {
    private var window: NSWindow?
    private let projectDir: String

    // Fields
    private var serverURLField: NSTextField!
    private var authTokenField: NSTextField!
    private var captureIntervalField: NSTextField!
    private var jpegQualityField: NSTextField!
    private var maxDimensionField: NSTextField!
    private var controlPortField: NSTextField!
    private var vlmEnabledCheckbox: NSButton!
    private var vlmIntervalField: NSTextField!

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
            contentRect: NSRect(x: 0, y: 0, width: 520, height: 440),
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

        var y: CGFloat = 400

        func addLabel(_ text: String) {
            let label = NSTextField(labelWithString: text)
            label.frame = NSRect(x: 20, y: y, width: 160, height: 20)
            label.font = .systemFont(ofSize: 12, weight: .medium)
            content.addSubview(label)
        }

        func addField(placeholder: String, monospace: Bool = false) -> NSTextField {
            let field = NSTextField(frame: NSRect(x: 180, y: y, width: 320, height: 22))
            field.placeholderString = placeholder
            field.font = monospace
                ? .monospacedSystemFont(ofSize: 11, weight: .regular)
                : .systemFont(ofSize: 12)
            field.lineBreakMode = .byTruncatingTail
            field.usesSingleLineMode = true
            field.cell?.isScrollable = true
            content.addSubview(field)
            y -= 36
            return field
        }

        // Server URL
        addLabel("Server URL:")
        serverURLField = addField(placeholder: "ws://localhost:9999/ingest")

        // Auth Token
        addLabel("Auth Token:")
        authTokenField = addField(placeholder: "optional", monospace: true)

        // Separator
        y -= 4
        let sep = NSBox(frame: NSRect(x: 20, y: y, width: 480, height: 1))
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

        // VLM section
        y -= 4
        let sep2 = NSBox(frame: NSRect(x: 20, y: y, width: 480, height: 1))
        sep2.boxType = .separator
        content.addSubview(sep2)
        y -= 16

        let vlmLabel = NSTextField(labelWithString: "Scene Understanding (VLM)")
        vlmLabel.frame = NSRect(x: 20, y: y, width: 200, height: 18)
        vlmLabel.font = .systemFont(ofSize: 11, weight: .semibold)
        vlmLabel.textColor = .secondaryLabelColor
        content.addSubview(vlmLabel)
        y -= 32

        vlmEnabledCheckbox = NSButton(checkboxWithTitle: "Enable VLM (Moondream — runs locally on T2 frames)", target: nil, action: nil)
        vlmEnabledCheckbox.frame = NSRect(x: 20, y: y, width: 480, height: 20)
        vlmEnabledCheckbox.font = .systemFont(ofSize: 12)
        content.addSubview(vlmEnabledCheckbox)
        y -= 32

        addLabel("VLM Interval (s):")
        vlmIntervalField = addField(placeholder: "10.0")

        // Save button
        let saveButton = NSButton(title: "Save & Restart", target: self, action: #selector(saveSettings))
        saveButton.bezelStyle = .rounded
        saveButton.frame = NSRect(x: 390, y: 12, width: 110, height: 28)
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
        vlmEnabledCheckbox.state = (env["FISH_VLM_ENABLED"] ?? "").lowercased() == "true" ? .on : .off
        vlmIntervalField.stringValue = env["FISH_VLM_INTERVAL"] ?? ""
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
            ("FISH_VLM_INTERVAL", vlmIntervalField),
        ]

        for (key, field) in fields {
            let val = field.stringValue.trimmingCharacters(in: .whitespaces)
            if val.isEmpty {
                env.removeValue(forKey: key)
            } else {
                env[key] = val
            }
        }

        // Checkbox field
        env["FISH_VLM_ENABLED"] = vlmEnabledCheckbox.state == .on ? "true" : "false"

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

// MARK: - OCR Result

struct OCRResult {
    let text: String
    let urls: [String]
}

// MARK: - URL regex (internal so Accessibility.swift can use it)

let urlPattern = try! NSRegularExpression(pattern: "https?://[^\\s<>\"')\\]]+", options: [])

func extractURLs(from text: String) -> [String] {
    let range = NSRange(text.startIndex..., in: text)
    let matches = urlPattern.matches(in: text, options: [], range: range)
    return matches.compactMap { match -> String? in
        guard let swiftRange = Range(match.range, in: text) else { return nil }
        return String(text[swiftRange])
    }
}

// MARK: - Capture Engine

class CaptureEngine {
    private var timer: Timer?
    private var lastHash: UInt64 = 0
    private var firstFrame = true
    private let threshold: Int
    private let maxDimension: Int
    private let jpegQuality: Double
    private let controlPort: String
    private let excludedBundles: Set<String>
    private let captureInterval: TimeInterval
    private let batteryCaptureInterval: TimeInterval
    private var currentInterval: TimeInterval
    private var consecutiveIdle: Int = 0
    private var session: URLSession

    // OCR pipelining: run Vision OCR on background queue
    private let ocrQueue = DispatchQueue(label: "fish.ocr", qos: .userInitiated)
    private var pendingOCR: (result: OCRResult, frameHash: UInt64)? = nil
    private let pendingOCRLock = NSLock()

    // Screenpipe-inspired optimizations
    private let ocrCache = OCRCache()
    private let activityMonitor = ActivityMonitor()

    init(controlPort: String, config: [String: String]) {
        self.controlPort = controlPort
        self.threshold = Int(config["FISH_DIFF_THRESHOLD"] ?? "") ?? 6
        self.maxDimension = Int(config["FISH_MAX_DIMENSION"] ?? "") ?? 1920
        self.jpegQuality = (Double(config["FISH_JPEG_QUALITY"] ?? "") ?? 60.0) / 100.0
        self.captureInterval = Double(config["FISH_CAPTURE_INTERVAL"] ?? "") ?? 2.0
        self.batteryCaptureInterval = Double(config["FISH_BATTERY_CAPTURE_INTERVAL"] ?? "") ?? 5.0
        self.currentInterval = self.captureInterval

        let bundleStr = config["FISH_EXCLUDED_BUNDLES"] ?? ""
        if bundleStr.isEmpty {
            self.excludedBundles = Set([
                "com.1password.1password",
                "com.agilebits.onepassword7",
                "com.apple.keychainaccess",
                "com.lastpass.LastPass",
                "com.dashlane.Dashlane",
                "com.bitwarden.desktop",
                "com.keepassxc.keepassxc",
                "com.apple.systempreferences",
                "com.apple.Passwords",
            ])
        } else {
            self.excludedBundles = Set(
                bundleStr.components(separatedBy: ",").map { $0.trimmingCharacters(in: .whitespaces) }
            )
        }

        let sessionConfig = URLSessionConfiguration.ephemeral
        sessionConfig.timeoutIntervalForRequest = 5
        sessionConfig.httpMaximumConnectionsPerHost = 2
        self.session = URLSession(configuration: sessionConfig)
    }

    func start() {
        // Watch for app switches — capture immediately
        NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didActivateApplicationNotification,
            object: nil, queue: .main
        ) { [weak self] _ in
            self?.captureNow()
        }

        // Activity monitor — capture on first input after idle
        activityMonitor.onActivity = { [weak self] in
            self?.captureNow()
        }
        activityMonitor.start()

        // Request accessibility permission (needed for AX tree + key monitoring)
        if !AccessibilityTextExtractor.isAvailable {
            AccessibilityTextExtractor.requestAccess()
        }

        currentInterval = CaptureEngine.isOnBattery() ? batteryCaptureInterval : captureInterval
        scheduleTimer()
    }

    func stop() {
        timer?.invalidate()
        timer = nil
        activityMonitor.stop()
        NSWorkspace.shared.notificationCenter.removeObserver(self)
    }

    private func scheduleTimer() {
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: currentInterval, repeats: false) { [weak self] _ in
            self?.captureNow()
        }
    }

    func captureNow() {
        // Always reschedule the timer from now (resets interval after manual triggers)
        updateInterval()
        scheduleTimer()

        // 1. Capture screen via CG API
        guard let cgImage = CGWindowListCreateImage(
            CGRect.null,
            .optionOnScreenOnly,
            kCGNullWindowID,
            .bestResolution
        ) else { return }

        // 2. Compute dhash
        let hash = computeDHash(cgImage)
        let distance = firstFrame ? 64 : hammingDistance(hash, lastHash)
        firstFrame = false

        if distance < threshold {
            consecutiveIdle += 1
            return
        }

        lastHash = hash
        consecutiveIdle = 0

        // 3. Get frontmost app info
        let frontApp = NSWorkspace.shared.frontmostApplication
        let appName = frontApp?.localizedName
        let bundleId = frontApp?.bundleIdentifier
        let pid = frontApp?.processIdentifier

        // Skip excluded bundles
        if let bid = bundleId, excludedBundles.contains(bid) {
            return
        }

        // Get window title via CGWindowListCopyWindowInfo
        var windowTitle: String? = nil
        if let pid {
            if let windowList = CGWindowListCopyWindowInfo(
                [.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID
            ) as? [[CFString: Any]] {
                for w in windowList {
                    if let wPid = w[kCGWindowOwnerPID] as? Int32, wPid == pid {
                        if let title = w[kCGWindowName] as? String, !title.isEmpty {
                            windowTitle = title
                            break
                        }
                    }
                }
            }
        }

        // 4. Skip incognito/private browsing windows
        if IncognitoDetector.isIncognito(bundleId: bundleId, windowTitle: windowTitle) {
            return
        }

        // 5. Resize + JPEG encode
        let origW = CGFloat(cgImage.width)
        let origH = CGFloat(cgImage.height)
        let scale = min(CGFloat(maxDimension) / max(origW, origH), 1.0)
        let newW = Int(origW * scale)
        let newH = Int(origH * scale)

        let jpegData: Data
        let imageForOCR: CGImage
        if scale < 1.0 {
            guard let ctx = CGContext(
                data: nil, width: newW, height: newH,
                bitsPerComponent: 8, bytesPerRow: 0,
                space: CGColorSpaceCreateDeviceRGB(),
                bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue
            ) else { return }
            ctx.interpolationQuality = .high
            ctx.draw(cgImage, in: CGRect(x: 0, y: 0, width: newW, height: newH))
            guard let resized = ctx.makeImage() else { return }
            imageForOCR = resized

            let rep = NSBitmapImageRep(cgImage: resized)
            guard let data = rep.representation(using: .jpeg, properties: [.compressionFactor: jpegQuality])
            else { return }
            jpegData = data
        } else {
            imageForOCR = cgImage
            let rep = NSBitmapImageRep(cgImage: cgImage)
            guard let data = rep.representation(using: .jpeg, properties: [.compressionFactor: jpegQuality])
            else { return }
            jpegData = data
        }

        // 6. Text extraction — prioritized pipeline:
        //    a) OCR cache hit (by dhash) → no work needed
        //    b) Accessibility tree → instant text, no Vision OCR
        //    c) Pipelined Vision OCR from previous frame
        //    d) None → Python daemon handles OCR
        var textResult: OCRResult? = nil
        var textSource: String? = nil
        var needsVisionOCR = true

        // (a) Check OCR cache
        if let cached = ocrCache.get(hash) {
            textResult = cached
            textSource = "cache"
            needsVisionOCR = false
        }

        // (b) Try accessibility tree (instant, ~1ms)
        if textResult == nil, let pid,
           let axResult = AccessibilityTextExtractor.extractText(pid: pid, bundleId: bundleId)
        {
            textResult = axResult
            textSource = "accessibility"
            ocrCache.set(hash, result: axResult)
            needsVisionOCR = false
        }

        // (c) Collect pipelined Vision OCR from previous frame
        if textResult == nil {
            pendingOCRLock.lock()
            if let pending = pendingOCR {
                textResult = pending.result
                textSource = "ocr"
                ocrCache.set(pending.frameHash, result: pending.result)
            }
            pendingOCR = nil
            pendingOCRLock.unlock()
            // Still need OCR for THIS frame even if we got prev frame's result
            needsVisionOCR = true
        }

        // 7. POST to Python daemon
        postFrame(
            jpeg: jpegData, width: newW, height: newH,
            appName: appName, bundleId: bundleId, windowTitle: windowTitle,
            timestamp: Date().timeIntervalSince1970, dhashDistance: distance,
            ocrResult: textResult, textSource: textSource
        )

        // 8. Start Vision OCR for this frame on background queue if needed
        if needsVisionOCR {
            let frameHash = hash
            ocrQueue.async { [weak self] in
                let result = Self.runOCR(on: imageForOCR)
                self?.pendingOCRLock.lock()
                self?.pendingOCR = (result: result, frameHash: frameHash)
                self?.pendingOCRLock.unlock()
            }
        }
    }

    // MARK: - Vision OCR

    static func runOCR(on image: CGImage) -> OCRResult {
        let handler = VNImageRequestHandler(cgImage: image, options: [:])
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.usesLanguageCorrection = false
        request.minimumTextHeight = 0.01

        do {
            try handler.perform([request])
        } catch {
            return OCRResult(text: "", urls: [])
        }

        guard let observations = request.results else {
            return OCRResult(text: "", urls: [])
        }

        var lines: [String] = []
        for obs in observations {
            let candidates = obs.topCandidates(3)
            if let best = candidates.max(by: { $0.confidence < $1.confidence }) {
                lines.append(best.string)
            }
        }

        let fullText = lines.joined(separator: "\n")
        let urls = extractURLs(from: fullText)
        return OCRResult(text: fullText, urls: urls)
    }

    // MARK: - dhash

    func computeDHash(_ image: CGImage) -> UInt64 {
        guard let ctx = CGContext(
            data: nil, width: 9, height: 8,
            bitsPerComponent: 8, bytesPerRow: 9,
            space: CGColorSpaceCreateDeviceGray(),
            bitmapInfo: CGImageAlphaInfo.none.rawValue
        ) else { return 0 }
        ctx.interpolationQuality = .high
        ctx.draw(image, in: CGRect(x: 0, y: 0, width: 9, height: 8))
        guard let data = ctx.data else { return 0 }
        let pixels = data.bindMemory(to: UInt8.self, capacity: 72)
        var hash: UInt64 = 0
        for y in 0..<8 {
            for x in 0..<8 {
                if pixels[y * 9 + x] > pixels[y * 9 + x + 1] {
                    hash |= 1 << (y * 8 + x)
                }
            }
        }
        return hash
    }

    func hammingDistance(_ a: UInt64, _ b: UInt64) -> Int {
        return (a ^ b).nonzeroBitCount
    }

    // MARK: - Adaptive interval (idle + activity + thermal)

    private func updateInterval() {
        let base = CaptureEngine.isOnBattery() ? batteryCaptureInterval : captureInterval
        var interval = base

        // Idle backoff
        if consecutiveIdle >= 10 {
            interval = min(interval * 2.0, 30.0)
        } else if consecutiveIdle >= 5 {
            interval = min(interval * 1.5, 15.0)
        }

        // Activity-based adjustment
        interval *= activityMonitor.intervalMultiplier

        // Thermal state adjustment
        let thermal = ProcessInfo.processInfo.thermalState
        switch thermal {
        case .critical:
            interval = max(interval, 10.0)
        case .serious:
            interval *= 3.0
        case .fair:
            interval *= 1.5
        case .nominal:
            break
        @unknown default:
            break
        }

        currentInterval = interval
    }

    // MARK: - Battery

    static func isOnBattery() -> Bool {
        guard let info = IOPSCopyPowerSourcesInfo()?.takeRetainedValue(),
              let list = IOPSCopyPowerSourcesList(info)?.takeRetainedValue() as? [CFTypeRef],
              let first = list.first,
              let desc = IOPSGetPowerSourceDescription(info, first)?.takeUnretainedValue()
                as? [String: Any]
        else { return false }
        return desc[kIOPSPowerSourceStateKey as String] as? String
            == kIOPSBatteryPowerValue as String
    }

    // MARK: - HTTP POST

    private func postFrame(
        jpeg: Data, width: Int, height: Int,
        appName: String?, bundleId: String?, windowTitle: String?,
        timestamp: Double, dhashDistance: Int,
        ocrResult: OCRResult? = nil, textSource: String? = nil
    ) {
        guard let url = URL(string: "http://127.0.0.1:\(controlPort)/frame") else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        var body: [String: Any] = [
            "jpeg_b64": jpeg.base64EncodedString(),
            "width": width,
            "height": height,
            "app_name": appName ?? "",
            "bundle_id": bundleId ?? "",
            "window_title": windowTitle ?? "",
            "timestamp": timestamp,
            "dhash_distance": dhashDistance,
        ]
        if let ocr = ocrResult {
            body["ocr_text"] = ocr.text
            body["urls"] = ocr.urls
        }
        if let source = textSource {
            body["text_source"] = source
        }
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        session.dataTask(with: request).resume()
    }
}

// MARK: - App Delegate

class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private var daemonProcess: Process?
    private var pollTimer: Timer?
    private var captureEngine: CaptureEngine?

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

        // Add Edit menu so Cmd+C/V/X/A work in text fields
        let mainMenu = NSMenu()
        let editMenuItem = NSMenuItem()
        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editMenuItem.submenu = editMenu
        mainMenu.addItem(editMenuItem)
        NSApp.mainMenu = mainMenu

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

        let viewerItem = NSMenuItem(title: "View Frames...", action: #selector(openViewer), keyEquivalent: "v")
        viewerItem.target = self
        menu.addItem(viewerItem)

        let settingsItem = NSMenuItem(title: "Settings...", action: #selector(openSettings), keyEquivalent: ",")
        settingsItem.target = self
        menu.addItem(settingsItem)

        menu.addItem(.separator())

        let quitItem = NSMenuItem(title: "Quit Fisherman", action: #selector(quit), keyEquivalent: "q")
        quitItem.target = self
        menu.addItem(quitItem)

        statusItem.menu = menu

        // Ensure Screen Recording permission before launching daemon.
        // The .app must be code-signed for this grant to persist across rebuilds.
        _ = ensureScreenRecordingAccess(prompt: true)

        // Launch daemon (skips if one is already running from Terminal)
        startDaemon()

        // Start CaptureEngine after a short delay to let daemon's control server bind
        let config = loadEnvConfig(projDir)
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) { [weak self] in
            guard let self else { return }
            let engine = CaptureEngine(controlPort: self.controlPort, config: config)
            self.captureEngine = engine
            engine.start()
        }

        // Poll status every 2s
        pollTimer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
            self?.pollStatus()
        }
        // Fire immediately
        pollStatus()
    }

    func applicationWillTerminate(_ notification: Notification) {
        captureEngine?.stop()
        captureEngine = nil
        stopDaemon()
    }

    // MARK: Daemon management

    private func ensureScreenRecordingAccess(prompt: Bool) -> Bool {
        if CGPreflightScreenCaptureAccess() {
            return true
        }
        if prompt && CGRequestScreenCaptureAccess() {
            return true
        }
        return CGPreflightScreenCaptureAccess()
    }

    private func isDaemonRunning() -> Bool {
        // Check if something is already listening on the control port
        let sock = socket(AF_INET, SOCK_STREAM, 0)
        guard sock >= 0 else { return false }
        defer { close(sock) }
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = UInt16(Int(controlPort) ?? 7891).bigEndian
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        let result = withUnsafePointer(to: &addr) { ptr in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sockPtr in
                Darwin.connect(sock, sockPtr, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        return result == 0
    }

    private func startDaemon() {
        // Don't launch if a daemon is already running (e.g. started from Terminal)
        if isDaemonRunning() {
            return
        }

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
        // Tell the Python daemon that Swift handles capture — it only
        // needs to receive frames via POST /frame and run OCR + routing.
        env["FISHERMAN_SWIFT_CAPTURE"] = "1"
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
        captureEngine?.stop()
        captureEngine = nil
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

        // Restart capture engine with fresh config
        let config = loadEnvConfig(projDir)
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) { [weak self] in
            guard let self else { return }
            let engine = CaptureEngine(controlPort: self.controlPort, config: config)
            self.captureEngine = engine
            engine.start()
        }
    }

    private func loadEnvConfig(_ projDir: String) -> [String: String] {
        let envPath = projDir + "/.env"
        guard let contents = try? String(contentsOfFile: envPath, encoding: .utf8) else {
            return [:]
        }
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

        // 2. Walk up from binary (dev — running from repo checkout)
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

        // 3. Installed location
        let installed = NSHomeDirectory() + "/.fisherman"
        if FileManager.default.fileExists(atPath: installed + "/pyproject.toml") {
            return installed
        }

        // Fallback
        return installed
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
        // Open System Settings directly — don't call CGRequestScreenCaptureAccess()
        // because the .app binary isn't what does the capturing (the child Python
        // process does), and prompting for the wrong binary causes a TCC loop.
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture") {
            NSWorkspace.shared.open(url)
        }
    }

    @objc private func openViewer() {
        let url = URL(string: "http://127.0.0.1:\(controlPort)/viewer")!
        NSWorkspace.shared.open(url)
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
