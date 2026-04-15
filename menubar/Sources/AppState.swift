import AppKit
import Observation

enum AppStatus: String {
    case starting = "Starting..."
    case running = "Running"
    case paused = "Paused"
    case degraded = "Degraded"
    case error = "Error"
}

struct ActivityEntry: Identifiable {
    let id = UUID()
    let emoji: String
    let category: String
    let status: String
    let timestamp: Date
}

enum ActivityCategory: String {
    case coding, debugging, codeReview = "code review", readingDocs = "reading docs"
    case design, writing, chat, email, meeting
    case browsing, news, reading, gaming, terminal, idle

    var color: NSColor {
        switch self {
        case .coding, .debugging, .codeReview, .readingDocs, .terminal:
            return .systemBlue
        case .reading, .writing:
            return .systemGreen
        case .chat, .email:
            return .systemPurple
        case .browsing, .news:
            return .systemOrange
        case .design:
            return .systemYellow
        case .meeting:
            return .systemRed
        case .gaming:
            return .systemPink
        case .idle:
            return .systemGray
        }
    }

    static func from(_ string: String) -> ActivityCategory {
        ActivityCategory(rawValue: string) ?? .idle
    }
}

struct Poke: Identifiable {
    let id = UUID()
    let fromShort: String   // first 16 chars of pubkey
    let at: Date
}

struct UserActivity: Identifiable {
    let id: String          // "me" or friend name
    let name: String
    let emoji: String
    let category: String
    let status: String
    let stale: Bool
    var history: [ActivityEntry] = []
    var sessionStart: Date?
    var isWorkingTogether: Bool = false
    var inFlow: Bool = false
    var pokes: [Poke] = []
    var sharingTier: SharingTier = .high

    var sessionDuration: TimeInterval {
        guard let start = sessionStart else { return 0 }
        return Date().timeIntervalSince(start)
    }

    var sessionDurationText: String {
        let mins = Int(sessionDuration / 60)
        if mins < 1 { return "" }
        if mins < 60 { return "\(mins)m" }
        return "\(mins / 60)h\(mins % 60)m"
    }
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

    // Multi-user activity
    var allActivity: [UserActivity] = []

    // Hangout suggestion
    var hangoutSuggestion: String?   // e.g. "You and 2 friends are winding down"
    var incomingPokes: [Poke] = []   // pokes received on "me"

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
