import AppKit
import SwiftUI

/// Menu-bar status item with the harness app's menu and a Settings window.
@MainActor
final class MenuBarController: NSObject, NSWindowDelegate {
    private var statusItem: NSStatusItem!
    private var settingsWindow: NSWindow?
    private var settingsTab: SettingsTab = .today

    func install() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            let img = NSImage(systemSymbolName: "fish", accessibilityDescription: "Harness")
            img?.isTemplate = true
            button.image = img
        }

        let menu = NSMenu()
        menu.addItem(NSMenuItem(
            title: "Open Settings…",
            action: #selector(openSettings),
            keyEquivalent: ","
        ).withTarget(self))
        menu.addItem(NSMenuItem(
            title: "Open Pipeline Window",
            action: #selector(openPipelineWindow),
            keyEquivalent: "p"
        ).withTarget(self))
        menu.addItem(NSMenuItem(
            title: "Open Diet Window",
            action: #selector(openDietWindow),
            keyEquivalent: "i"
        ).withTarget(self))
        menu.addItem(NSMenuItem(
            title: "Open Retro Labeler (Web)",
            action: #selector(openLabeler),
            keyEquivalent: "l"
        ).withTarget(self))
        menu.addItem(NSMenuItem(
            title: "Open Dashboard (Web)",
            action: #selector(openDashboard),
            keyEquivalent: "d"
        ).withTarget(self))
        menu.addItem(NSMenuItem.separator())
        menu.addItem(NSMenuItem(
            title: "Snooze 30 min",
            action: #selector(snooze30),
            keyEquivalent: ""
        ).withTarget(self))
        menu.addItem(NSMenuItem(
            title: "Unsnooze",
            action: #selector(unsnooze),
            keyEquivalent: ""
        ).withTarget(self))
        menu.addItem(NSMenuItem.separator())
        menu.addItem(NSMenuItem(
            title: "Quit Harness Notch",
            action: #selector(quit),
            keyEquivalent: "q"
        ).withTarget(self))

        statusItem.menu = menu
    }

    @objc func openSettings() {
        openSettingsWindow(tab: .today, title: "Harness Settings")
    }

    @objc func openPipelineWindow() {
        openSettingsWindow(tab: .pipeline, title: "Harness Pipeline")
    }

    @objc func openDietWindow() {
        openSettingsWindow(tab: .diet, title: "Harness Diet")
    }

    private func openSettingsWindow(tab: SettingsTab, title: String) {
        if let w = settingsWindow, settingsTab == tab {
            w.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }
        if let w = settingsWindow {
            w.close()
            settingsWindow = nil
        }
        let w = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 720, height: 640),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        w.title = title
        w.center()
        w.delegate = self
        w.contentView = NSHostingView(rootView: SettingsRoot(initialTab: tab))
        w.isReleasedWhenClosed = false
        settingsTab = tab
        settingsWindow = w
        w.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc func openLabeler() {
        let url = URL(string: "http://127.0.0.1:7893/label")!
        NSWorkspace.shared.open(url)
    }

    @objc func openDashboard() {
        let url = URL(string: "http://127.0.0.1:7893/dashboard")!
        NSWorkspace.shared.open(url)
    }

    @objc func snooze30() {
        Task { await HarnessAPI.snooze(duration: "30m") }
    }

    @objc func unsnooze() {
        Task { await HarnessAPI.unsnooze() }
    }

    @objc func quit() {
        NSApp.terminate(nil)
    }

    nonisolated func windowWillClose(_ notification: Notification) {
        Task { @MainActor in
            // Keep the model in memory but allow the window to be re-created next time.
            self.settingsWindow = nil
        }
    }
}

private extension NSMenuItem {
    func withTarget(_ t: AnyObject) -> NSMenuItem {
        self.target = t
        return self
    }
}
