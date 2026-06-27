import AppKit
import Combine
import CoreGraphics
import DynamicNotchKit
import SwiftUI

@main
struct FishermanApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate

    var body: some Scene {
        // No visible window — everything lives in the notch
        Settings {
            EmptyView()
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, @unchecked Sendable {
    private var appState: AppState!
    private var processManager: ProcessManager!
    private var statusPoller: StatusPoller!
    private var configManager: ConfigManager!
    private var notch: DynamicNotch<ExpandedContent, CompactLeading, CompactTrailing>!
    private var controlPort: String = "7892"
    private var hoverCancellable: AnyCancellable?
    private var settingsWindow: NSWindow?
    private var welcomeWindow: NSWindow?
    private var dailyCardWindow: NSWindow?
    private var friendCardWindow: NSWindow?
    private var friendCardWindowUserId: String?
    private var rewindWindow: NSWindow?
    /// Shared "I want Rewind to show date X" channel. The Rewind window
    /// reads requestedDate on appear and on every requestId bump, so
    /// clicking Rewind from a Daily Card opened to May 11 takes the user
    /// to May 11 — even if a Rewind window is already open on a different
    /// day. Bumping `requestId` is what guarantees re-presentation fires
    /// the .onChange even when the date hasn't otherwise changed.
    private let rewindCoordinator = RewindCoordinator()

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)

        // Register Fisherman with TCC for Screen Recording and prompt once, as
        // Fisherman itself, so it appears in System Settings → Privacy &
        // Security → Screen Recording. The capture daemon runs as our child
        // process and inherits this grant (default TCC responsibility), so the
        // grant must live on the app — not the separately-signed python binary.
        // No-op (returns true silently) once the user has granted, so this does
        // not re-prompt on every launch.
        let alreadyGranted = CGRequestScreenCaptureAccess()
        if !alreadyGranted {
            NSLog("[Fisherman] Screen Recording not yet granted — prompted user")
            // The prompt is async and the daemon spawns moments from now, so a
            // first-launch daemon caches the denial. Watch for the grant and
            // restart the daemon as a fresh (granted) child the instant it
            // flips — no 5-minute backoff or manual "Repair Capture" needed.
            watchForScreenRecordingGrant()
        }

        // macOS does not enforce single-instance for LSUIElement apps. During
        // "Update Fisherman", the upgrade subprocess pkill's the old menubar
        // and then `open -a Fisherman` launches the new one — but the two can
        // briefly overlap, leaving two notches on screen. Take a "new wins"
        // approach: terminate any sibling instance before we wire up our own
        // DynamicNotch.
        terminateOlderInstances()

        // DMG builds embed the Python source in the app bundle. On first
        // launch, copy it into ~/.fisherman and prepare the venv before the
        // config manager tries to read or create ~/.fisherman/.env.
        BundledBootstrap.ensureInstall()

        configManager = ConfigManager()
        configManager.load()

        controlPort = configManager.controlPort
        appState = AppState()
        processManager = ProcessManager(controlPort: controlPort)
        statusPoller = StatusPoller(state: appState, controlPort: controlPort, config: configManager)

        let state = appState!
        let pm = processManager!
        let port = controlPort

        notch = DynamicNotch(
            hoverBehavior: .all,
            style: .auto
        ) {
            ExpandedContent(
                state: state,
                onPauseResume: {
                    pm.togglePause(isPaused: state.isPaused)
                },
                onViewFrames: {
                    if let url = URL(string: "http://127.0.0.1:\(port)/viewer") {
                        NSWorkspace.shared.open(url)
                    }
                },
                onRepairCapture: {
                    pm.repairCaptureStack()
                },
                onSettings: { [weak self] in
                    Task { @MainActor in
                        self?.openSettings()
                    }
                },
                onOpenCard: { [weak self] in
                    Task { @MainActor in
                        self?.openDailyCard()
                    }
                },
                onOpenFriendCard: { [weak self] user in
                    Task { @MainActor in
                        self?.openFriendCard(user)
                    }
                },
                onPoke: { [weak self] user in
                    self?.statusPoller?.sendPoke(to: user)
                },
                onClearPokes: { [weak self] in
                    self?.statusPoller?.clearIncomingPokes()
                },
                onQuit: {
                    pm.stopAll()
                    NSApp.terminate(nil)
                }
            )
        } compactLeading: {
            CompactLeading(state: state)
        } compactTrailing: {
            CompactTrailing(state: state)
        }

        // Expand on hover, collapse when mouse leaves
        let notchRef = notch!
        hoverCancellable = notchRef.$isHovering
            .debounce(for: .milliseconds(150), scheduler: DispatchQueue.main)
            .removeDuplicates()
            .sink { hovering in
                Task { @MainActor in
                    if hovering {
                        await notchRef.expand()
                    } else {
                        await notchRef.compact()
                    }
                }
            }

        // Launch processes
        processManager.startAll()

        // Start health polling
        statusPoller.start()

        // Show compact notch
        Task { @MainActor in
            await notch.compact()
        }

        // First-launch flow: brand-new installs (FISH_ONBOARDED=0) get the
        // welcome wizard. Legacy installs (no FISH_ONBOARDED line) are
        // treated as already onboarded by ConfigManager and skip this.
        if !configManager.isConfigured {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
                self?.openWelcomeWizard()
            }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        hoverCancellable?.cancel()
        statusPoller?.stop()
        processManager?.stopAll()
    }

    // MARK: - Single-instance enforcement

    /// Poll for the Screen Recording grant after a first-launch prompt and
    /// restart the daemon once it flips to granted, so capture starts the
    /// instant the user allows it. Stops itself after the grant lands or after
    /// a 5-minute window (the user dismissed the prompt; "Repair Capture" or a
    /// relaunch remains available).
    private var screenGrantWatchTimer: Timer?
    private func watchForScreenRecordingGrant() {
        let deadline = Date().addingTimeInterval(300)
        screenGrantWatchTimer?.invalidate()
        screenGrantWatchTimer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] timer in
            guard let self else { timer.invalidate(); return }
            if CGPreflightScreenCaptureAccess() {
                NSLog("[Fisherman] Screen Recording granted — restarting daemon as a granted child")
                self.processManager?.restartFisherman()
                timer.invalidate()
                self.screenGrantWatchTimer = nil
            } else if Date() > deadline {
                timer.invalidate()
                self.screenGrantWatchTimer = nil
            }
        }
    }

    private func terminateOlderInstances() {
        let me = NSRunningApplication.current
        let bid = Bundle.main.bundleIdentifier ?? "com.fisherman.menubar"
        let siblings = NSRunningApplication.runningApplications(withBundleIdentifier: bid)
            .filter { $0.processIdentifier != me.processIdentifier }
        guard !siblings.isEmpty else { return }

        NSLog("[Fisherman] found \(siblings.count) older Fisherman instance(s); terminating to enforce single-instance")
        for app in siblings {
            _ = app.terminate()
        }

        // Wait up to 3s for graceful exit. Polling `runningApplications` is
        // safe on the main thread before the run loop starts — each call is
        // a fresh OS query, no KVO required.
        let deadline = Date().addingTimeInterval(3.0)
        while Date() < deadline {
            let alive = NSRunningApplication.runningApplications(withBundleIdentifier: bid)
                .filter { $0.processIdentifier != me.processIdentifier }
            if alive.isEmpty { return }
            Thread.sleep(forTimeInterval: 0.1)
        }

        // Force-terminate stragglers (typically blocked in their own
        // applicationWillTerminate stopping the daemon).
        let stragglers = NSRunningApplication.runningApplications(withBundleIdentifier: bid)
            .filter { $0.processIdentifier != me.processIdentifier }
        for app in stragglers {
            NSLog("[Fisherman] force-terminating straggler PID \(app.processIdentifier)")
            _ = app.forceTerminate()
        }

        // Hard fallback: SIGKILL any sibling matched by executable path.
        // NSRunningApplication's bundle-id lookup has missed live instances in
        // the wild (two menu bars coexisting for hours, each spawning a
        // daemon), so don't rely on it alone — a path match can't miss.
        killSiblingsByExecutablePath()

        // Small settle so the menu bar slot is fully released before we
        // claim it via DynamicNotch.
        Thread.sleep(forTimeInterval: 0.3)
    }

    /// SIGKILL every other process running our exact executable. Matches by the
    /// running binary's path (e.g. /Applications/Fisherman.app/Contents/MacOS/
    /// FishermanMenu), so it's independent of bundle-identifier registration —
    /// the part that has silently failed. Excludes our own pid.
    private func killSiblingsByExecutablePath() {
        let myPid = ProcessInfo.processInfo.processIdentifier
        let exePath = Bundle.main.executablePath ?? "FishermanMenu"

        let pgrep = Process()
        pgrep.executableURL = URL(fileURLWithPath: "/usr/bin/pgrep")
        pgrep.arguments = ["-f", exePath]
        let pipe = Pipe()
        pgrep.standardOutput = pipe
        pgrep.standardError = FileHandle.nullDevice
        do { try pgrep.run(); pgrep.waitUntilExit() }
        catch {
            NSLog("[Fisherman] pgrep fallback failed to run: \(error)")
            return
        }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        guard let out = String(data: data, encoding: .utf8) else { return }
        for token in out.split(whereSeparator: { $0 == "\n" || $0 == " " }) {
            guard let pid = pid_t(token.trimmingCharacters(in: .whitespaces)),
                  pid != myPid else { continue }
            NSLog("[Fisherman] SIGKILL sibling menu bar PID \(pid) (path-match fallback)")
            kill(pid, SIGKILL)
        }
    }

    // MARK: - Welcome wizard

    @MainActor private func openWelcomeWizard() {
        if let existing = welcomeWindow {
            presentWelcomeWindow(existing)
            return
        }

        let cm = configManager!
        let pm = processManager!

        let view = WelcomeWizard(config: cm) { [weak self] in
            self?.welcomeWindow?.close()
            self?.welcomeWindow = nil
            pm.restartFisherman()
        }

        let hostingView = NSHostingView(rootView: view)
        hostingView.frame = NSRect(x: 0, y: 0, width: 520, height: 540)

        let window = WelcomePanel(
            contentRect: hostingView.frame,
            styleMask: [.titled, .closable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        window.title = "Welcome to Fisherman"
        window.contentView = hostingView
        window.delegate = self
        window.isFloatingPanel = false
        window.hidesOnDeactivate = false
        window.level = .floating
        window.collectionBehavior = [.moveToActiveSpace, .fullScreenAuxiliary]
        window.center()
        window.isReleasedWhenClosed = false

        welcomeWindow = window
        presentWelcomeWindow(window)
    }

    @MainActor private func presentWelcomeWindow(_ window: NSWindow) {
        if window.isMiniaturized { window.deminiaturize(nil) }
        window.setIsVisible(true)
        NSApp.unhide(nil)
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
        window.orderFrontRegardless()
    }

    // MARK: - Settings window

    @MainActor private func openSettings() {
        if let existing = settingsWindow {
            presentSettingsWindow(existing)
            return
        }

        let cm = configManager!
        let pm = processManager!
        cm.load() // refresh from disk

        let settingsView = SettingsView(
            config: cm,
            onSave: { [weak self] in
                self?.settingsWindow?.close()
                pm.restartFisherman()
            },
            onCancel: { [weak self] in
                self?.settingsWindow?.close()
            }
        )

        let hostingView = NSHostingView(rootView: settingsView)
        // 600×500 fits the now-8 tabs without word-wrapping. The
        // earlier 400×420 was tight even with 7 tabs.
        hostingView.frame = NSRect(x: 0, y: 0, width: 600, height: 500)

        let window = SettingsPanel(
            contentRect: hostingView.frame,
            styleMask: [.titled, .closable, .resizable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        window.title = "Fisherman Settings"
        window.contentView = hostingView
        window.delegate = self
        window.isFloatingPanel = false
        window.hidesOnDeactivate = false
        window.level = .floating
        window.collectionBehavior = [.moveToActiveSpace, .fullScreenAuxiliary]
        window.center()
        window.isReleasedWhenClosed = false
        window.minSize = NSSize(width: 480, height: 360)

        settingsWindow = window
        presentSettingsWindow(window)
    }

    @MainActor private func presentSettingsWindow(_ window: NSWindow) {
        NSLog("[Fisherman] presenting settings window")
        if window.isMiniaturized {
            window.deminiaturize(nil)
        }
        window.setIsVisible(true)
        NSApp.unhide(nil)
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
        window.orderFrontRegardless()

        DispatchQueue.main.async {
            NSApp.activate(ignoringOtherApps: true)
            window.makeKeyAndOrderFront(nil)
            window.orderFrontRegardless()
        }
    }

    // MARK: - Daily Card window

    @MainActor private func openDailyCard() {
        if let existing = dailyCardWindow {
            presentDailyCardWindow(existing)
            return
        }

        let state = appState!
        let cm = configManager!
        // Black background to match the notch aesthetic. Forced dark color
        // scheme so .primary/.secondary text stays readable on black even
        // if the system is in light mode.
        let view = DailyCardWindowView(
            state: state,
            config: cm,
            onOpenRewind: { [weak self] date in
                Task { @MainActor in self?.openRewind(at: date) }
            }
        )
            .padding(EdgeInsets(top: 28, leading: 20, bottom: 20, trailing: 20))
            .frame(minWidth: 460, idealWidth: 520, minHeight: 480, idealHeight: 620)
            .background(Color.black)
            .preferredColorScheme(.dark)

        let hostingView = NSHostingView(rootView: view)
        hostingView.frame = NSRect(x: 0, y: 0, width: 520, height: 620)

        let window = DailyCardPanel(
            contentRect: hostingView.frame,
            styleMask: [.titled, .closable, .resizable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        window.title = "Daily Card"
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.backgroundColor = .black
        window.isOpaque = true
        window.isMovableByWindowBackground = true
        window.contentView = hostingView
        window.delegate = self
        window.isFloatingPanel = false
        window.hidesOnDeactivate = false
        window.level = .floating
        window.collectionBehavior = [.moveToActiveSpace, .fullScreenAuxiliary]
        window.center()
        window.isReleasedWhenClosed = false
        window.minSize = NSSize(width: 460, height: 420)

        dailyCardWindow = window
        presentDailyCardWindow(window)
    }

    @MainActor private func presentDailyCardWindow(_ window: NSWindow) {
        if window.isMiniaturized { window.deminiaturize(nil) }
        window.setIsVisible(true)
        NSApp.unhide(nil)
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
        window.orderFrontRegardless()
    }

    // MARK: - Friend Card window

    @MainActor private func openFriendCard(_ friend: UserActivity) {
        if let existing = friendCardWindow, friendCardWindowUserId == friend.id {
            presentDailyCardWindow(existing)
            return
        }

        friendCardWindow?.close()
        friendCardWindow = nil
        friendCardWindowUserId = nil

        let view = FriendCardWindowView(friend: friend)
            .padding(EdgeInsets(top: 28, leading: 20, bottom: 20, trailing: 20))
            .frame(minWidth: 460, idealWidth: 520, minHeight: 480, idealHeight: 620)
            .background(Color.black)
            .preferredColorScheme(.dark)

        let hostingView = NSHostingView(rootView: view)
        hostingView.frame = NSRect(x: 0, y: 0, width: 520, height: 620)

        let window = DailyCardPanel(
            contentRect: hostingView.frame,
            styleMask: [.titled, .closable, .resizable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        window.title = "\(friend.name) Card"
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.backgroundColor = .black
        window.isOpaque = true
        window.isMovableByWindowBackground = true
        window.contentView = hostingView
        window.delegate = self
        window.isFloatingPanel = false
        window.hidesOnDeactivate = false
        window.level = .floating
        window.collectionBehavior = [.moveToActiveSpace, .fullScreenAuxiliary]
        window.center()
        window.isReleasedWhenClosed = false
        window.minSize = NSSize(width: 460, height: 420)

        friendCardWindow = window
        friendCardWindowUserId = friend.id
        presentDailyCardWindow(window)
    }

    // MARK: - Rewind window

    @MainActor func openRewind(at date: Date? = nil) {
        // Publish the target date BEFORE presenting the window so any
        // already-running view picks it up immediately via .onChange.
        let targetDay = Calendar.current.startOfDay(for: date ?? Date())
        rewindCoordinator.requestedDate = targetDay
        rewindCoordinator.requestId &+= 1

        if let existing = rewindWindow {
            presentRewindWindow(existing)
            return
        }

        let state = appState!
        let cm = configManager!
        let coord = rewindCoordinator
        let view = RewindWindowView(state: state, config: cm, coordinator: coord)
            .padding(EdgeInsets(top: 28, leading: 20, bottom: 20, trailing: 20))
            .frame(minWidth: 640, idealWidth: 820, minHeight: 540, idealHeight: 720)
            .background(Color.black)
            .preferredColorScheme(.dark)

        let hostingView = NSHostingView(rootView: view)
        hostingView.frame = NSRect(x: 0, y: 0, width: 820, height: 720)

        let window = RewindPanel(
            contentRect: hostingView.frame,
            styleMask: [.titled, .closable, .resizable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        window.title = "Rewind"
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.backgroundColor = .black
        window.isOpaque = true
        window.isMovableByWindowBackground = true
        window.contentView = hostingView
        window.delegate = self
        window.isFloatingPanel = false
        window.hidesOnDeactivate = false
        window.level = .floating
        window.collectionBehavior = [.moveToActiveSpace, .fullScreenAuxiliary]
        window.center()
        window.isReleasedWhenClosed = false
        window.minSize = NSSize(width: 640, height: 480)

        rewindWindow = window
        presentRewindWindow(window)
    }

    @MainActor private func presentRewindWindow(_ window: NSWindow) {
        if window.isMiniaturized { window.deminiaturize(nil) }
        window.setIsVisible(true)
        NSApp.unhide(nil)
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
        window.orderFrontRegardless()
    }

    func windowWillClose(_ notification: Notification) {
        guard let window = notification.object as? NSWindow else { return }
        if window === settingsWindow { settingsWindow = nil }
        if window === welcomeWindow { welcomeWindow = nil }
        if window === dailyCardWindow { dailyCardWindow = nil }
        if window === friendCardWindow {
            friendCardWindow = nil
            friendCardWindowUserId = nil
        }
        if window === rewindWindow { rewindWindow = nil }
    }
}

/// Shared "switch Rewind to date X" channel. AppDelegate owns one
/// instance; both the menu code that opens the window and the view
/// inside the window read/write `requestedDate` here, so a Rewind
/// already on screen can be navigated to a different day without
/// being torn down and rebuilt (preserving the image cache).
///
/// `requestId` is bumped on every open call so SwiftUI's .onChange
/// fires even when the same date is requested twice — important so
/// "click Rewind on May 11 → navigate inside Rewind to May 13 →
/// close it → click Rewind on May 11 card again" actually snaps back
/// to May 11 instead of staying on May 13.
@Observable
final class RewindCoordinator {
    var requestedDate: Date = Calendar.current.startOfDay(for: Date())
    var requestId: Int = 0
}

/// Daily Card popup with prev/next-day navigation. Owns its own selectedDate
/// + per-day fetch cache so flipping back to a previously viewed day is
/// instant. Falls back to AppState's live "me" history when displaying today
/// before the first fetch returns, so the window never opens empty.
private struct DailyCardWindowView: View {
    let state: AppState
    let config: ConfigManager
    /// Called with the date currently shown in the card so the Rewind
    /// window opens at that day instead of always defaulting to today.
    let onOpenRewind: (Date) -> Void

    @State private var selectedDate: Date = Calendar.current.startOfDay(for: Date())
    @State private var cache: [Date: [ActivityEntry]] = [:]
    @State private var loadingDates: Set<Date> = []
    @State private var lastError: String?
    @State private var calendarOpen: Bool = false

    private var today: Date { Calendar.current.startOfDay(for: Date()) }
    private var isToday: Bool { selectedDate == today }
    private var canGoForward: Bool { selectedDate < today }

    /// Live "me" history from the poller — used as a placeholder for today
    /// before the first authoritative fetch lands so the window has content
    /// to render immediately on open.
    private var liveMeHistory: [ActivityEntry] {
        state.allActivity.first(where: { $0.id == "me" })?.history ?? []
    }

    private var displayedHistory: [ActivityEntry] {
        if let cached = cache[selectedDate] { return cached }
        if isToday { return liveMeHistory }
        return []
    }

    private var dateLabel: String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US")
        if isToday {
            f.dateFormat = "EEEE MMM d"
            return "Today · \(f.string(from: selectedDate))"
        }
        let cal = Calendar.current
        if let yest = cal.date(byAdding: .day, value: -1, to: today),
           selectedDate == yest {
            f.dateFormat = "EEEE MMM d"
            return "Yesterday · \(f.string(from: selectedDate))"
        }
        f.dateFormat = "EEEE MMM d, yyyy"
        return f.string(from: selectedDate)
    }

    private var emptyMessage: String {
        if loadingDates.contains(selectedDate) { return "Loading…" }
        if let err = lastError { return err }
        if isToday { return "No activity today yet — capture is warming up." }
        return "No activity recorded on this day."
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            navHeader
            Divider().opacity(0.4)
            DailyCardView(
                history: displayedHistory,
                displayDate: selectedDate,
                emptyMessage: emptyMessage
            )
        }
        .onAppear {
            fetchIfNeeded(selectedDate)
        }
        .onChange(of: selectedDate) { _, newValue in
            fetchIfNeeded(newValue)
        }
    }

    private var navHeader: some View {
        HStack(spacing: 8) {
            Button {
                shiftDay(by: -1)
            } label: {
                Image(systemName: "chevron.left")
                    .font(.system(size: 12, weight: .semibold))
                    .frame(width: 14, height: 14)
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            .help("Previous day")

            Button {
                calendarOpen.toggle()
            } label: {
                HStack(spacing: 4) {
                    Text(dateLabel)
                        .font(.system(size: 12, weight: .medium))
                    Image(systemName: "calendar")
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                }
                .frame(minWidth: 200, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help("Pick a date")
            .popover(isPresented: $calendarOpen) {
                CapturesCalendarView(
                    config: config,
                    selectedDate: $selectedDate,
                    isOpen: $calendarOpen
                )
                .preferredColorScheme(.dark)
            }

            Button {
                shiftDay(by: 1)
            } label: {
                Image(systemName: "chevron.right")
                    .font(.system(size: 12, weight: .semibold))
                    .frame(width: 14, height: 14)
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            .disabled(!canGoForward)
            .help(canGoForward ? "Next day" : "Already on today")

            if loadingDates.contains(selectedDate) {
                ProgressView()
                    .controlSize(.small)
            }

            Spacer()

            if !isToday {
                Button("Today") {
                    selectedDate = today
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
            }

            Button {
                onOpenRewind(selectedDate)
            } label: {
                HStack(spacing: 4) {
                    Image(systemName: "play.rectangle")
                    Text("Rewind")
                }
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            .help("Open Rewind for this day")

            Button {
                cache[selectedDate] = nil
                fetchIfNeeded(selectedDate)
            } label: {
                Image(systemName: "arrow.clockwise")
                    .font(.system(size: 12, weight: .semibold))
                    .frame(width: 14, height: 14)
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            .help("Refresh this day")
        }
    }

    private func shiftDay(by days: Int) {
        guard let next = Calendar.current.date(byAdding: .day, value: days, to: selectedDate)
        else { return }
        let nextStart = Calendar.current.startOfDay(for: next)
        if nextStart > today { return }
        selectedDate = nextStart
    }

    private func fetchIfNeeded(_ day: Date) {
        let dayStart = Calendar.current.startOfDay(for: day)
        guard cache[dayStart] == nil, !loadingDates.contains(dayStart) else { return }
        guard let dayEnd = Calendar.current.date(byAdding: .day, value: 1, to: dayStart)
        else { return }

        loadingDates.insert(dayStart)
        lastError = nil
        Task {
            let result = await fetchActivityHistory(
                config: config,
                since: dayStart,
                until: dayEnd
            )
            await MainActor.run {
                loadingDates.remove(dayStart)
                switch result {
                case .success(let entries):
                    cache[dayStart] = entries
                case .failure(let err):
                    // Don't blow away cached data — only surface the error
                    // when the day genuinely has nothing to show.
                    if cache[dayStart] == nil {
                        lastError = err.localizedDescription
                    }
                }
            }
        }
    }
}

private final class SettingsPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }
}

private final class WelcomePanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }
}

private final class DailyCardPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }
}

private final class RewindPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }
}
