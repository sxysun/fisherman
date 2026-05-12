import AppKit
import CoreGraphics
import SwiftUI

/// First-launch wizard. Three steps — Welcome (with live Screen Recording
/// permission status), Identity (display name + ed25519 pubkey), and
/// Context Home (Local / Cloud / Self-Hosted). The user can finish or
/// skip at any time; either path marks onboarding complete so the wizard
/// doesn't reappear on next launch.
struct WelcomeWizard: View {
    let config: ConfigManager
    let onFinish: () -> Void

    @State private var stepIndex: Int = 0
    @State private var displayName: String
    @State private var chosenMode: String = "local"
    @State private var hasScreenRecording: Bool = CGPreflightScreenCaptureAccess()
    @State private var permissionPollTimer: Timer?
    @State private var didCopyPubkey: Bool = false

    private let steps = ["Welcome", "Identity", "Context Home"]

    init(config: ConfigManager, onFinish: @escaping () -> Void) {
        self.config = config
        self.onFinish = onFinish
        _displayName = State(initialValue: config.displayName)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()
            content
                .frame(maxWidth: .infinity, alignment: .topLeading)
                .padding(.horizontal, 28)
                .padding(.vertical, 24)
            Divider()
            footer
        }
        .frame(width: 520, height: 540)
        .onAppear(perform: startPermissionPolling)
        .onDisappear { permissionPollTimer?.invalidate() }
    }

    // MARK: Header (step pills)

    private var header: some View {
        HStack(spacing: 8) {
            ForEach(Array(steps.enumerated()), id: \.offset) { idx, label in
                stepPill(idx: idx, label: label)
                if idx < steps.count - 1 {
                    Rectangle()
                        .fill(Color.secondary.opacity(0.25))
                        .frame(width: 22, height: 1)
                }
            }
            Spacer()
            Text("Fisherman")
                .font(.system(size: 11, weight: .medium, design: .monospaced))
                .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 28)
        .padding(.vertical, 16)
    }

    private func stepPill(idx: Int, label: String) -> some View {
        let state = pillState(for: idx)
        return HStack(spacing: 6) {
            ZStack {
                Circle()
                    .fill(state.background)
                    .frame(width: 18, height: 18)
                if state == .done {
                    Image(systemName: "checkmark")
                        .font(.system(size: 9, weight: .bold))
                        .foregroundStyle(.white)
                } else {
                    Text("\(idx + 1)")
                        .font(.system(size: 10, weight: .semibold, design: .monospaced))
                        .foregroundStyle(state.foreground)
                }
            }
            Text(label)
                .font(.system(size: 11, weight: state == .active ? .semibold : .regular))
                .foregroundStyle(state == .pending ? .secondary : .primary)
        }
    }

    private enum PillState {
        case done, active, pending

        var background: Color {
            switch self {
            case .done: return .green
            case .active: return .accentColor
            case .pending: return Color.secondary.opacity(0.18)
            }
        }
        var foreground: Color {
            switch self {
            case .done: return .white
            case .active: return .white
            case .pending: return .secondary
            }
        }
    }

    private func pillState(for idx: Int) -> PillState {
        if idx < stepIndex { return .done }
        if idx == stepIndex { return .active }
        return .pending
    }

    // MARK: Content (3 step bodies)

    @ViewBuilder
    private var content: some View {
        switch stepIndex {
        case 0: welcomeStep
        case 1: identityStep
        default: contextHomeStep
        }
    }

    // Step 0 — Welcome + Screen Recording permission
    private var welcomeStep: some View {
        VStack(alignment: .leading, spacing: 18) {
            VStack(alignment: .leading, spacing: 8) {
                Text("Welcome to Fisherman.")
                    .font(.system(size: 28, weight: .semibold, design: .serif))
                Text("A private context home for your Mac. Captures your screen locally and turns it into ambient status you can share — with friends, or with scoped agents.")
                    .font(.system(size: 13))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            permissionCard

            Text("You can change any of this later in Settings. Nothing is sent anywhere until you finish this wizard.")
                .font(.system(size: 11))
                .foregroundStyle(.tertiary)
                .fixedSize(horizontal: false, vertical: true)

            Spacer()
        }
    }

    private var permissionCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 10) {
                Circle()
                    .fill(hasScreenRecording ? Color.green : Color.orange)
                    .frame(width: 10, height: 10)
                Text(hasScreenRecording
                     ? "Screen recording is granted."
                     : "Screen recording is required for capture.")
                    .font(.system(size: 13, weight: .medium))
                Spacer()
                if !hasScreenRecording {
                    Button("Open System Settings") {
                        openScreenRecordingSettings()
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                }
            }
            Text(hasScreenRecording
                 ? "Captures run every 5 seconds locally. They never leave your Mac unless you opt into Fisherman Cloud or self-hosted in step 3."
                 : "macOS needs your explicit permission. Open System Settings → Privacy & Security → Screen Recording and toggle Fisherman on. This view updates automatically.")
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(14)
        .background(
            RoundedRectangle(cornerRadius: 8)
                .fill(Color(nsColor: .controlBackgroundColor))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .strokeBorder(Color.secondary.opacity(0.2), lineWidth: 1)
        )
    }

    // Step 1 — Identity
    private var identityStep: some View {
        VStack(alignment: .leading, spacing: 18) {
            VStack(alignment: .leading, spacing: 6) {
                Text("Who you are to friends.")
                    .font(.system(size: 24, weight: .semibold, design: .serif))
                Text("A display name and a long-lived ed25519 key live in ~/.fisherman/.env. The key signs friend codes and agent tokens — it's how you stay you across reinstalls.")
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            VStack(alignment: .leading, spacing: 6) {
                Text("Display name")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(.secondary)
                TextField("Display name", text: $displayName)
                    .textFieldStyle(.roundedBorder)
                    .font(.system(size: 13))
                Text("Friends see this next to your status emoji. Pre-filled from your macOS account.")
                    .font(.system(size: 10))
                    .foregroundStyle(.tertiary)
            }

            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Text("Public key")
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(.secondary)
                    Spacer()
                    Button(didCopyPubkey ? "Copied ✓" : "Copy") {
                        let pasteboard = NSPasteboard.general
                        pasteboard.clearContents()
                        pasteboard.setString(config.publicKeyHex, forType: .string)
                        didCopyPubkey = true
                        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
                            didCopyPubkey = false
                        }
                    }
                    .buttonStyle(.borderless)
                    .font(.system(size: 11))
                }
                Text(config.publicKeyHex.isEmpty ? "(generating…)" : config.publicKeyHex)
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(.primary)
                    .padding(10)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(
                        RoundedRectangle(cornerRadius: 6)
                            .fill(Color.black.opacity(0.25))
                    )
                    .overlay(
                        RoundedRectangle(cornerRadius: 6)
                            .strokeBorder(Color.secondary.opacity(0.18), lineWidth: 1)
                    )
                    .textSelection(.enabled)
                Text("Never share the private half. The public key alone is safe to publish.")
                    .font(.system(size: 10))
                    .foregroundStyle(.tertiary)
            }

            Spacer()
        }
    }

    // Step 2 — Context Home
    private var contextHomeStep: some View {
        VStack(alignment: .leading, spacing: 18) {
            VStack(alignment: .leading, spacing: 6) {
                Text("Where context lives.")
                    .font(.system(size: 24, weight: .semibold, design: .serif))
                Text("Captures and transcripts can stay on this Mac (Local), live in an attested Fisherman Cloud, or run on your own server. You can switch later from Settings → Context Home.")
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            VStack(spacing: 8) {
                modeRow(
                    id: "local",
                    title: "Local Only",
                    detail: "Raw context stays on this Mac. Friend status still works through the encrypted relay. Recommended for first run.",
                    badge: "default"
                )
                modeRow(
                    id: "cloud",
                    title: "Fisherman Cloud",
                    detail: "Managed backend in a TDX-attested enclave. Each new release must be reviewed and re-approved before it can compute on your history.",
                    badge: "attested"
                )
                modeRow(
                    id: "self_hosted",
                    title: "Self-Hosted",
                    detail: "Same backend image on your own server. Configure the URL after finishing — Settings → Context Home.",
                    badge: "sovereign"
                )
            }

            Spacer()
        }
    }

    private func modeRow(id: String, title: String, detail: String, badge: String) -> some View {
        let selected = chosenMode == id
        return Button {
            chosenMode = id
        } label: {
            HStack(alignment: .top, spacing: 12) {
                ZStack {
                    Circle()
                        .strokeBorder(selected ? Color.accentColor : Color.secondary.opacity(0.4), lineWidth: 1.5)
                        .frame(width: 16, height: 16)
                    if selected {
                        Circle()
                            .fill(Color.accentColor)
                            .frame(width: 8, height: 8)
                    }
                }
                .padding(.top, 2)

                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: 8) {
                        Text(title)
                            .font(.system(size: 13, weight: .semibold))
                        Text(badge)
                            .font(.system(size: 9, weight: .semibold, design: .monospaced))
                            .foregroundStyle(.secondary)
                            .padding(.horizontal, 6)
                            .padding(.vertical, 2)
                            .background(
                                Capsule()
                                    .strokeBorder(Color.secondary.opacity(0.3), lineWidth: 1)
                            )
                    }
                    Text(detail)
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer()
            }
            .padding(12)
            .background(
                RoundedRectangle(cornerRadius: 8)
                    .fill(selected ? Color.accentColor.opacity(0.08) : Color(nsColor: .controlBackgroundColor))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 8)
                    .strokeBorder(
                        selected ? Color.accentColor.opacity(0.6) : Color.secondary.opacity(0.18),
                        lineWidth: 1
                    )
            )
        }
        .buttonStyle(.plain)
    }

    // MARK: Footer (skip / back / continue)

    private var footer: some View {
        HStack {
            if stepIndex > 0 {
                Button("Back") { stepIndex -= 1 }
                    .buttonStyle(.bordered)
            } else {
                Button("Skip for now") { skip() }
                    .buttonStyle(.borderless)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Text(stepIndex == 0 ? "Step 1 of 3 · Welcome"
                 : stepIndex == 1 ? "Step 2 of 3 · Identity"
                 : "Step 3 of 3 · Context Home")
                .font(.system(size: 10, design: .monospaced))
                .foregroundStyle(.tertiary)
            Spacer()
            Button(stepIndex == steps.count - 1 ? "Finish" : "Continue") {
                advance()
            }
            .buttonStyle(.borderedProminent)
            .keyboardShortcut(.defaultAction)
        }
        .padding(.horizontal, 28)
        .padding(.vertical, 14)
    }

    private func advance() {
        if stepIndex < steps.count - 1 {
            stepIndex += 1
            return
        }
        finish()
    }

    private func finish() {
        config.displayName = displayName.trimmingCharacters(in: .whitespacesAndNewlines)
        if config.displayName.isEmpty {
            config.displayName = NSFullUserName().components(separatedBy: " ").first ?? NSUserName()
        }
        config.backendMode = chosenMode
        if chosenMode == "local" {
            config.backendURL = ""
            config.serverURL = "ws://localhost:9999/ingest"
            config.cloudIngestStatus = ""
        }
        config.completeOnboarding()
        onFinish()
    }

    private func skip() {
        // Keep whatever the install.sh defaults were; just mark onboarded
        // so the wizard doesn't keep firing. The user can revisit any of
        // this from Settings.
        config.completeOnboarding()
        onFinish()
    }

    // MARK: Screen-recording permission polling

    private func startPermissionPolling() {
        permissionPollTimer?.invalidate()
        permissionPollTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { _ in
            let current = CGPreflightScreenCaptureAccess()
            DispatchQueue.main.async {
                if current != hasScreenRecording {
                    hasScreenRecording = current
                }
            }
        }
    }

    private func openScreenRecordingSettings() {
        // Trigger the permission prompt once so Fisherman appears in the
        // list, then open the pane. Belt-and-braces — the prompt is
        // idempotent.
        _ = CGRequestScreenCaptureAccess()
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture") {
            NSWorkspace.shared.open(url)
        }
    }
}
