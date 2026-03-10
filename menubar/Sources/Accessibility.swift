import ApplicationServices
import Foundation

/// Extracts visible text from the focused window using the macOS Accessibility API.
/// Much faster than OCR (~1ms vs 40-200ms) but requires Accessibility permission.
class AccessibilityTextExtractor {

    // Bundle IDs where AX text is unreliable — always prefer Vision OCR.
    // Terminal emulators expose raw buffer text via AXTextArea which lacks
    // visual layout. Canvas-rendered apps (Figma, Google Docs in browser)
    // have thin AX trees that miss the main content.
    private static let ocrPreferredBundles: Set<String> = [
        "com.apple.Terminal",
        "com.googlecode.iterm2",
        "dev.warp.Warp-Stable",
        "net.kovidgoyal.kitty",
        "co.zeit.hyper",
        "com.mitchellh.ghostty",
        "io.alacritty",
        "com.github.wez.wezterm",
    ]

    /// Minimum text length to accept AX tree result. Below this threshold
    /// the tree is probably "thin" (toolbar/sidebar only, canvas app) and
    /// we should fall back to OCR.
    private static let minTextLength = 20

    /// Maximum AX elements to visit per walk (prevents pathological trees).
    private static let maxElements = 500

    /// Check whether the process has Accessibility permission.
    static var isAvailable: Bool {
        AXIsProcessTrusted()
    }

    /// Prompt the user for Accessibility permission if not already granted.
    static func requestAccess() {
        let key = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
        let options = [key: true] as CFDictionary
        AXIsProcessTrustedWithOptions(options)
    }

    /// Extract visible text from the focused window of the given process.
    ///
    /// Returns an `OCRResult` with the extracted text and URLs, or `nil` if:
    /// - The app prefers OCR (terminals, canvas apps)
    /// - Accessibility permission is not granted
    /// - The extracted text is too short (< 20 chars)
    static func extractText(pid: pid_t, bundleId: String?) -> OCRResult? {
        guard AXIsProcessTrusted() else { return nil }

        if let bid = bundleId, ocrPreferredBundles.contains(bid) {
            return nil
        }

        let appElement = AXUIElementCreateApplication(pid)

        // Get the focused window
        var windowRef: CFTypeRef?
        guard AXUIElementCopyAttributeValue(
            appElement, kAXFocusedWindowAttribute as CFString, &windowRef
        ) == .success else {
            return nil
        }

        // Walk the tree
        var texts: [String] = []
        var visited = 0
        collectText(
            from: windowRef as! AXUIElement,
            into: &texts,
            depth: 0,
            maxDepth: 12,
            visited: &visited
        )

        let fullText = texts.joined(separator: "\n")

        if fullText.count < minTextLength {
            return nil
        }

        // Extract URLs
        let urls = extractURLs(from: fullText)

        return OCRResult(text: fullText, urls: urls)
    }

    // MARK: - Private

    private static func collectText(
        from element: AXUIElement,
        into texts: inout [String],
        depth: Int,
        maxDepth: Int,
        visited: inout Int
    ) {
        guard depth < maxDepth, visited < maxElements else { return }
        visited += 1

        // Check role — skip non-content elements
        var roleRef: CFTypeRef?
        if AXUIElementCopyAttributeValue(element, kAXRoleAttribute as CFString, &roleRef) == .success,
           let role = roleRef as? String
        {
            if role == "AXScrollBar" || role == "AXMenu" || role == "AXMenuBar"
                || role == "AXToolbar"
            {
                return
            }
        }

        // AXValue — text content of text fields, text areas, etc.
        var valueRef: CFTypeRef?
        if AXUIElementCopyAttributeValue(element, kAXValueAttribute as CFString, &valueRef)
            == .success,
            let value = valueRef as? String
        {
            let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty {
                texts.append(trimmed)
            }
        }

        // AXTitle — button labels, group titles, etc.
        var titleRef: CFTypeRef?
        if AXUIElementCopyAttributeValue(element, kAXTitleAttribute as CFString, &titleRef)
            == .success,
            let title = titleRef as? String
        {
            let trimmed = title.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty && !texts.contains(trimmed) {
                texts.append(trimmed)
            }
        }

        // Recurse into children
        var childrenRef: CFTypeRef?
        if AXUIElementCopyAttributeValue(element, kAXChildrenAttribute as CFString, &childrenRef)
            == .success,
            let children = childrenRef as? [AXUIElement]
        {
            for child in children {
                collectText(
                    from: child, into: &texts, depth: depth + 1,
                    maxDepth: maxDepth, visited: &visited)
            }
        }
    }
}
