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

// MARK: - Compact Trailing (emoji row + timeline + duration)

struct CompactTrailing: View {
    let state: AppState

    var body: some View {
        HStack(spacing: 4) {
            // Poke indicator
            if !state.incomingPokes.isEmpty {
                Text("👋")
                    .font(.system(size: 12))
                    .opacity(0.9)
            }

            ForEach(state.allActivity.prefix(5)) { user in
                HStack(spacing: 2) {
                    // Emoji with flow/working-together indicators
                    Text(user.emoji)
                        .font(.system(size: 14))
                        .shadow(
                            color: user.inFlow
                                ? Color(nsColor: .systemRed).opacity(0.7)
                                : user.isWorkingTogether
                                    ? Color(nsColor: .systemYellow).opacity(0.8)
                                    : .clear,
                            radius: (user.inFlow || user.isWorkingTogether) ? 4 : 0
                        )

                    // Flow indicator
                    if user.inFlow {
                        Text("🔥")
                            .font(.system(size: 8))
                    }

                    // Session duration
                    if !user.sessionDurationText.isEmpty {
                        Text(user.sessionDurationText)
                            .font(.system(size: 8, weight: .medium, design: .monospaced))
                            .foregroundStyle(.secondary)
                    }

                    // Mini timeline bar
                    if !user.history.isEmpty {
                        TimelineBar(history: user.history)
                            .frame(width: 36, height: 4)
                    }
                }
            }

            if state.allActivity.isEmpty {
                Text("\(state.framesSent)")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
        }
    }
}

// MARK: - Timeline Bar

struct TimelineBar: View {
    let history: [ActivityEntry]

    var body: some View {
        GeometryReader { geo in
            HStack(spacing: 0.5) {
                ForEach(history.prefix(8).reversed()) { entry in
                    RoundedRectangle(cornerRadius: 1)
                        .fill(Color(nsColor: ActivityCategory.from(entry.category).color))
                        .frame(maxWidth: .infinity)
                }
            }
            .clipShape(RoundedRectangle(cornerRadius: 2))
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
    var onPoke: ((String) -> Void)?       // friend name -> send poke
    var onClearPokes: (() -> Void)?

    @State private var hoveredUserId: String?
    @State private var pokedUsers: Set<String> = []

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

            // Incoming pokes
            if !state.incomingPokes.isEmpty {
                HStack(spacing: 6) {
                    Text("👋")
                        .font(.system(size: 14))
                    Text("\(state.incomingPokes.count) poke\(state.incomingPokes.count == 1 ? "" : "s")")
                        .font(.system(size: 12, weight: .medium))
                    Spacer()
                    Button("Clear") {
                        onClearPokes?()
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.mini)
                }
                .padding(6)
                .background(Color(nsColor: .systemYellow).opacity(0.15))
                .clipShape(RoundedRectangle(cornerRadius: 6))
            }

            // Hangout suggestion
            if let suggestion = state.hangoutSuggestion {
                HStack(spacing: 6) {
                    Text("🍿")
                        .font(.system(size: 12))
                    Text(suggestion)
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                }
                .padding(6)
                .background(Color(nsColor: .systemGreen).opacity(0.1))
                .clipShape(RoundedRectangle(cornerRadius: 6))
            }

            // Multi-user activity list
            if !state.allActivity.isEmpty {
                ForEach(state.allActivity) { user in
                    VStack(alignment: .leading, spacing: 4) {
                        // Main activity row
                        HStack(spacing: 6) {
                            // Working-together badge
                            if user.isWorkingTogether {
                                Text("🤝")
                                    .font(.system(size: 10))
                            }

                            Text(user.emoji).font(.system(size: 14))
                                .shadow(
                                    color: user.isWorkingTogether
                                        ? Color(nsColor: .systemYellow).opacity(0.6)
                                        : .clear,
                                    radius: user.isWorkingTogether ? 3 : 0
                                )

                            Text(user.name)
                                .font(.system(size: 12, weight: .medium))

                            Text(user.category)
                                .font(.system(size: 11))
                                .foregroundStyle(Color(nsColor: ActivityCategory.from(user.category).color))

                            if !user.sessionDurationText.isEmpty {
                                Text("— \(user.sessionDurationText)")
                                    .font(.system(size: 11))
                                    .foregroundStyle(.secondary)
                            }

                            // Flow state badge
                            if user.inFlow {
                                Text("🔥")
                                    .font(.system(size: 10))
                                    .help("In the zone for 30+ min")
                            }

                            Spacer()

                            // Poke button (for friends, not "me")
                            if user.id != "me" {
                                Button(action: {
                                    pokedUsers.insert(user.id)
                                    onPoke?(user.name)
                                    // Reset after 3s
                                    DispatchQueue.main.asyncAfter(deadline: .now() + 3) {
                                        pokedUsers.remove(user.id)
                                    }
                                }) {
                                    Text(pokedUsers.contains(user.id) ? "✓" : "👋")
                                        .font(.system(size: 10))
                                }
                                .buttonStyle(.bordered)
                                .controlSize(.mini)
                                .disabled(pokedUsers.contains(user.id))
                            }

                            // Timeline bar in expanded view
                            if !user.history.isEmpty {
                                TimelineBar(history: user.history)
                                    .frame(width: 48, height: 4)
                            }
                        }

                        // Status line
                        if !user.status.isEmpty {
                            Text(user.status)
                                .font(.system(size: 11))
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                                .padding(.leading, user.isWorkingTogether ? 26 : 20)
                        }

                        // Hover history expansion
                        if hoveredUserId == user.id, !user.history.isEmpty {
                            VStack(alignment: .leading, spacing: 3) {
                                ForEach(user.history.prefix(5)) { entry in
                                    HStack(spacing: 4) {
                                        Text(entry.emoji).font(.system(size: 10))
                                        Text(entry.category)
                                            .font(.system(size: 10, weight: .medium))
                                            .foregroundStyle(Color(nsColor: ActivityCategory.from(entry.category).color))
                                        Text("— \(entry.status)")
                                            .font(.system(size: 10))
                                            .foregroundStyle(.secondary)
                                            .lineLimit(1)
                                        Spacer()
                                        Text(relativeTime(entry.timestamp))
                                            .font(.system(size: 9, design: .monospaced))
                                            .foregroundStyle(.tertiary)
                                    }
                                }
                            }
                            .padding(.leading, 20)
                            .padding(.top, 2)
                            .transition(.opacity.combined(with: .move(edge: .top)))
                        }
                    }
                    .opacity(user.stale ? 0.5 : 1.0)
                    .padding(.vertical, 2)
                    .contentShape(Rectangle())
                    .onHover { isHovering in
                        withAnimation(.easeInOut(duration: 0.15)) {
                            hoveredUserId = isHovering ? user.id : nil
                        }
                    }
                }
            } else {
                Text("No activity yet")
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
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
        .frame(width: 300)
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

    private func relativeTime(_ date: Date) -> String {
        let seconds = Int(Date().timeIntervalSince(date))
        if seconds < 60 { return "now" }
        let minutes = seconds / 60
        if minutes < 60 { return "\(minutes)m ago" }
        let hours = minutes / 60
        return "\(hours)h ago"
    }
}
