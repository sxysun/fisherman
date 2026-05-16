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
    case browsing, news, reading, gaming, terminal, idle, waiting

    /// Distinct hue per category, hand-tuned for dark-mode legibility.
    /// Related categories live in the same color family (all "coding-like"
    /// in the blue/cyan range, read/write in greens, comms in magentas) so
    /// the bar still reads as topical groupings, but no two categories
    /// share a color — same category always maps to the same color forever.
    var color: NSColor {
        switch self {
        case .coding:      return Self.hex(0x4F, 0xA8, 0xFF) // azure
        case .debugging:   return Self.hex(0x5B, 0xC5, 0xC5) // cyan
        case .codeReview:  return Self.hex(0x7A, 0x8C, 0xFF) // indigo
        case .readingDocs: return Self.hex(0x4F, 0xD3, 0xA8) // mint
        case .terminal:    return Self.hex(0x8B, 0x9F, 0xB8) // slate
        case .reading:     return Self.hex(0x5B, 0xD1, 0x6A) // green
        case .writing:     return Self.hex(0xA8, 0xD8, 0x5B) // lime
        case .chat:        return Self.hex(0xC9, 0x5B, 0xD1) // magenta
        case .email:       return Self.hex(0x8B, 0x6B, 0xD1) // violet
        case .browsing:    return Self.hex(0xFF, 0x9F, 0x40) // orange
        case .news:        return Self.hex(0xFF, 0xB8, 0x5B) // amber
        case .design:      return Self.hex(0xFF, 0xD2, 0x3F) // yellow
        case .meeting:     return Self.hex(0xFF, 0x5B, 0x6B) // red
        case .gaming:      return Self.hex(0xFF, 0x6F, 0xB5) // pink
        case .idle:        return Self.hex(0x6B, 0x72, 0x80) // gray
        case .waiting:     return Self.hex(0x4A, 0x52, 0x60) // dark gray
        }
    }

    private static func hex(_ r: Int, _ g: Int, _ b: Int) -> NSColor {
        NSColor(
            red: CGFloat(r) / 255.0,
            green: CGFloat(g) / 255.0,
            blue: CGFloat(b) / 255.0,
            alpha: 1.0
        )
    }

    static func from(_ string: String) -> ActivityCategory {
        ActivityCategory(rawValue: string) ?? .idle
    }
}

struct UserActivity: Identifiable {
    let id: String          // "me" or "relay:<pubkey>"
    let name: String
    let emoji: String
    let category: String
    let status: String
    let stale: Bool
    var history: [ActivityEntry] = []
    var sessionStart: Date?
    var inFlow: Bool = false

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

    // Capture
    var captureBackend: String = "native"

    // Fisherman daemon
    var fishermanRunning = false
    var fishermanConnected = false
    var backendMode: String = "local"
    var streamingEnabled = false
    var framesSent: Int = 0
    var framesStreamed: Int = 0
    var framesDropped: Int = 0
    var uploadQueuePending: Int = 0
    var uploadQueueUnbound: Int = 0
    var backendBlockCode: String?
    var backendBlockDetail: String?
    var backendBlockAction: String?
    var streamError: String?

    // Pause
    var isPaused = false

    // Error detail
    var errorDetail: String?

    // Multi-user activity
    var allActivity: [UserActivity] = []

