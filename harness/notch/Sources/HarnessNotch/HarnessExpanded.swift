import AppKit
import SwiftUI

// MARK: - Expanded content (shown when the harness surface is open)

struct HarnessExpanded: View {
    @ObservedObject var state: HarnessState
    @State private var dashboardData: JSON?
    @State private var evalData: JSON?
    @State private var dietData: JSON?
    @State private var loadingPanel: HarnessNotchPanel?
    @State private var loadError: String?
    @State private var refreshedAt: Date?

    var body: some View {
        inspectorBody
        .task { await refreshActivePanel() }
        .onChange(of: state.activePanel) {
            Task { await refreshActivePanel() }
        }
    }

    @ViewBuilder
    private var inspectorBody: some View {
        VStack(alignment: .leading, spacing: 12) {
            header

            switch state.activePanel {
            case .ping:
                if let p = state.current {
                    pingPanel(for: p)
                } else {
                    emptyPanel("No active ping.")
                }
            case .pipeline:
                PipelineNotchPanel(
                    dashboard: dashboardData,
                    eval: evalData,
                    loading: loadingPanel == .pipeline,
                    error: loadError,
                    refreshedAt: refreshedAt,
                    refresh: { Task { await refreshActivePanel(force: true) } }
                )
            case .diet:
                DietNotchPanel(
                    diet: dietData,
                    loading: loadingPanel == .diet,
                    error: loadError,
                    refreshedAt: refreshedAt,
                    refresh: { Task { await refreshActivePanel(force: true) } }
                )
            case .settings:
                SettingsNotchPanel()
            }
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 15)
        .frame(width: state.activePanel == .ping ? 620 : 780, alignment: .topLeading)
    }

    private var header: some View {
        HStack(spacing: 12) {
            HStack(spacing: 12) {
                StatusDot()
                VStack(alignment: .leading, spacing: 2) {
                    Text("Harness")
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(.white.opacity(0.94))
                    Text(headerSubtitle)
                        .font(.system(size: 10.5))
                        .foregroundStyle(.white.opacity(0.48))
                        .lineLimit(1)
                }
            }
            .notchDragHandle(state)
            .help("Drag to move the harness capsule")

            Spacer(minLength: 12)

            HarnessPanelSwitcher(
                panels: availablePanels,
                selected: state.activePanel,
                select: { state.activePanel = $0 }
            )
            .frame(width: state.current == nil ? 282 : 360)

            Button(action: { state.togglePinHandler?() }) {
                Image(systemName: state.surfacePinned ? "pin.fill" : "pin")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(state.surfacePinned ? Color(hex: 0xE8D8A8) : .white.opacity(0.62))
                    .frame(width: 27, height: 24)
                    .background(Capsule().fill(Color.white.opacity(0.08)))
            }
            .buttonStyle(.plain)
            .help(state.surfacePinned ? "Unpin" : "Pin open")
        }
    }

    private var availablePanels: [HarnessNotchPanel] {
        state.current == nil ? [.pipeline, .diet, .settings] : HarnessNotchPanel.allCases
    }

    private var headerSubtitle: String {
        if let p = state.current {
            let intent = p.intent?.replacingOccurrences(of: "_", with: " ") ?? "notification"
            return "live ping · \(intent)"
        }
        return "hover-expanded pipeline inspector"
    }

