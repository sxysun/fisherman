import Foundation

/// Detects incognito/private browsing windows.
/// Chromium browsers: queries window properties via AppleScript (~150ms, cached 2s).
/// Non-Chromium: falls back to title-based pattern matching.
class IncognitoDetector {

    private static let chromiumBundles: Set<String> = [
        "com.google.Chrome",
        "com.google.Chrome.canary",
        "com.microsoft.edgemac",
        "com.brave.Browser",
        "company.thebrowser.Browser",   // Arc
        "com.vivaldi.Vivaldi",
        "com.operasoftware.Opera",
    ]

    private static let privatePatterns = [
        "private browsing",
        "inprivate",
        "(private)",
        "incognito",
    ]

    // Cache: one AppleScript call fetches ALL incognito titles for a browser.
    private static var cachedBundleId: String = ""
    private static var cachedIncognitoTitles: Set<String> = []
    private static var cacheTime: TimeInterval = 0
    private static let cacheTTL: TimeInterval = 2.0

    /// Returns true if the window appears to be private/incognito browsing.
    static func isIncognito(bundleId: String?, windowTitle: String?) -> Bool {
        guard let bundleId, let title = windowTitle, !title.isEmpty else { return false }

        if chromiumBundles.contains(bundleId) {
            return isChromiumIncognito(bundleId: bundleId, windowTitle: title)
        }

        return isTitlePrivate(title)
    }

    // MARK: - Private

    private static func isChromiumIncognito(bundleId: String, windowTitle: String) -> Bool {
        let now = Date().timeIntervalSince1970

        // Use cache if fresh and same browser
        if bundleId == cachedBundleId && (now - cacheTime) < cacheTTL {
            return cachedIncognitoTitles.contains(windowTitle)
        }

        // Query all incognito window titles via AppleScript
        let appName = chromiumAppName(for: bundleId)
        guard let incognitoTitles = queryIncognitoTitles(appName: appName) else {
            return isTitlePrivate(windowTitle)
        }

        cachedBundleId = bundleId
        cachedIncognitoTitles = incognitoTitles
        cacheTime = now

        return incognitoTitles.contains(windowTitle)
    }

    private static func chromiumAppName(for bundleId: String) -> String {
        switch bundleId {
        case "com.google.Chrome", "com.google.Chrome.canary": return "Google Chrome"
        case "com.microsoft.edgemac": return "Microsoft Edge"
        case "com.brave.Browser": return "Brave Browser"
        case "company.thebrowser.Browser": return "Arc"
        case "com.vivaldi.Vivaldi": return "Vivaldi"
        case "com.operasoftware.Opera": return "Opera"
        default: return bundleId
        }
    }

    /// One osascript call (~150ms) fetches all incognito window titles.
    private static func queryIncognitoTitles(appName: String) -> Set<String>? {
        let script = """
            if application "\(appName)" is running then
                tell application "\(appName)"
                    set result_list to ""
                    repeat with w in every window
                        set dominated to false
                        try
                            if mode of w is "incognito" then set dominated to true
                        end try
                        if not dominated then
                            try
                                if incognito of w then set dominated to true
                            end try
                        end if
                        if dominated then
                            if result_list is "" then
                                set result_list to name of w
                            else
                                set result_list to result_list & "|||" & name of w
                            end if
                        end if
                    end repeat
                    if result_list is "" then return "none"
                    return result_list
                end tell
            else
                return "not_running"
            end if
            """

        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        process.arguments = ["-e", script]

        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice

        do {
            try process.run()
            process.waitUntilExit()
        } catch {
            return nil
        }

        guard process.terminationStatus == 0 else { return nil }

        let output =
            String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""

        if output == "not_running" || output == "none" || output.isEmpty {
            return Set()
        }

        return Set(output.components(separatedBy: "|||"))
    }

    private static func isTitlePrivate(_ title: String) -> Bool {
        let lower = title.lowercased()
        return privatePatterns.contains { lower.contains($0) }
    }
}
