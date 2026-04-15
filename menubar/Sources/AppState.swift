import AppKit
import Observation

enum AppStatus: String {
    case starting = "Starting..."
    case running = "Running"
    case paused = "Paused"
    case degraded = "Degraded"
    case error = "Error"
}

@Observable
final class AppState {
    var status: AppStatus = .starting

    // Screenpipe
    var screenpipeHealthy = false

    // Fisherman daemon
    var fishermanRunning = false
    var fishermanConnected = false
    var framesSent: Int = 0
    var framesDropped: Int = 0

    // Pause
    var isPaused = false

    // Error detail
    var errorDetail: String?

    // Activity (NEW)
    var currentActivity: String?  // e.g. "coding: main.py"
    var activityCategory: String?  // e.g. "coding", "reading", "browsing", "idle"
    var activityUpdatedAt: String?

    var statusColor: NSColor {
        switch status {
        case .starting: return .systemGray
        case .running: return .systemGreen
        case .paused: return .systemYellow
        case .degraded: return .systemOrange
        case .error: return .systemRed
        }
    }

    var statusText: String {
        if let detail = errorDetail {
            return detail
        }
        return status.rawValue
    }

    func update(screenpipeOK: Bool, fishermanStatus: [String: Any]?) {
        screenpipeHealthy = screenpipeOK

        if let s = fishermanStatus {
            fishermanRunning = true
            fishermanConnected = s["connected"] as? Bool ?? false
            framesSent = s["frames_sent"] as? Int ?? 0
            framesDropped = s["frames_dropped"] as? Int ?? 0
            isPaused = s["paused"] as? Bool ?? false
            errorDetail = s["error"] as? String
        } else {
            fishermanRunning = false
            fishermanConnected = false
        }

        // Derive overall status
        if isPaused {
            status = .paused
        } else if screenpipeOK && fishermanConnected {
            status = .running
            errorDetail = nil
        } else if screenpipeOK || fishermanRunning {
            status = .degraded
        } else if status != .starting {
            status = .error
        }
    }
}