    private func pingPanel(for p: PendingPayload) -> some View {
        HStack(spacing: 14) {
            VStack(alignment: .leading, spacing: 6) {
                if let intent = p.intent, !intent.isEmpty {
                    Text(intent.replacingOccurrences(of: "_", with: " ").uppercased())
                        .font(.system(size: 9.5, weight: .semibold))
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
        .padding(12)
        .frame(minHeight: 68)
        .background(NotchCardBackground())
    }

    private func emptyPanel(_ text: String) -> some View {
        Text(text)
            .font(.system(size: 12))
            .foregroundStyle(.white.opacity(0.52))
            .frame(maxWidth: .infinity, minHeight: 64, alignment: .leading)
            .padding(12)
            .background(NotchCardBackground())
    }

    @MainActor
    private func refreshActivePanel(force: Bool = false) async {
        switch state.activePanel {
        case .ping:
            return
        case .pipeline:
            if !force, dashboardData != nil, evalData != nil { return }
            loadingPanel = .pipeline
            loadError = nil
            async let dashboard = HarnessAPI.fetchData(window: "24h")
            async let eval = HarnessAPI.fetchEvalReport(window: "24h", maxExamples: 4)
            let (dashboardResult, evalResult) = await (dashboard, eval)
            dashboardData = dashboardResult
            evalData = evalResult
            if dashboardResult == nil && evalResult == nil {
                loadError = "Pipeline data unavailable"
            }
            refreshedAt = Date()
            loadingPanel = nil
        case .diet:
            if !force, dietData != nil { return }
            loadingPanel = .diet
            loadError = nil
            let diet = await HarnessAPI.fetchInformationDiet(window: "24h", maxEpisodes: 6)
            dietData = diet
            if diet == nil { loadError = "Diet data unavailable" }
            refreshedAt = Date()
            loadingPanel = nil
        case .settings:
            return
        }
    }
}

private struct HarnessPanelSwitcher: View {
    let panels: [HarnessNotchPanel]
    let selected: HarnessNotchPanel
    let select: (HarnessNotchPanel) -> Void

    var body: some View {
        HStack(spacing: 3) {
            ForEach(panels) { panel in
                Button(action: { select(panel) }) {
                    Text(panel.rawValue)
                        .font(.system(size: 10.5, weight: .semibold))
                        .foregroundStyle(panel == selected ? Color(hex: 0x1a1408) : .white.opacity(0.68))
                        .frame(maxWidth: .infinity)
                        .frame(height: 24)
                        .background(
                            Capsule()
                                .fill(panel == selected ? Color(hex: 0xE8D8A8) : Color.white.opacity(0.075))
                        )
                }
                .buttonStyle(.plain)
                .help("Show \(panel.rawValue)")
            }
        }
        .padding(3)
        .background(Capsule().fill(Color.white.opacity(0.055)))
        .overlay(Capsule().strokeBorder(Color.white.opacity(0.08), lineWidth: 0.5))
    }
}

struct HarnessFloatingSurface: View {
    @ObservedObject var state: HarnessState

    var body: some View {
        ZStack(alignment: .topLeading) {
            HarnessExpanded(state: state)
                .background(FloatingSurfaceBackground(radius: 18))
                .shadow(color: .black.opacity(0.35), radius: 18, x: 0, y: 8)
                .opacity(state.surfaceExpanded ? 1 : 0)
                .allowsHitTesting(state.surfaceExpanded)

            HarnessFloatingCompact(state: state)
                .background(FloatingSurfaceBackground(radius: 18))
                .shadow(color: .black.opacity(0.32), radius: 12, x: 0, y: 5)
                .opacity(state.surfaceExpanded ? 0 : 1)
                .allowsHitTesting(!state.surfaceExpanded)
        }
        .frame(width: surfaceSize.width, height: surfaceSize.height, alignment: .topLeading)
        .clipped()
        .onHover { state.surfaceHoverHandler?($0) }
    }

    private var surfaceSize: CGSize {
        if !state.surfaceExpanded {
            return CGSize(width: 132, height: 38)
        }
        switch state.activePanel {
        case .ping:
            return CGSize(width: 620, height: 176)
        case .pipeline:
            return CGSize(width: 780, height: 384)
        case .diet:
            return CGSize(width: 780, height: 418)
        case .settings:
            return CGSize(width: 780, height: 618)
        }
    }
}

private struct HarnessFloatingCompact: View {
    @ObservedObject var state: HarnessState

    var body: some View {
        HStack(spacing: 9) {
            StatusDot()
            Text(compactLabel)
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(.white.opacity(0.74))
                .lineLimit(1)
            if state.current != nil {
                Circle()
                    .fill(Color(hex: 0xE8D8A8))
                    .frame(width: 5, height: 5)
            }
        }
        .padding(.horizontal, 14)
        .frame(width: 132, height: 38)
        .contentShape(Rectangle())
        .notchDragHandle(state)
        .onTapGesture { state.surfaceHoverHandler?(true) }
        .help("Drag to move. Hover to expand.")
    }

    private var compactLabel: String {
        if let intent = state.current?.intent, !intent.isEmpty {
            return intent.replacingOccurrences(of: "_", with: " ").uppercased()
        }
        return state.activePanel.rawValue.uppercased()
    }
}

private struct PipelineNotchPanel: View {
    let dashboard: JSON?
    let eval: JSON?
    let loading: Bool
    let error: String?
    let refreshedAt: Date?
    let refresh: () -> Void

    var body: some View {
        let evalData = eval?["data"]
        let nPings = evalData?["n_pings"].int ?? 0
        VStack(alignment: .leading, spacing: 12) {
            panelToolbar(title: "Pipeline and eval", detail: updatedText, loading: loading, refresh: refresh)
            NotchRail(stages: [
                NotchStage("Observe", "\(dashboard?["n_candidates"].int ?? 0)", "candidates"),
                NotchStage("Gate", "\(dashboard?["n_decisions"].int ?? 0)", "decisions"),
                NotchStage("Ping", "\(evalData?["n_pings"].int ?? 0)", "eligible"),
                NotchStage("Claim", "\(evalData?["n_claimed_pings"].int ?? 0)", "shown"),
                NotchStage("Outcome", "\(evalData?["n_outcomes"].int ?? 0)", "captured"),
            ])

            HStack(spacing: 8) {
                NotchMetric(label: "claimed capture", value: nPings == 0 ? "no pings" : notchPct(evalData?["outcome_capture_rate_for_claimed_pings"]))
                NotchMetric(label: "implicit usable", value: "\(evalData?["n_implicit_usable"].int ?? 0)")
                NotchMetric(label: "explicit labels", value: "\(evalData?["n_explicit_labels"].int ?? 0)")
                NotchMetric(label: "label coverage", value: notchPct(evalData?["explicit_label_coverage"]))
            }

            if nPings == 0 {
                NotchBanner("No pings were delivered in this 24h window, so ping capture, implicit ping labels, and recent ping examples are empty.")
            }

            HStack(alignment: .top, spacing: 10) {
                NotchMiniSection(title: "Recent misses") {
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(Array(exampleRows.prefix(3).enumerated()), id: \.offset) { _, row in
                            NotchExampleRow(row: row)
                        }
                        if exampleRows.isEmpty {
                            NotchMutedText(error ?? (nPings == 0 ? "No ping examples because Ping/Claim are 0 in this window." : "No non-green examples in this window."))
                        }
                    }
                }
            }
        }
    }

    private var updatedText: String {
        guard let refreshedAt else { return "24h live window" }
        return "24h live window · refreshed \(notchTime(refreshedAt))"
    }

    private var exampleRows: [[String: Any]] {
        (eval?["examples"].list ?? []).compactMap { $0 as? [String: Any] }
    }
}

private struct SettingsNotchPanel: View {
    @StateObject private var model = SettingsModel()
    @State private var loading = true
    @State private var saving = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            panelToolbar(title: "Settings", detail: detailText, loading: loading || saving, refresh: {
                Task { await refresh() }
            })

            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    SettingsBand(title: "Today") {
                        TextEditor(text: $model.dailyGoal)
                            .font(.system(size: 12.5))
                            .foregroundStyle(.white.opacity(0.92))
                            .scrollContentBackground(.hidden)
                            .padding(8)
                            .frame(minHeight: 74, maxHeight: 94)
                            .background(NotchInputBackground())

                        HStack(spacing: 6) {
                            ForEach(["gentle", "balanced", "responsive"], id: \.self) { value in
                                SettingsChip(
                                    title: value.capitalized,
                                    selected: model.sensitivity == value,
                                    action: { model.sensitivity = value }
                                )
                            }
                        }
                    }

                    SettingsBand(title: "Policy") {
                        HStack(spacing: 8) {
                            SettingsChip(
                                title: "LLM learner",
                                selected: model.activePolicy == "llm_icl_v0" && model.policyLearnerEnabled,
                                action: {
                                    model.activePolicy = "llm_icl_v0"
                                    model.policyLearnerEnabled = true
                                }
                            )
                            SettingsChip(
                                title: "Rule baseline",
                                selected: model.activePolicy == "rule_v0",
                                action: {
                                    model.activePolicy = "rule_v0"
                                    model.policyLearnerEnabled = false
                                }
                            )
                            Spacer(minLength: 8)
                            NotchMutedText(model.activePolicy)
                        }

                        SettingsSlider(
                            title: "Explore ping rate",
                            value: $model.explorePingRate,
                            range: 0...0.12,
                            step: 0.01,
                            format: { "\(Int($0 * 100))%" }
                        )
                        SettingsSlider(
                            title: "Min confidence to ping",
                            value: $model.policyLearnerMinConfidence,
                            range: 0.35...0.90,
                            step: 0.05,
                            format: { "\(Int($0 * 100))%" }
                        )
                    }

                    SettingsBand(title: "Learner endpoint") {
                        SettingsTextField(title: "Base URL", text: $model.policyLearnerBaseURL)
                        SettingsTextField(title: "Model", text: $model.policyLearnerModel)
                        SettingsSecretField(title: "API key", text: $model.policyLearnerApiKey)
                        HStack(spacing: 10) {
                            Stepper("Examples \(model.policyLearnerMaxExamples)", value: $model.policyLearnerMaxExamples, in: 0...64, step: 2)
                            Stepper("Call gap \(model.policyLearnerMinInterval)s", value: $model.policyLearnerMinInterval, in: 0...120, step: 5)
                        }
                        .font(.system(size: 11))
                        .foregroundStyle(.white.opacity(0.72))
                    }

                    SettingsBand(title: "Message model") {
                        SettingsTextField(title: "Base URL", text: $model.realizerBaseURL)
                        SettingsTextField(title: "Model", text: $model.realizerModel)
                        SettingsSecretField(title: "API key", text: $model.realizerApiKey)
                        HStack(spacing: 8) {
                            SettingsToggle(title: "Vision", isOn: $model.includeVision)
                            SettingsToggle(title: "Secret OCR guard", isOn: $model.skipVisionOnSensitiveOCR)
                            SettingsToggle(title: "Screenshot redaction", isOn: $model.redactSensitiveScreenshots)
                        }
                    }

                    SettingsBand(title: "Scene reader") {
                        HStack(spacing: 8) {
                            SettingsToggle(title: "Enabled", isOn: $model.vlmEnabled)
                            SettingsTextField(title: "Model", text: $model.vlmModel)
                        }
                        SettingsTextField(title: "Base URL", text: $model.vlmBaseURL)
                        SettingsSecretField(title: "API key", text: $model.vlmApiKey)
                    }

                    HStack(spacing: 8) {
                        Button(saving ? "Saving..." : "Save") {
                            Task { await save() }
                        }
                        .buttonStyle(.plain)
                        .disabled(saving || loading)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 7)
                        .background(Capsule().fill(Color(hex: 0xE8D8A8)))
                        .foregroundStyle(Color(hex: 0x1a1408))

                        Button("Labeler") { openLocal("/label") }
                            .buttonStyle(.plain)
                            .padding(.horizontal, 12)
                            .padding(.vertical, 7)
                            .background(Capsule().fill(Color.white.opacity(0.08)))

                        Button("Dashboard") { openLocal("/dashboard") }
                            .buttonStyle(.plain)
                            .padding(.horizontal, 12)
                            .padding(.vertical, 7)
                            .background(Capsule().fill(Color.white.opacity(0.08)))

                        Button("Snooze 30m") {
                            Task {
                                await HarnessAPI.snooze(duration: "30m")
                                await refresh()
                            }
                        }
                        .buttonStyle(.plain)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 7)
                        .background(Capsule().fill(Color.white.opacity(0.08)))

                        Spacer()
                    }
                    .font(.system(size: 11.5, weight: .semibold))
                    .foregroundStyle(.white.opacity(0.86))
                }
                .padding(.trailing, 6)
            }
            .frame(height: 492)
        }
        .task { await refresh() }
    }

    private var detailText: String {
        if saving { return "saving config" }
        if loading { return "loading config" }
        return model.statusLine.isEmpty ? "LLM ICL policy controls" : model.statusLine
    }

    private func refresh() async {
        loading = true
        await model.refresh()
        loading = false
    }

    private func save() async {
        saving = true
        if model.activePolicy == "llm_icl_v0" {
            model.policyLearnerEnabled = true
            if model.policyLearnerBaseURL.isEmpty { model.policyLearnerBaseURL = model.realizerBaseURL }
            if model.policyLearnerModel.isEmpty { model.policyLearnerModel = model.realizerModel }
            if model.policyLearnerApiKey.isEmpty { model.policyLearnerApiKey = model.realizerApiKey }
        }
        await HarnessAPI.setGoal(model.dailyGoal, sensitivity: model.sensitivity)
        await model.save()
        await model.refresh()
        saving = false
    }

    private func openLocal(_ path: String) {
        if let url = URL(string: "http://127.0.0.1:7893\(path)") {
            NSWorkspace.shared.open(url)
        }
    }
}

