import AppKit
import Combine
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

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)

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
        let view = DailyCardWindowView(state: state, config: cm)
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

    func windowWillClose(_ notification: Notification) {
        guard let window = notification.object as? NSWindow else { return }
        if window === settingsWindow { settingsWindow = nil }
        if window === welcomeWindow { welcomeWindow = nil }
        if window === dailyCardWindow { dailyCardWindow = nil }
    }
}

/// Daily Card popup with prev/next-day navigation. Owns its own selectedDate
/// + per-day fetch cache so flipping back to a previously viewed day is
/// instant. Falls back to AppState's live "me" history when displaying today
/// before the first fetch returns, so the window never opens empty.
private struct DailyCardWindowView: View {
    let state: AppState
    let config: ConfigManager

    @State private var selectedDate: Date = Calendar.current.startOfDay(for: Date())
    @State private var cache: [Date: [ActivityEntry]] = [:]
    @State private var loadingDates: Set<Date> = []
    @State private var lastError: String?

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

            Text(dateLabel)
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(.primary)
                .frame(minWidth: 200, alignment: .leading)

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
