import SwiftUI

// MARK: - Expanded content (shown when DynamicNotch is in .expanded state)

struct HarnessExpanded: View {
    @ObservedObject var state: HarnessState

    var body: some View {
        Group {
            if let p = state.current {
                pillBody(for: p)
            } else {
                EmptyView()
            }
        }
    }

    @ViewBuilder
    private func pillBody(for p: PendingPayload) -> some View {
        HStack(spacing: 14) {
            StatusDot()
                .padding(.leading, 2)

            VStack(alignment: .leading, spacing: 3) {
                if let intent = p.intent, !intent.isEmpty {
                    Text(intent.replacingOccurrences(of: "_", with: " ").uppercased())
                        .font(.system(size: 9, weight: .semibold))
                        .tracking(0.9)
                        .foregroundStyle(.white.opacity(0.48))
                }
                Text(p.message)
                    .font(.system(size: 13, weight: .regular))
                    .foregroundStyle(.white.opacity(0.94))
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            HStack(spacing: 6) {
                HarnessButton(label: "Yes", style: .accent,
                              onHover: { state.hoverHandler?("yes", $0) },
                              onTap:   { state.actionHandler?("clicked") })
                HarnessButton(label: "Later", style: .ghost,
                              onHover: { state.hoverHandler?("later", $0) },
                              onTap:   { state.actionHandler?("snoozed") })
                HarnessButton(label: "✕", style: .iconGhost,
                              onHover: { state.hoverHandler?("dismiss", $0) },
                              onTap:   { state.actionHandler?("dismissed") })
            }
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 14)
        .frame(minWidth: 520, idealWidth: 560, maxWidth: 620, minHeight: 70)
    }
}

// MARK: - Compact strips (shown next to the system notch in compact state)

struct HarnessCompactLeading: View {
    @ObservedObject var state: HarnessState
    var body: some View {
        StatusDot().padding(.leading, 4)
    }
}

struct HarnessCompactTrailing: View {
    @ObservedObject var state: HarnessState
    var body: some View {
        let intent = state.current?.intent ?? ""
        Group {
            if !intent.isEmpty {
                Text(intent.replacingOccurrences(of: "_", with: " ").uppercased())
                    .font(.system(size: 9, weight: .semibold))
                    .tracking(0.8)
                    .foregroundStyle(.white.opacity(0.55))
                    .padding(.trailing, 4)
            } else {
                EmptyView()
            }
        }
    }
}

// MARK: - Small components

struct StatusDot: View {
    @State private var pulse = false
    var body: some View {
        Circle()
            .fill(LinearGradient(
                colors: [Color(hex: 0xE8D8A8), Color(hex: 0xB89E68)],
                startPoint: .top, endPoint: .bottom
            ))
            .frame(width: 8, height: 8)
            .shadow(color: Color(hex: 0xE8D8A8).opacity(0.6), radius: 4)
            .opacity(pulse ? 1.0 : 0.55)
            .scaleEffect(pulse ? 1.0 : 0.85)
            .animation(.easeInOut(duration: 1.4).repeatForever(autoreverses: true), value: pulse)
            .onAppear { pulse = true }
    }
}

enum HarnessButtonStyle { case accent, ghost, iconGhost }

struct HarnessButton: View {
    let label: String
    let style: HarnessButtonStyle
    let onHover: (Bool) -> Void
    let onTap: () -> Void

    @State private var hover = false
    @State private var pressed = false

    var body: some View {
        Button(action: {
            withAnimation(.easeOut(duration: 0.08)) { pressed = true }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.08) {
                pressed = false
                onTap()
            }
        }) {
            Text(label)
                .font(.system(size: 11.5, weight: style == .accent ? .semibold : .medium))
                .foregroundStyle(foreground)
                .padding(.horizontal, style == .iconGhost ? 9 : 13)
                .padding(.vertical, 6)
                .background(
                    Capsule()
                        .fill(background)
                        .overlay(
                            Capsule()
                                .strokeBorder(borderColor, lineWidth: 0.5)
                        )
                )
                .scaleEffect(pressed ? 0.94 : 1.0)
        }
        .buttonStyle(.plain)
        .onHover { h in
            withAnimation(.easeOut(duration: 0.14)) { hover = h }
            onHover(h)
        }
    }

    private var foreground: Color {
        switch style {
        case .accent:    return Color(hex: 0x1a1408)
        case .ghost:     return Color.white.opacity(0.85)
        case .iconGhost: return Color.white.opacity(0.55)
        }
    }
    private var background: Color {
        switch style {
        case .accent:    return hover ? Color(hex: 0xF0DFA8) : Color(hex: 0xE8D8A8)
        case .ghost:     return hover ? Color.white.opacity(0.14) : Color.white.opacity(0.07)
        case .iconGhost: return hover ? Color.white.opacity(0.14) : Color.clear
        }
    }
    private var borderColor: Color {
        switch style {
        case .accent:    return Color.white.opacity(0.2)
        case .ghost:     return Color.white.opacity(0.08)
        case .iconGhost: return Color.clear
        }
    }
}

// MARK: - Hex helper

extension Color {
    init(hex: UInt32, alpha: Double = 1.0) {
        let r = Double((hex >> 16) & 0xFF) / 255.0
        let g = Double((hex >> 8) & 0xFF) / 255.0
        let b = Double(hex & 0xFF) / 255.0
        self.init(.sRGB, red: r, green: g, blue: b, opacity: alpha)
    }
}