private struct SettingsBand<Content: View>: View {
    let title: String
    @ViewBuilder let content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title.uppercased())
                .font(.system(size: 8.8, weight: .semibold))
                .foregroundStyle(.white.opacity(0.44))
            content()
        }
        .padding(10)
        .background(NotchCardBackground())
    }
}

private struct SettingsChip: View {
    let title: String
    let selected: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Text(title)
                .font(.system(size: 11.2, weight: .semibold))
                .foregroundStyle(selected ? Color(hex: 0x1a1408) : .white.opacity(0.74))
                .frame(maxWidth: .infinity)
                .padding(.vertical, 7)
                .background(Capsule().fill(selected ? Color(hex: 0xE8D8A8) : Color.white.opacity(0.08)))
        }
        .buttonStyle(.plain)
    }
}

private struct SettingsSlider: View {
    let title: String
    @Binding var value: Double
    let range: ClosedRange<Double>
    let step: Double
    let format: (Double) -> String

    var body: some View {
        HStack(spacing: 10) {
            Text(title)
                .font(.system(size: 11))
                .foregroundStyle(.white.opacity(0.68))
                .frame(width: 150, alignment: .leading)
            Slider(value: $value, in: range, step: step)
            Text(format(value))
                .font(.system(size: 11, weight: .semibold, design: .monospaced))
                .foregroundStyle(Color(hex: 0xE8D8A8))
                .frame(width: 42, alignment: .trailing)
        }
    }
}

