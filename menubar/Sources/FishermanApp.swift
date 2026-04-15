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

final class AppDelegate: NSObject, NSApplicationDelegate, @unchecked Sendable {
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
        let poller = statusPoller!
        let cfg = configManager!

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
                onSettings: { [weak self] in
                    self?.openSettings()
                },
                onQuit: {
                    pm.stopAll()
                    NSApp.terminate(nil)
                },
                onPoke: { friendName in
                    if let friend = cfg.friends.first(where: { $0.name == friendName }) {
                        poller.sendPoke(to: friend)
                    }
                },
                onClearPokes: {
                    poller.clearMyPokes()
                },
                onToggleTier: { friendName in
                    cfg.toggleFriendTier(name: friendName)
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
        if let existing = settingsWindow, existing.isVisible {
            existing.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
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
        hostingView.frame = NSRect(x: 0, y: 0, width: 400, height: 420)

        let window = NSPanel(
            contentRect: hostingView.frame,
            styleMask: [.titled, .closable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        window.title = "Fisherman Settings"
        window.contentView = hostingView
        window.isFloatingPanel = true
        window.level = .floating
        window.center()
        window.isReleasedWhenClosed = false
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        settingsWindow = window
    }
}
