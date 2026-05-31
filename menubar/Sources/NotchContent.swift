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
        HStack(spacing: 3) {
            if !state.incomingPokes.isEmpty {
                Text("👋")
                    .font(.system(size: 11))
                    .opacity(0.9)
            }

            // Keep sleeping friends in the pill (they now show 😴) instead of
            // dropping them when they go quiet — that vanishing felt lonely.
            // Only friends we've never seen a status from ("waiting") stay hidden.
            let compactActivities = state.allActivity.filter { user in
                user.id == "me" || user.category != "waiting"
            }
            // Emoji + flow badge only — duration/timeline live in expanded view
            // to keep the compact strip narrow and the menu bar visible.
            ForEach(compactActivities.prefix(5)) { user in
                let showFlow = user.inFlow
                HStack(spacing: 1) {
                    Text(user.emoji)
                        .font(.system(size: 12))
                        .shadow(
                            color: showFlow ? Color(nsColor: .systemRed).opacity(0.7) : .clear,
                            radius: showFlow ? 3 : 0
                        )
                    if showFlow {
                        Text("🔥")
                            .font(.system(size: 8))
                    }
                }
            }

            // Hangout suggestion in compact view
            if state.hangoutSuggestion != nil {
                Text("🍿")
                    .font(.system(size: 11))
                    .opacity(0.9)
            }

            if compactActivities.isEmpty {
                Text("\(state.primaryFrameCount)")
                    .font(.system(size: 9, design: .monospaced))
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
    let onRepairCapture: () -> Void
    let onSettings: () -> Void
    let onOpenCard: () -> Void
    let onOpenFriendCard: (UserActivity) -> Void
    let onPoke: (UserActivity) -> Void
    let onClearPokes: () -> Void
    let onQuit: () -> Void

    @State private var hoveredUserId: String?
    @State private var repairingCapture = false
    @State private var friendPreviewExpanded = false
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
            statusRow(
                name: state.captureServiceName,
                iconName: state.captureServiceIcon,
                color: Color(nsColor: state.captureServiceColor),
                label: state.captureServiceLabel
            )
            statusRow(
                name: "fisherman",
                iconName: state.fishermanServiceIcon,
                color: Color(nsColor: state.fishermanServiceColor),
                label: state.fishermanServiceLabel
            )

            if let help = state.captureStatusHelpText {
                HStack(alignment: .top, spacing: 6) {
                    Image(systemName: "arrow.turn.down.right")
                        .font(.system(size: 9))
                        .foregroundStyle(.secondary)
                    Text(help)
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                        .lineLimit(3)
                    Spacer()
                    Button(repairingCapture ? "Repairing" : state.captureRepairButtonLabel) {
                        repairingCapture = true
                        onRepairCapture()
                        DispatchQueue.main.asyncAfter(deadline: .now() + 4.0) {
                            repairingCapture = false
                        }
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.mini)
                    .disabled(repairingCapture)
                }
                .padding(.leading, 18)
            }

            if let help = state.backendStatusHelpText {
                HStack(alignment: .top, spacing: 6) {
                    Image(systemName: "arrow.turn.down.right")
                        .font(.system(size: 9))
                        .foregroundStyle(.secondary)
                    Text(help)
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                        .lineLimit(3)
                    Spacer()
                    Button("Fix") {
                        onSettings()
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.mini)
                }
                .padding(.leading, 18)
            }

            Divider()

            if !state.incomingPokes.isEmpty {
                HStack(spacing: 6) {
                    Text("👋")
                        .font(.system(size: 14))
                    VStack(alignment: .leading, spacing: 2) {
                        Text("\(state.incomingPokes.count) poke\(state.incomingPokes.count == 1 ? "" : "s")")
                            .font(.system(size: 12, weight: .medium))
                        Text(pokeSummary(state.incomingPokes))
                            .font(.system(size: 10))
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                    Spacer()
                    Button("Clear") {
                        onClearPokes()
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
                            Text(user.emoji).font(.system(size: 14))

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

                            if user.id != "me" {
                                Button {
                                    pokedUsers.insert(user.id)
                                    onPoke(user)
                                    DispatchQueue.main.asyncAfter(deadline: .now() + 3.0) {
                                        pokedUsers.remove(user.id)
                                    }
                                } label: {
                                    Text(pokedUsers.contains(user.id) ? "✓" : "👋")
                                        .font(.system(size: 10))
                                        .frame(width: 12, height: 12)
                                }
                                .buttonStyle(.borderless)
                                .disabled(pokedUsers.contains(user.id))
                                .help("Poke \(user.name)")

                                Button {
                                    onOpenFriendCard(user)
                                } label: {
                                    Image(systemName: "calendar.day.timeline.left")
                                        .font(.system(size: 10, weight: .medium))
                                        .frame(width: 12, height: 12)
                                }
                                .buttonStyle(.borderless)
                                .help("Open \(user.name)'s activity card")
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
                                .padding(.leading, 20)
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

            if !state.publishedFriendPreviews.isEmpty {
                sharedStatusPreview
            }

            Divider()

            // Frame stats
            HStack(spacing: 16) {
                statLabel(state.primaryFrameLabel, value: "\(state.primaryFrameCount)")
                statLabel(state.secondaryFrameLabel, value: "\(state.secondaryFrameCount)")
            }

            Divider()

            // Actions — icon buttons with tooltips. Card keeps its label as the
            // primary affordance; the rest are SF Symbols so nothing truncates
            // at the 300pt notch width.
            HStack(spacing: 6) {
                iconButton(
                    systemName: state.isPaused ? "play.fill" : "pause.fill",
                    help: state.isPaused ? "Resume capture" : "Pause capture",
                    action: onPauseResume
                )

                iconButton(
                    systemName: "square.grid.2x2",
                    help: "View captured frames",
                    action: onViewFrames
                )

                Button(action: onOpenCard) {
                    HStack(spacing: 4) {
                        Image(systemName: "calendar.day.timeline.left")
                        Text("Card")
                    }
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
                .help("Open today's Daily Card")

                iconButton(
                    systemName: "gearshape",
                    help: "Settings",
                    action: onSettings
                )

                Spacer()

                iconButton(
                    systemName: "power",
                    help: "Quit Fisherman",
                    tint: .red,
                    action: onQuit
                )
            }
        }
        .padding(12)
        .frame(width: 300)
    }

    @ViewBuilder
    private func iconButton(
        systemName: String,
        help: String,
        tint: Color? = nil,
        action: @escaping () -> Void
    ) -> some View {
        let btn = Button(action: action) {
            Image(systemName: systemName)
                .font(.system(size: 12, weight: .medium))
                .frame(width: 14, height: 14)
        }
        .buttonStyle(.bordered)
        .controlSize(.small)
        .help(help)
        if let tint {
            btn.tint(tint)
        } else {
            btn
        }
    }

    private func statusRow(name: String, iconName: String, color: Color, label: String) -> some View {
        HStack(spacing: 6) {
            Image(systemName: iconName)
                .foregroundStyle(color)
                .font(.system(size: 12))
            Text(name)
                .font(.system(size: 12, design: .monospaced))
            Spacer()
            Text(label)
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

    private var sharedStatusPreview: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 5) {
                Button {
                    withAnimation(.easeInOut(duration: 0.15)) {
                        friendPreviewExpanded.toggle()
                    }
                } label: {
                    Image(systemName: friendPreviewExpanded ? "chevron.down" : "chevron.right")
                        .font(.system(size: 8, weight: .semibold))
                        .frame(width: 10, height: 10)
                }
                .buttonStyle(.plain)
                .help(friendPreviewExpanded ? "Hide friend previews" : "Show friend previews")

                Image(systemName: "lock.fill")
                    .font(.system(size: 9))
                    .foregroundStyle(.secondary)
                Text("Friend preview")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(.secondary)
                Text("\(state.publishedFriendPreviews.count)")
                    .font(.system(size: 9, design: .monospaced))
                    .foregroundStyle(.tertiary)
                Spacer()
            }

            if friendPreviewExpanded {
                ForEach(state.publishedFriendPreviews.prefix(3)) { preview in
                    publishedPreviewRow(preview)
                }
            }
        }
        .padding(7)
        .background(Color.black.opacity(0.22))
        .overlay(
            RoundedRectangle(cornerRadius: 7)
                .stroke(Color.white.opacity(0.08), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: 7))
        .help("This is the encrypted activity status friends see. Friends receive the derived activity row and recent activity timeline, not screenshots or OCR.")
    }

    private func publishedPreviewRow(_ preview: PublishedFriendStatus) -> some View {
        let accent = Color(nsColor: ActivityCategory.from(preview.category).color)
        return VStack(alignment: .leading, spacing: 3) {
            HStack(alignment: .firstTextBaseline, spacing: 5) {
                Text("to \(preview.friend)")
                    .font(.system(size: 10, weight: .medium))
                    .lineLimit(1)
                Text(preview.audience)
                    .font(.system(size: 9))
                    .foregroundStyle(.secondary)
                Spacer(minLength: 4)
                if preview.published {
                    Text(preview.timestamp.map(relativeTime) ?? "unknown")
                        .font(.system(size: 9, design: .monospaced))
                        .foregroundStyle(preview.isStale ? Color.orange : Color(nsColor: .tertiaryLabelColor))
                } else {
                    Text("not yet")
                        .font(.system(size: 9, design: .monospaced))
                        .foregroundStyle(.orange)
                }
            }

            if preview.published {
                HStack(spacing: 4) {
                    Text(preview.emoji)
                        .font(.system(size: 10))
                    Text(preview.category)
                        .font(.system(size: 10, weight: .medium))
                        .foregroundStyle(accent)
                    if preview.flow {
                        Text("🔥")
                            .font(.system(size: 9))
                            .help("In the zone for 30+ min")
                    }
                    Text("— \(preview.status)")
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                .padding(.leading, 12)
            }
        }
        .padding(.horizontal, 7)
        .padding(.vertical, 5)
        .background(accent.opacity(preview.published ? 0.10 : 0.04))
        .overlay(
            RoundedRectangle(cornerRadius: 6)
                .stroke(accent.opacity(preview.published ? 0.18 : 0.08), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: 6))
    }

    private func relativeTime(_ date: Date) -> String {
        let seconds = Int(Date().timeIntervalSince(date))
        if seconds < 60 { return "now" }
        let minutes = seconds / 60
        if minutes < 60 { return "\(minutes)m ago" }
        let hours = minutes / 60
        return "\(hours)h ago"
    }

    private func pokeSummary(_ pokes: [Poke]) -> String {
        let names = Array(Set(pokes.map(\.fromName))).sorted()
        let display = names.prefix(3).joined(separator: ", ")
        if names.count > 3 {
            return "\(display) +\(names.count - 3) more"
        }
        return display
    }
}