private struct SettingsTextField: View {
    let title: String
    @Binding var text: String

    var body: some View {
        HStack(spacing: 10) {
            Text(title)
                .font(.system(size: 11))
                .foregroundStyle(.white.opacity(0.58))
                .frame(width: 70, alignment: .leading)
            TextField("", text: $text)
                .textFieldStyle(.plain)
                .font(.system(size: 11.5, design: .monospaced))
                .foregroundStyle(.white.opacity(0.88))
                .padding(.horizontal, 9)
                .padding(.vertical, 7)
                .background(NotchInputBackground())
        }
    }
}

private struct SettingsSecretField: View {
    let title: String
    @Binding var text: String
    @State private var editing = false
    @State private var draft = ""

    var body: some View {
        HStack(spacing: 10) {
            Text(title)
                .font(.system(size: 11))
                .foregroundStyle(.white.opacity(0.58))
                .frame(width: 70, alignment: .leading)

            if editing {
                TextField("", text: $draft)
                    .textFieldStyle(.plain)
                    .font(.system(size: 11.5, design: .monospaced))
                    .foregroundStyle(.white.opacity(0.88))
                    .padding(.horizontal, 9)
                    .padding(.vertical, 7)
                    .background(NotchInputBackground())

                SettingsIconButton(systemName: "checkmark", help: "Apply API key") {
                    text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
                    draft = text
                    editing = false
                }
                SettingsIconButton(systemName: "xmark", help: "Cancel editing") {
                    draft = text
                    editing = false
                }
            } else {
                HStack(spacing: 2) {
                    secretPreview
                    Spacer(minLength: 0)
                }
                .padding(.horizontal, 9)
                .padding(.vertical, 7)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(NotchInputBackground())

                SettingsIconButton(systemName: "pencil", help: "Edit API key") {
                    draft = text
                    editing = true
                }
            }
        }
    }

