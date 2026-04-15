import SwiftUI

// MARK: - Compact Leading (status dot)

struct CompactLeading: View {
    let state: AppState

    var body: some View {
        Circle()
            .fill(Color(nsColor: state.statusColor))
            .frame(width: 8, height: 8)
    }
}

// MARK: - Compact Trailing (frame count)

struct CompactTrailing: View {
    let state: AppState

    var body: some View {
        HStack(spacing: 4) {
            // Pixel character (placeholder emoji)
            Text(characterEmoji(for: state.activityCategory))
                .font(.system(size: 16))

            Text("\(state.framesSent)")
                .font(.system(size: 10, design: .monospaced))
                .foregroundStyle(.secondary)
        }
    }

    private func characterEmoji(for category: String?) -> String {
        switch category {
        case "coding": return "👨‍💻"
        case "reading": return "📖"
        case "browsing": return "🔍"
        case "idle": return "😴"
        default: return "❓"
        }
    }
}

// MARK: - Expanded panel

struct ExpandedContent: View {
    let state: AppState
    let onPauseResume: () -> Void
    let onViewFrames: () -> Void
    let onSettings: () -> Void
    let onQuit: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            // Header
            HStack {
                Circle()
                    .fill(Color(nsColor: state.statusColor))
                    .frame(width: 10, height: 10)
                Text(state.statusText)
                    .font(.system(size: 13, weight: .semibold))
                Spacer()
            }

            Divider()

            // Process rows
            processRow(name: "screenpipe", ok: state.screenpipeHealthy)
            processRow(name: "fisherman", ok: state.fishermanRunning && state.fishermanConnected)

            Divider()

            // Activity status (NEW)
            if let category = state.activityCategory, let status = state.currentActivity {
                HStack(spacing: 6) {
                    Text(characterEmoji(for: category))
                        .font(.system(size: 14))
                    Text("\(category): \(status)")
                        .font(.system(size: 12))
                        .lineLimit(1)
                    Spacer()
                }
                .padding(.vertical, 2)
            }

            Divider()

            // Frame stats
            HStack(spacing: 16) {
                statLabel("Sent", value: "\(state.framesSent)")
                statLabel("Dropped", value: "\(state.framesDropped)")
            }

            Divider()

            // Actions
            HStack(spacing: 8) {
                Button(state.isPaused ? "Resume" : "Pause") {
                    onPauseResume()
                }
                .buttonStyle(.bordered)
                .controlSize(.small)

                Button("View Frames") {
                    onViewFrames()
                }
                .buttonStyle(.bordered)
                .controlSize(.small)

                Button("Settings") {
                    onSettings()
                }
                .buttonStyle(.bordered)
                .controlSize(.small)

                Spacer()

                Button("Quit") {
                    onQuit()
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .tint(.red)
            }
        }
        .padding(12)
        .frame(width: 280)
    }

    private func processRow(name: String, ok: Bool) -> some View {
        HStack(spacing: 6) {
            Image(systemName: ok ? "checkmark.circle.fill" : "xmark.circle.fill")
                .foregroundStyle(ok ? .green : .red)
                .font(.system(size: 12))
            Text(name)
                .font(.system(size: 12, design: .monospaced))
            Spacer()
            Text(ok ? "healthy" : "down")
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
        }
    }

    private func statLabel(_ label: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label)
                .font(.system(size: 10))
                .foregroundStyle(.tertiary)
            Text(value)
                .font(.system(size: 13, weight: .medium, design: .monospaced))
        }
    }

    private func characterEmoji(for category: String?) -> String {
        switch category {
        case "coding": return "👨‍💻"
        case "reading": return "📖"
        case "browsing": return "🔍"
        case "idle": return "😴"
        default: return "❓"
        }
    }
}
