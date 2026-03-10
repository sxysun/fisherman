import AppKit
import Foundation

/// Monitors keyboard/mouse activity to enable adaptive capture rates.
/// Uses NSEvent global monitors (mouse events work without accessibility;
/// key events require accessibility permission but fail gracefully).
class ActivityMonitor {

    enum Level: Int, Comparable {
        case active = 0    // input within last 2s
        case cooling = 1   // input within last 5s
        case idle = 2      // 5-30s without input
        case deepIdle = 3  // > 30s without input

        static func < (lhs: Level, rhs: Level) -> Bool {
            lhs.rawValue < rhs.rawValue
        }
    }

    private var lastInputTime: TimeInterval = 0
    private var mouseMonitor: Any?
    private var keyMonitor: Any?

    /// Called when transitioning from idle/deepIdle to active (first input
    /// after a quiet period). Use this to trigger an immediate capture.
    var onActivity: (() -> Void)?

    var currentLevel: Level {
        let elapsed = ProcessInfo.processInfo.systemUptime - lastInputTime
        if elapsed < 2.0 { return .active }
        if elapsed < 5.0 { return .cooling }
        if elapsed < 30.0 { return .idle }
        return .deepIdle
    }

    /// Capture interval multiplier based on current activity level.
    var intervalMultiplier: Double {
        switch currentLevel {
        case .active: return 0.5   // faster during active use
        case .cooling: return 1.0
        case .idle: return 2.0
        case .deepIdle: return 4.0
        }
    }

    func start() {
        lastInputTime = ProcessInfo.processInfo.systemUptime

        // Mouse clicks + scroll (no accessibility permission needed)
        mouseMonitor = NSEvent.addGlobalMonitorForEvents(
            matching: [.leftMouseDown, .rightMouseDown, .scrollWheel]
        ) { [weak self] _ in
            self?.recordActivity()
        }

        // Key events (requires accessibility; fails silently without it)
        keyMonitor = NSEvent.addGlobalMonitorForEvents(
            matching: [.keyDown]
        ) { [weak self] _ in
            self?.recordActivity()
        }
    }

    func stop() {
        if let m = mouseMonitor { NSEvent.removeMonitor(m) }
        if let m = keyMonitor { NSEvent.removeMonitor(m) }
        mouseMonitor = nil
        keyMonitor = nil
    }

    private func recordActivity() {
        let wasIdle = currentLevel.rawValue >= Level.idle.rawValue
        lastInputTime = ProcessInfo.processInfo.systemUptime
        if wasIdle {
            onActivity?()
        }
    }
}