    @ViewBuilder
    private var secretPreview: some View {
        let value = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if value.isEmpty {
            Text("not set")
                .font(.system(size: 11.5, design: .monospaced))
                .foregroundStyle(.white.opacity(0.38))
        } else {
            let parts = previewParts(for: value)
            Text(parts.prefix)
                .font(.system(size: 11.5, weight: .semibold, design: .monospaced))
                .foregroundStyle(.white.opacity(0.84))
            Text(parts.mask)
                .font(.system(size: 11.5, weight: .semibold, design: .monospaced))
                .foregroundStyle(.white.opacity(0.46))
                .blur(radius: 1.2)
            Text(parts.suffix)
                .font(.system(size: 11.5, weight: .semibold, design: .monospaced))
                .foregroundStyle(Color(hex: 0xE8D8A8).opacity(0.88))
        }
    }

    private func previewParts(for value: String) -> (prefix: String, mask: String, suffix: String) {
        if value.count <= 10 {
            let suffixCount = min(2, value.count)
            let suffix = String(value.suffix(suffixCount))
            let maskCount = max(4, value.count - suffixCount)
            return ("", String(repeating: "*", count: maskCount), suffix)
        }

        let prefixCount = min(6, max(0, value.count - 4))
        let suffixCount = min(4, max(0, value.count - prefixCount))
        let hiddenCount = max(4, min(14, value.count - prefixCount - suffixCount))
        return (
            String(value.prefix(prefixCount)),
            String(repeating: "*", count: hiddenCount),
            String(value.suffix(suffixCount))
        )
    }
}

private struct SettingsIconButton: View {
    let systemName: String
    let help: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: systemName)
                .font(.system(size: 10.5, weight: .semibold))
                .foregroundStyle(.white.opacity(0.72))
                .frame(width: 28, height: 28)
                .background(Capsule().fill(Color.white.opacity(0.08)))
        }
        .buttonStyle(.plain)
        .help(help)
    }
}

