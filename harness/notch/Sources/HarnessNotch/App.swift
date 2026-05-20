import AppKit

@main
enum HarnessNotchMain {
    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.accessory)
        installEditMenu()
        app.run()
    }
}

/// `.accessory` apps don't get a default Edit menu, which means Cmd+C/V/X/A
/// silently fail in text fields. Install a minimal Edit menu so the standard
/// responder chain picks up the selectors.
@MainActor
private func installEditMenu() {
    let mainMenu = NSMenu()

    // App menu (mostly empty, just so the menubar isn't totally bare when
    // the settings window is key).
    let appItem = NSMenuItem()
    let appMenu = NSMenu()
    appItem.submenu = appMenu
    appMenu.addItem(
        withTitle: "Quit Harness Notch",
        action: #selector(NSApplication.terminate(_:)),
        keyEquivalent: "q"
    )
    mainMenu.addItem(appItem)

    // Edit menu — the actual fix
    let editItem = NSMenuItem()
    let editMenu = NSMenu(title: "Edit")
    editItem.submenu = editMenu
    editMenu.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
    let redo = editMenu.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "z")
    redo.keyEquivalentModifierMask = [.command, .shift]
    editMenu.addItem(NSMenuItem.separator())
    editMenu.addItem(withTitle: "Cut",        action: #selector(NSText.cut(_:)),         keyEquivalent: "x")
    editMenu.addItem(withTitle: "Copy",       action: #selector(NSText.copy(_:)),        keyEquivalent: "c")
    editMenu.addItem(withTitle: "Paste",      action: #selector(NSText.paste(_:)),       keyEquivalent: "v")
    editMenu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)),   keyEquivalent: "a")
    mainMenu.addItem(editItem)

    // Window menu — gives Cmd+W close, Cmd+M minimize for the settings window.
    let windowItem = NSMenuItem()
    let windowMenu = NSMenu(title: "Window")
    windowItem.submenu = windowMenu
    windowMenu.addItem(withTitle: "Minimize",
                       action: #selector(NSWindow.performMiniaturize(_:)),
                       keyEquivalent: "m")
    windowMenu.addItem(withTitle: "Close",
                       action: #selector(NSWindow.performClose(_:)),
                       keyEquivalent: "w")
    mainMenu.addItem(windowItem)

    NSApp.mainMenu = mainMenu
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var coordinator: NotchCoordinator?
    private var menuBar: MenuBarController?

    func applicationDidFinishLaunching(_: Notification) {
        let baseURL = ProcessInfo.processInfo.environment["HARNESS_URL"]
            ?? "http://127.0.0.1:7893"
        Task { @MainActor in
            let c = NotchCoordinator(baseURL: baseURL)
            c.start()
            self.coordinator = c

            let mb = MenuBarController()
            mb.install()
            self.menuBar = mb
        }
    }

    func applicationWillTerminate(_: Notification) {
        Task { @MainActor in
            self.coordinator?.stop()
        }
    }
}
