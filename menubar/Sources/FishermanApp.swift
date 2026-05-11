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

        // Auto-open settings on first launch if not configured
        if !configManager.isConfigured {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
                self?.openSettings()
            }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        hoverCancellable?.cancel()
        statusPoller?.stop()
        processManager?.stopAll()
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

    func windowWillClose(_ notification: Notification) {
        guard let window = notification.object as? NSWindow,
              window === settingsWindow
        else { return }
        settingsWindow = nil
    }
}

private final class SettingsPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }
}