private struct SettingsToggle: View {
    let title: String
    @Binding var isOn: Bool

    var body: some View {
        Button(action: { isOn.toggle() }) {
            HStack(spacing: 6) {
                Image(systemName: isOn ? "checkmark.circle.fill" : "circle")
                    .font(.system(size: 11, weight: .semibold))
                Text(title)
                    .font(.system(size: 10.8, weight: .semibold))
                    .lineLimit(1)
                    .minimumScaleFactor(0.78)
            }
            .foregroundStyle(isOn ? Color(hex: 0xE8D8A8) : .white.opacity(0.60))
            .padding(.horizontal, 9)
            .frame(height: 30)
            .background(Capsule().fill(Color.white.opacity(0.07)))
        }
        .buttonStyle(.plain)
    }
}

private struct NotchInputBackground: View {
    var body: some View {
        RoundedRectangle(cornerRadius: 7, style: .continuous)
            .fill(Color.white.opacity(0.07))
            .overlay(
                RoundedRectangle(cornerRadius: 7, style: .continuous)
                    .strokeBorder(Color.white.opacity(0.10), lineWidth: 0.5)
            )
    }
}

private struct DietNotchPanel: View {
    let diet: JSON?
    let loading: Bool
    let error: String?
    let refreshedAt: Date?
    let refresh: () -> Void

    var body: some View {
        let summary = diet?["summary"]
        VStack(alignment: .leading, spacing: 12) {
            panelToolbar(title: "Information diet", detail: updatedText, loading: loading, refresh: refresh)
            HStack(spacing: 8) {
                NotchMetric(label: "research events", value: "\(summary?["n_research_events"].int ?? 0)")
                NotchMetric(label: "episodes", value: "\(summary?["n_episodes"].int ?? 0)")
                NotchMetric(label: "observed min", value: notchNumber(summary?["observed_research_min"].double))
                NotchMetric(label: "hypotheses", value: "\(skillRows.count)")
            }

            HStack(alignment: .top, spacing: 10) {
                NotchMiniSection(title: "Domains") {
                    NotchBars(data: notchPairs(summary?["top_domains"].dict, limit: 6))
                }
                NotchMiniSection(title: "Workflow") {
                    NotchBars(data: notchPairs(summary?["workflow_patterns"].dict, limit: 6))
                }
            }

            NotchMiniSection(title: "Skill hypotheses") {
                VStack(alignment: .leading, spacing: 7) {
                    ForEach(Array(skillRows.prefix(3).enumerated()), id: \.offset) { _, row in
                        NotchSkillRow(row: row)
                    }
                    if skillRows.isEmpty {
                        NotchMutedText(error ?? "No research-workflow hypotheses in this window.")
                    }
                }
            }
        }
    }

    private var updatedText: String {
        guard let refreshedAt else { return "24h live window" }
        return "24h live window · refreshed \(notchTime(refreshedAt))"
    }

    private var skillRows: [[String: Any]] {
        (diet?["skill_hypotheses"].list ?? []).compactMap { $0 as? [String: Any] }
    }
}

private func panelToolbar(title: String, detail: String, loading: Bool, refresh: @escaping () -> Void) -> some View {
    HStack(spacing: 8) {
        VStack(alignment: .leading, spacing: 2) {
            Text(title)
                .font(.system(size: 12.5, weight: .semibold))
                .foregroundStyle(.white.opacity(0.92))
            Text(loading ? "loading..." : detail)
                .font(.system(size: 10.5))
                .foregroundStyle(.white.opacity(0.44))
        }
        Spacer()
        Button(action: refresh) {
            Image(systemName: "arrow.clockwise")
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(.white.opacity(0.72))
                .frame(width: 28, height: 24)
                .background(Capsule().fill(Color.white.opacity(0.08)))
        }
        .buttonStyle(.plain)
    }
}

private struct NotchStage {
    let name: String
    let value: String
    let detail: String

    init(_ name: String, _ value: String, _ detail: String) {
        self.name = name
        self.value = value
        self.detail = detail
    }
}

private struct NotchRail: View {
    let stages: [NotchStage]