    // Hangout suggestion
    var hangoutSuggestion: String?   // e.g. "You and 2 friends are winding down"

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
        if let backendDetail = backendStatusDetail, status == .degraded {
            return backendDetail
        }
        return status.rawValue
    }

    var ingestExpected: Bool {
        backendMode == "cloud" || backendMode == "self_hosted"
    }

    var fishermanHealthy: Bool {
        guard fishermanRunning else { return false }
        if streamingEnabled {
            return fishermanConnected
        }
        return !ingestExpected
    }

    var captureHealthy: Bool {
        if errorDetail == "screen_recording_not_granted" {
            return false
        }
        return fishermanRunning
    }

    var captureServiceName: String {
        switch captureBackend {
        case "swift":
            return "swift capture"
        case "native":
            return "native capture"
        default:
            return "\(captureBackend) capture"
        }
    }

    var captureServiceLabel: String {
        if errorDetail == "screen_recording_not_granted" {
            return "permission needed"
        }
        return fishermanRunning ? "active" : "waiting"
    }

    var captureServiceIcon: String {
        captureHealthy ? "checkmark.circle.fill" : "xmark.circle.fill"
    }

    var captureServiceColor: NSColor {
        captureHealthy ? .systemGreen : .systemRed
    }

    var fishermanServiceLabel: String {
        if !fishermanRunning {
            return "down"
        }
        if streamingEnabled {
            return fishermanConnected ? "connected" : disconnectedServiceLabel
        }
        if backendMode == "cloud" {
            return blockedServiceLabel
        }
        if backendMode == "self_hosted" {
            return "ingest disabled"
        }
        return "local only"
    }

    var fishermanServiceIcon: String {
        if fishermanHealthy {
            return "checkmark.circle.fill"
        }
        if fishermanRunning {
            return "exclamationmark.circle.fill"
        }
        return "xmark.circle.fill"
    }

    var fishermanServiceColor: NSColor {
        if fishermanHealthy {
            return .systemGreen
        }
        if fishermanRunning {
            return .systemOrange
        }
        return .systemRed
    }

    var primaryFrameLabel: String {
        streamingEnabled ? "Uploaded" : "Captured"
    }

    var primaryFrameCount: Int {
        streamingEnabled ? framesStreamed : framesSent
    }

    var secondaryFrameLabel: String {
        if uploadQueueUnbound > 0 {
            return "Needs review"
        }
        return ingestExpected ? "Queued" : "Dropped"
    }

    var secondaryFrameCount: Int {
        if uploadQueueUnbound > 0 {
            return uploadQueueUnbound
        }
        return ingestExpected ? uploadQueuePending : framesDropped
    }

    private var backendStatusDetail: String? {
        guard fishermanRunning else { return nil }
        if streamingEnabled && !fishermanConnected {
            return backendBlockDetail ?? disconnectedStatusText
        }
        if !streamingEnabled && backendMode == "cloud" {
            return backendBlockDetail ?? "Cloud setup incomplete"
        }
        if !streamingEnabled && backendMode == "self_hosted" {
            return "Ingest disabled"
        }
        return nil
    }

    var backendStatusHelpText: String? {
        guard status == .degraded else { return nil }
        if let detail = backendBlockDetail, !detail.isEmpty {
            if let action = backendBlockAction, !action.isEmpty {
                return "\(detail) \(action)."
            }
            return detail
        }
        if streamingEnabled && !fishermanConnected {
            return disconnectedStatusText
        }
        return nil
    }

    var captureStatusHelpText: String? {
        guard status == .degraded else { return nil }
        if errorDetail == "screen_recording_not_granted" {
            return "macOS Screen Recording permission is blocked for native capture."
        }
        return nil
    }

    var captureRepairButtonLabel: String {
        return "Restart Capture"
    }

    private var blockedServiceLabel: String {
        switch backendBlockCode {
        case "cloud_approval_required", "cloud_attestation_failed", "cloud_attestation_unreachable":
            return "approval needed"
        case "cloud_account_not_enabled":
            return "account pending"
        case "cloud_ingest_not_ready":
            return "cloud not ready"
        default:
            return "cloud setup needed"
        }
    }

    private var disconnectedServiceLabel: String {
        switch backendBlockCode {
        case "cloud_approval_required", "cloud_attestation_failed", "cloud_attestation_unreachable":
            return "approval needed"
        case "cloud_account_not_enabled":
            return "account pending"
        case "cloud_ingest_not_ready":
            return "cloud not ready"
        default:
            if backendMode == "cloud", let streamError, streamError.contains("403") {
                return "account rejected"
            }
            return backendMode == "self_hosted" ? "server down" : "ingest down"
        }
    }

    private var disconnectedStatusText: String {
        if backendMode == "cloud", let streamError, streamError.contains("403") {
            return "Cloud account is not enabled"
        }
        if backendMode == "cloud" {
            return "Cloud ingest disconnected"
        }
        if backendMode == "self_hosted" {
            return "Self-hosted ingest disconnected"
        }
        return "Ingest disconnected"
    }

    func update(fishermanStatus: [String: Any]?) {
        if let s = fishermanStatus {
            fishermanRunning = true
            captureBackend = (
                s["capture_backend"] as? String ?? captureBackend
            ).trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            backendMode = s["backend_mode"] as? String ?? backendMode
            streamingEnabled = s["streaming_enabled"] as? Bool ?? true
            fishermanConnected = s["connected"] as? Bool ?? false
            framesSent = s["frames_sent"] as? Int ?? 0
            framesStreamed = s["frames_streamed"] as? Int ?? (streamingEnabled ? framesSent : 0)
            framesDropped = s["frames_dropped"] as? Int ?? 0
            uploadQueuePending = s["upload_queue_pending"] as? Int ?? 0
            uploadQueueUnbound = s["upload_queue_unbound"] as? Int ?? 0
            backendBlockCode = s["backend_block_code"] as? String
            backendBlockDetail = s["backend_block_detail"] as? String
            backendBlockAction = s["backend_block_action"] as? String
            streamError = s["stream_error"] as? String
            isPaused = s["paused"] as? Bool ?? false
            errorDetail = s["error"] as? String
        } else {
            fishermanRunning = false
            fishermanConnected = false
            framesStreamed = 0
            uploadQueuePending = 0
            uploadQueueUnbound = 0
            backendBlockCode = nil
            backendBlockDetail = nil
            backendBlockAction = nil
            streamError = nil
        }

        // Derive overall status
        if isPaused {
            status = .paused
        } else if captureHealthy && fishermanHealthy {
            status = .running
            errorDetail = nil
        } else if captureHealthy || fishermanRunning {
            status = .degraded
        } else if status != .starting {
            status = .error
        }
    }
}