    var body: some View {
        HStack(spacing: 7) {
            ForEach(Array(stages.enumerated()), id: \.offset) { index, stage in
                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: 5) {
                        Circle()
                            .fill(index == 0 ? Color(hex: 0xE8D8A8) : Color.white.opacity(0.22))
                            .frame(width: 6, height: 6)
                        Text(stage.name.uppercased())
                            .font(.system(size: 8.8, weight: .semibold))
                            .foregroundStyle(.white.opacity(0.46))
                            .lineLimit(1)
                    }
                    Text(stage.value)
                        .font(.system(size: 17, weight: .semibold, design: .monospaced))
                        .foregroundStyle(.white.opacity(0.92))
                        .lineLimit(1)
                        .minimumScaleFactor(0.7)
                    Text(stage.detail)
                        .font(.system(size: 9.5))
                        .foregroundStyle(.white.opacity(0.34))
                        .lineLimit(1)
                }
                .padding(9)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(NotchCardBackground())
            }
        }
    }
}

private struct NotchMetric: View {
    let label: String
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(label.uppercased())
                .font(.system(size: 8.8, weight: .semibold))
                .foregroundStyle(.white.opacity(0.42))
                .lineLimit(1)
            Text(value)
                .font(.system(size: 18, weight: .semibold, design: .monospaced))
                .foregroundStyle(Color(hex: 0xE8D8A8))
                .lineLimit(1)
                .minimumScaleFactor(0.7)
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(NotchCardBackground())
    }
}

private struct NotchMiniSection<Content: View>: View {
    let title: String
    @ViewBuilder let content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title.uppercased())
                .font(.system(size: 8.8, weight: .semibold))
                .foregroundStyle(.white.opacity(0.44))
            content()
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .background(NotchCardBackground())
    }
}

private struct NotchBars: View {
    let data: [(String, Int)]

    var body: some View {
        let maxV = max(data.first?.1 ?? 1, 1)
        VStack(alignment: .leading, spacing: 6) {
            ForEach(data, id: \.0) { key, value in
                HStack(spacing: 8) {
                    Text(notchShort(key, max: 18))
                        .font(.system(size: 10.5, design: .monospaced))
                        .foregroundStyle(.white.opacity(0.74))
                        .frame(width: 118, alignment: .leading)
                    GeometryReader { geometry in
                        ZStack(alignment: .leading) {
                            Capsule().fill(Color.white.opacity(0.10)).frame(height: 4)
                            Capsule()
                                .fill(Color(hex: 0xE8D8A8).opacity(0.9))
                                .frame(width: geometry.size.width * CGFloat(Double(value) / Double(maxV)), height: 4)
                        }
                    }
                    .frame(height: 4)
                    Text("\(value)")
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.white.opacity(0.38))
                        .frame(width: 24, alignment: .trailing)
                }
            }
            if data.isEmpty {
                NotchMutedText("No data yet.")
            }
        }
    }
}

private struct NotchExampleRow: View {
    let row: [String: Any]

    var body: some View {
        let cls = notchDict(row, "classification")
        let context = notchDict(row, "context")
        HStack(spacing: 8) {
            Text(notchString(cls, "type").uppercased())
                .font(.system(size: 9.8, weight: .semibold, design: .monospaced))
                .foregroundStyle(Color(hex: 0xE8D8A8))
                .frame(width: 118, alignment: .leading)
            Text(notchShort(notchString(context, "message"), max: 48))
                .font(.system(size: 10.5))
                .foregroundStyle(.white.opacity(0.72))
                .lineLimit(1)
            Spacer(minLength: 0)
        }
    }
}

private struct NotchSkillRow: View {
    let row: [String: Any]

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(notchString(row, "topic").uppercased())
                    .font(.system(size: 9.5, weight: .semibold, design: .monospaced))
                    .foregroundStyle(Color(hex: 0xE8D8A8))
                Spacer()
                Text("c=\(notchNumber(notchNumberValue(row["confidence"])))")
                    .font(.system(size: 9.5, design: .monospaced))
                    .foregroundStyle(.white.opacity(0.36))
            }
            Text(notchString(row, "hypothesis"))
                .font(.system(size: 10.8))
                .foregroundStyle(.white.opacity(0.76))
                .lineLimit(2)
        }
    }
}

private struct NotchMutedText: View {
    let text: String
    init(_ text: String) { self.text = text }

    var body: some View {
        Text(text)
            .font(.system(size: 10.5, design: .monospaced))
            .foregroundStyle(.white.opacity(0.36))
            .lineLimit(2)
    }
}

private struct NotchBanner: View {
    let text: String
    init(_ text: String) { self.text = text }

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "info.circle")
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(Color(hex: 0xE8D8A8).opacity(0.9))
            Text(text)
                .font(.system(size: 10.5))
                .foregroundStyle(.white.opacity(0.55))
                .lineLimit(2)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .background(NotchCardBackground())
    }
}

private struct NotchCardBackground: View {
    var body: some View {
        RoundedRectangle(cornerRadius: 8, style: .continuous)
            .fill(Color.white.opacity(0.075))
            .overlay(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .strokeBorder(Color.white.opacity(0.08), lineWidth: 0.5)
            )
    }
}

private func notchPairs(_ dict: [String: Any]?, limit: Int) -> [(String, Int)] {
    let pairs = (dict ?? [:]).map { key, value in
        (key, notchIntValue(value))
    }
    return Array(pairs.sorted { $0.1 > $1.1 }.prefix(limit))
}

private func notchIntValue(_ value: Any?) -> Int {
    if let n = value as? NSNumber { return n.intValue }
    if let i = value as? Int { return i }
    if let d = value as? Double { return Int(d) }
    if let j = value as? JSON { return j.int }
    return 0
}

private func notchNumberValue(_ value: Any?) -> Double? {
    if value == nil || value is NSNull { return nil }
    if let n = value as? NSNumber { return n.doubleValue }
    if let d = value as? Double { return d }
    if let i = value as? Int { return Double(i) }
    if let j = value as? JSON, !j.isNull { return j.double }
    return nil
}

private func notchNumber(_ value: Double?) -> String {
    guard let value else { return "n/a" }
    if abs(value) >= 10 { return String(format: "%.0f", value) }
    return String(format: "%.2f", value)
}

private func notchPct(_ value: JSON?) -> String {
    guard let value, !value.isNull else { return "n/a" }
    return String(format: "%.1f%%", value.double * 100.0)
}

private func notchDict(_ row: [String: Any], _ key: String) -> [String: Any] {
    row[key] as? [String: Any] ?? [:]
}

private func notchString(_ row: [String: Any], _ key: String) -> String {
    let value = row[key]
    if value == nil || value is NSNull { return "" }
    return String(describing: value!)
}

private func notchShort(_ value: String, max: Int) -> String {
    if value.count <= max { return value }
    if max <= 3 { return String(value.prefix(max)) }
    return String(value.prefix(max - 3)) + "..."
}

private func notchTime(_ date: Date) -> String {
    let formatter = DateFormatter()
    formatter.dateFormat = "HH:mm:ss"
    return formatter.string(from: date)
}

// MARK: - Compact strips (shown next to the system notch in compact state)

struct HarnessCompactLeading: View {
    @ObservedObject var state: HarnessState
    var body: some View {
        StatusDot()
            .padding(.leading, 4)
            .notchDragHandle(state)
            .help("Drag to move the harness capsule")
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
                    .foregroundStyle(.white.opacity(0.55))
                    .padding(.trailing, 4)
                    .notchDragHandle(state)
                    .help("Drag to move the harness capsule")
            } else {
                Text(state.activePanel.rawValue.uppercased())
                    .font(.system(size: 9, weight: .semibold))
                    .foregroundStyle(.white.opacity(0.55))
                    .padding(.trailing, 4)
                    .notchDragHandle(state)
                    .help("Drag to move the harness capsule")
            }
        }
    }
}

private struct NotchDragHandle: ViewModifier {
    @ObservedObject var state: HarnessState
    @State private var lastTranslationX: CGFloat = 0
    @State private var lastTranslationY: CGFloat = 0

    func body(content: Content) -> some View {
        content
            .contentShape(Rectangle())
            .gesture(
                DragGesture(minimumDistance: 3)
                    .onChanged { value in
                        let delta = CGSize(
                            width: value.translation.width - lastTranslationX,
                            height: value.translation.height - lastTranslationY
                        )
                        lastTranslationX = value.translation.width
                        lastTranslationY = value.translation.height
                        state.dragHandler?(delta)
                    }
                    .onEnded { _ in
                        lastTranslationX = 0
                        lastTranslationY = 0
                        state.dragEndHandler?()
                    }
            )
    }
}

private extension View {
    func notchDragHandle(_ state: HarnessState) -> some View {
        modifier(NotchDragHandle(state: state))
    }
}

private struct FloatingSurfaceBackground: View {
    let radius: CGFloat

    var body: some View {
        RoundedRectangle(cornerRadius: radius, style: .continuous)
            .fill(Color.black.opacity(0.96))
            .overlay(
                RoundedRectangle(cornerRadius: radius, style: .continuous)
                    .strokeBorder(Color.white.opacity(0.10), lineWidth: 0.75)
            )
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
