import SwiftUI

// MARK: - Top-level settings window content with tabs

struct SettingsRoot: View {
    @StateObject var model = SettingsModel()
    @State var selectedTab: SettingsTab = .today

    var body: some View {
        VStack(spacing: 0) {
            TabBar(selected: $selectedTab)

            ScrollView {
                Group {
                    switch selectedTab {
                    case .today:       TodayTab(model: model)
                    case .status:      StatusTab(model: model)
                    case .gate:        GateTab(model: model)
                    case .realizer:    RealizerTab(model: model)
                    case .sceneReader: SceneReaderTab(model: model)
                    case .diagnostics: DiagnosticsTab(model: model)
                    }
                }
                .padding(.horizontal, 24)
                .padding(.vertical, 16)
            }

            FooterBar(model: model)
        }
        .frame(minWidth: 640, idealWidth: 720, minHeight: 540, idealHeight: 640)
        .background(Color(nsColor: .windowBackgroundColor))
        .task {
            await model.refresh()
            model.startPolling()
        }
        .onDisappear { model.stopPolling() }
    }
}

// MARK: - Today tab (primary surface; goal-driven)

struct TodayTab: View {
    @ObservedObject var model: SettingsModel
    @State private var saving = false

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            SectionTitle("Your intention for today")
            Text("Write what you're actually trying to do. The harness uses this to decide when interrupting you would help — and to write messages that serve this goal.")
                .font(.system(size: 11.5))
                .foregroundStyle(.secondary)

            TextEditor(text: $model.dailyGoal)
                .font(.system(size: 13))
                .frame(minHeight: 100, maxHeight: 140)
                .padding(8)
                .background(Color(nsColor: .controlBackgroundColor))
                .overlay(
                    RoundedRectangle(cornerRadius: 6)
                        .strokeBorder(Color(nsColor: .separatorColor), lineWidth: 0.5)
                )
                .cornerRadius(6)

            HStack(spacing: 8) {
                Button(saving ? "Saving…" : "Save goal") {
                    Task {
                        saving = true
                        await HarnessAPI.setGoal(model.dailyGoal, sensitivity: model.sensitivity)
                        await model.refresh()
                        saving = false
                    }
                }
                .buttonStyle(.borderedProminent)
                .disabled(saving)

                Button("Clear") {
                    Task {
                        await HarnessAPI.clearGoal()
                        model.dailyGoal = ""
                        await model.refresh()
                    }
                }
                .buttonStyle(.bordered)

                Spacer()
                if let setAt = model.goalSetAt, !setAt.isEmpty {
                    Text("set: \(setAt.prefix(19))")
                        .font(.system(size: 10.5, design: .monospaced))
                        .foregroundStyle(.tertiary)
                }
            }

            Divider().padding(.vertical, 4)

            SectionTitle("How responsive should the harness be?")
            HStack(spacing: 0) {
                ForEach(["gentle", "balanced", "responsive"], id: \.self) { s in
                    Button(action: {
                        Task {
                            model.sensitivity = s
                            await HarnessAPI.setGoal(model.dailyGoal, sensitivity: s)
                        }
                    }) {
                        VStack(spacing: 4) {
                            Text(s.capitalized)
                                .font(.system(size: 12, weight: model.sensitivity == s ? .semibold : .regular))
                                .foregroundStyle(model.sensitivity == s ? Color.accentColor : Color(nsColor: .labelColor))
                            Text(sensitivityHint(s))
                                .font(.system(size: 10))
                                .foregroundStyle(.secondary)
                        }
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 10)
                        .background(model.sensitivity == s ? Color.accentColor.opacity(0.08) : Color.clear)
                    }
                    .buttonStyle(.plain)
                    .overlay(
                        Rectangle()
                            .fill(Color(nsColor: .separatorColor))
                            .frame(width: 0.5),
                        alignment: .trailing
                    )
                }
            }
            .background(Color(nsColor: .controlBackgroundColor))
            .overlay(
                RoundedRectangle(cornerRadius: 6)
                    .strokeBorder(Color(nsColor: .separatorColor), lineWidth: 0.5)
            )
            .cornerRadius(6)

            Divider().padding(.vertical, 4)

            SectionTitle("Today at a glance")
            HStack(spacing: 12) {
                Stat(label: "Pings", value: "\(model.data?["n_pings"].int ?? 0)")
                Stat(label: "Decisions", value: "\(model.data?["n_decisions"].int ?? 0)")
                Stat(label: "Clicked", value: "\(model.data?["n_clicked"].int ?? 0)")
                Stat(label: "Considered", value: "\(model.data?["n_considered_no_click"].int ?? 0)")
            }
        }
    }

    private func sensitivityHint(_ s: String) -> String {
        switch s {
        case "gentle":     return "15-min cooldown · only strong signals"
        case "balanced":   return "5-min cooldown · default"
        case "responsive": return "2-min cooldown · pings more"
        default: return ""
        }
    }
}

enum SettingsTab: String, CaseIterable {
    case today = "Today"
    case status = "Status"
    case gate = "Behavior"
    case realizer = "Model"
    case sceneReader = "Scene Reader"
    case diagnostics = "Diagnostics"
}

// MARK: - Tab bar

struct TabBar: View {
    @Binding var selected: SettingsTab

    var body: some View {
        HStack(spacing: 0) {
            ForEach(SettingsTab.allCases, id: \.self) { tab in
                Button(action: { selected = tab }) {
                    Text(tab.rawValue)
                        .font(.system(size: 12, weight: selected == tab ? .semibold : .regular))
                        .foregroundStyle(selected == tab ? Color.accentColor : Color(nsColor: .secondaryLabelColor))
                        .padding(.horizontal, 14)
                        .padding(.vertical, 10)
                        .background(
                            selected == tab
                                ? Color.accentColor.opacity(0.08)
                                : Color.clear
                        )
                        .cornerRadius(6)
                }
                .buttonStyle(.plain)
            }
            Spacer()
        }
        .padding(.horizontal, 12)
        .padding(.top, 12)
        .padding(.bottom, 6)
        .overlay(
            Rectangle()
                .fill(Color(nsColor: .separatorColor))
                .frame(height: 0.5)
                .padding(.horizontal, 0),
            alignment: .bottom
        )
    }
}

// MARK: - Status tab

struct StatusTab: View {
    @ObservedObject var model: SettingsModel

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            SectionTitle("Live")
            HStack(spacing: 12) {
                Stat(label: "Pings 24h",       value: "\(model.data?["n_pings"].int ?? 0)")
                Stat(label: "Decisions 24h",   value: "\(model.data?["n_decisions"].int ?? 0)")
                Stat(label: "Outcomes",        value: "\(model.data?["n_outcomes"].int ?? 0)")
                Stat(label: "Considered",      value: "\(model.data?["n_considered_no_click"].int ?? 0)")
            }
            SectionTitle("Lab metrics")
            HStack(spacing: 12) {
                Stat(label: "Ping rate",        value: pct(model.metrics?["ping_rate"]))
                Stat(label: "Outcome capture",  value: pct(model.metrics?["outcomes"]["capture_rate_for_pings"]))
                Stat(label: "Agreement",        value: pct(model.metrics?["labels"]["agreement_rate"]))
                Stat(label: "Labels",           value: "\(model.metrics?["labels"]["n"].int ?? 0)")
            }
            HStack(spacing: 12) {
                Stat(label: "False interrupts", value: pct(model.metrics?["labels"]["false_interruption_rate_labeled"]))
                Stat(label: "Missed help",      value: pct(model.metrics?["labels"]["missed_help_rate_labeled"]))
                Stat(label: "Need personal",    value: "\(model.metrics?["data_readiness"]["needs_labels_for_personalization"].int ?? 20)")
                Stat(label: "Need learned",     value: "\(model.metrics?["data_readiness"]["needs_labels_for_learned_gate"].int ?? 500)")
            }
            SectionTitle("Implicit learning signal")
            HStack(spacing: 12) {
                Stat(label: "Usable implicit",   value: "\(model.metrics?["implicit"]["usable"].int ?? 0)")
                Stat(label: "Weighted n",        value: String(format: "%.1f", model.metrics?["implicit"]["confidence_weighted_n"].double ?? 0))
                Stat(label: "Implicit +",        value: "\(model.metrics?["implicit"]["positive"].int ?? 0)")
                Stat(label: "Implicit -",        value: "\(model.metrics?["implicit"]["negative"].int ?? 0)")
            }
            HStack(spacing: 12) {
                Stat(label: "Implicit need",     value: "\(model.metrics?["data_readiness"]["needs_implicit_for_personalization"].int ?? 50)")
                Stat(label: "Ignored",           value: "\(model.metrics?["implicit"]["ignored"].int ?? 0)")
                Stat(label: "Neutral",           value: "\(model.metrics?["implicit"]["neutral"].int ?? 0)")
                Stat(label: "Outcome labels",    value: "\(model.metrics?["implicit"]["n"].int ?? 0)")
            }
            SectionTitle("Top scenes (24h)")
            BarList(data: dictAsKVs(model.data?["dist_scenes"].dict))
            SectionTitle("Top apps (24h)")
            BarList(data: dictAsKVs(model.data?["dist_apps"].dict))
            SectionTitle("Decision reasons")
            BarList(data: dictAsKVs(model.data?["dist_reasons"].dict))
            SectionTitle("Intent signal (outcomes)")
            BarList(data: dictAsKVs(model.data?["dist_intent_signals"].dict))
        }
    }

    private func dictAsKVs(_ d: [String: Any]?) -> [(String, Int)] {
        let pairs = (d ?? [:]).map { ($0.key, ($0.value as? Int) ?? 0) }
        return pairs.sorted { $0.1 > $1.1 }.prefix(8).map { $0 }
    }

    private func pct(_ value: JSON?) -> String {
        guard let value, !value.isNull else { return "n/a" }
        return String(format: "%.1f%%", value.double * 100.0)
    }
}

// MARK: - Gate tab

struct GateTab: View {
    @ObservedObject var model: SettingsModel

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            SectionTitle("How often the harness checks in")
            FormRow("Poll interval (sec)", hint: "5 = matches Fisherman capture") {
                TextField("", value: $model.pollInterval, formatter: intFormatter).textFieldStyle(.roundedBorder)
            }
            FormRow("Cooldown between pings", hint: "minimum minutes") {
                TextField("", value: $model.cooldownMin, formatter: doubleFormatter).textFieldStyle(.roundedBorder)
            }
            FormRow("After dismissal", hint: "minutes to back off after dismiss/mute") {
                TextField("", value: $model.negativeFeedbackBackoffMin, formatter: doubleFormatter).textFieldStyle(.roundedBorder)
            }
            FormRow("Quiet hours start",   hint: "24h") {
                TextField("", value: $model.quietStart, formatter: intFormatter).textFieldStyle(.roundedBorder)
            }
            FormRow("Quiet hours end",     hint: "24h, wraps midnight") {
                TextField("", value: $model.quietEnd, formatter: intFormatter).textFieldStyle(.roundedBorder)
            }
            SectionTitle("Experimentation")
            FormRow("Enabled", hint: "logs deterministic assignments") {
                Toggle("", isOn: $model.experimentEnabled).labelsHidden()
            }
            FormRow("Holdout rate", hint: "fraction of would-pings held silent") {
                TextField("", value: $model.holdoutRate, formatter: doubleFormatter).textFieldStyle(.roundedBorder)
            }
            FormRow("Explore ping rate", hint: "0 by default; opt-in random pings") {
                TextField("", value: $model.explorePingRate, formatter: doubleFormatter).textFieldStyle(.roundedBorder)
            }
            FormRow("Experiment salt", hint: "changes assignment buckets") {
                TextField("", text: $model.experimentSalt).textFieldStyle(.roundedBorder)
            }
            SectionTitle("Snooze")
            HStack(spacing: 8) {
                Button("30m")  { Task { await HarnessAPI.snooze(duration: "30m"); await model.refresh() } }
                Button("2h")   { Task { await HarnessAPI.snooze(duration: "2h");  await model.refresh() } }
                Button("Until tomorrow") { Task { await HarnessAPI.snooze(duration: "12h"); await model.refresh() } }
                Button("Clear") { Task { await HarnessAPI.unsnooze();              await model.refresh() } }
                Spacer()
                if let until = model.snoozedUntil, !until.isEmpty {
                    Text("snoozed until \(until)")
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.secondary)
                }
            }
        }
    }
}

// MARK: - Scene Reader tab (VLM per-candidate)

struct SceneReaderTab: View {
    @ObservedObject var model: SettingsModel

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            SectionTitle("Per-candidate vision pass")
            Text("Sends each meaningfully-changed screenshot to a cheap VLM to enrich the scene tag with what's actually visible. Smart-triggered: skips when the app + OCR are unchanged since the last call.")
                .font(.system(size: 11))
                .foregroundStyle(.secondary)

            FormRow("Enabled", hint: "off by default; needs key below") {
                Toggle("", isOn: $model.vlmEnabled).labelsHidden()
            }
            FormRow("Endpoint", hint: "OpenAI-compatible") {
                TextField("", text: $model.vlmBaseURL).textFieldStyle(.roundedBorder)
            }
            FormRow("Model", hint: "needs vision; default: gemma-3-4b-it") {
                TextField("", text: $model.vlmModel).textFieldStyle(.roundedBorder)
            }
            FormRow("OpenRouter key", hint: "your sk-or-v1-… key") {
                TextField("", text: $model.vlmApiKey).textFieldStyle(.roundedBorder)
            }
            FormRow("Min interval (sec)", hint: "smart-trigger cooldown") {
                TextField("", value: $model.vlmMinInterval, formatter: intFormatter).textFieldStyle(.roundedBorder)
            }
            FormRow("Timeout (sec)", hint: "VLM round-trip ceiling") {
                TextField("", value: $model.vlmTimeoutSec, formatter: intFormatter).textFieldStyle(.roundedBorder)
            }

            SectionTitle("Estimated cost")
            Text("Gemma-3-4b-it ≈ $0.04/M tokens · ~$0.000014 per call. At 30s min interval that's ~$1.20/mo of continuous use; idle screens cost $0 because the smart-trigger skips the call.")
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
                .padding(.bottom, 8)
        }
    }
}

// MARK: - Realizer tab

struct RealizerTab: View {
    @ObservedObject var model: SettingsModel

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            SectionTitle("LLM endpoint")
            Text("Paste your provider key here. It is saved only to ~/.harness/config.toml and is no longer shipped in the repo defaults.")
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
            FormRow("Base URL", hint: "OpenAI-compatible") {
                TextField("", text: $model.realizerBaseURL).textFieldStyle(.roundedBorder)
            }
            FormRow("Model", hint: "e.g. hermes-agent") {
                TextField("", text: $model.realizerModel).textFieldStyle(.roundedBorder)
            }
            FormRow("API key", hint: model.realizerApiKey.isEmpty ? "required unless HARNESS_REALIZER_KEY is set" : "configured locally") {
                // Use plain TextField (not SecureField) — SecureField triggers the
                // macOS Passwords / autofill UI which intercepts paste shortcuts.
                TextField("", text: $model.realizerApiKey).textFieldStyle(.roundedBorder)
            }
            FormRow("Max tokens out", hint: "tight ceiling = brevity") {
                TextField("", value: $model.maxTokens, formatter: intFormatter).textFieldStyle(.roundedBorder)
            }
            FormRow("Timeout (sec)", hint: "45 recommended with vision") {
                TextField("", value: $model.timeoutSec, formatter: intFormatter).textFieldStyle(.roundedBorder)
            }
            SectionTitle("Multimodal")
            FormRow("Send screenshot (vision)", hint: "attach JPEG to each ping") {
                Toggle("", isOn: $model.includeVision).labelsHidden()
            }
            FormRow("Mask sensitive screenshots", hint: "local OCR masks key/token text boxes") {
                Toggle("", isOn: $model.redactSensitiveScreenshots).labelsHidden()
            }
            FormRow("Fail closed on sensitive OCR", hint: "skip image if local masking cannot prove it worked") {
                Toggle("", isOn: $model.skipVisionOnSensitiveOCR).labelsHidden()
            }
            SectionTitle("Trust boundary")
            FormRow("Block unknown hosts", hint: "before any model prompt or image leaves") {
                Toggle("", isOn: $model.blockUntrustedModelHosts).labelsHidden()
            }
            FormRow("Allowed hosts", hint: "comma or space separated") {
                TextField("", text: $model.allowedModelHostsText).textFieldStyle(.roundedBorder)
            }
        }
    }
}

// IntentsTab removed — replaced by the goal-driven model in TodayTab.
// The 4 fixed intents are gone; reason_codes from the gate drive the realizer
// directly, which composes the message from (daily_goal, why_now, image).

// MARK: - Reward tab

struct RewardTab: View {
    @ObservedObject var model: SettingsModel

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            SectionTitle("Reward weights")
            Text("Used by `harness score`. Tune these to reflect how you actually feel about each outcome class.")
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
            FormRow("welcomed (clicked)",    hint: "+ for good pings") {
                TextField("", value: $model.rewardWelcomed, formatter: doubleFormatter).textFieldStyle(.roundedBorder)
            }
            FormRow("annoying (dismissed)",  hint: "− for bad pings") {
                TextField("", value: $model.rewardAnnoying, formatter: doubleFormatter).textFieldStyle(.roundedBorder)
            }
            FormRow("privacy violation",     hint: "large negative") {
                TextField("", value: $model.rewardPrivacy, formatter: doubleFormatter).textFieldStyle(.roundedBorder)
            }
            FormRow("duplicate",             hint: "− for repetition") {
                TextField("", value: $model.rewardDuplicate, formatter: doubleFormatter).textFieldStyle(.roundedBorder)
            }
        }
    }
}

// MARK: - Diagnostics tab

struct DiagnosticsTab: View {
    @ObservedObject var model: SettingsModel

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            SectionTitle("Recent decisions (30)")
            DiagList(rows: model.recentDecisions.map { d in
                let action = (d["action"] as? String) ?? "?"
                let intent = (d["intent"] as? String) ?? "—"
                let reasons = (d["reason_codes"] as? [String]) ?? []
                let exp = (d["experiment"] as? [String: Any]) ?? [:]
                let assignment = (exp["assignment"] as? String).map { " exp=\($0)" } ?? ""
                let ts = String(((d["ts"] as? String) ?? "").prefix(19))
                return "\(ts)  \(action.padding(toLength: 10, withPad: " ", startingAt: 0))  intent=\(intent)\(assignment)  [\(reasons.joined(separator: ", "))]"
            })
            SectionTitle("Recent outcomes (15)")
            DiagList(rows: model.recentOutcomes.map { o in
                let action = (o["user_action"] as? String) ?? "?"
                let did = (o["decision_id"] as? String) ?? "?"
                let summary = (o["interaction_summary"] as? [String: Any]) ?? [:]
                let signal = (summary["intent_signal"] as? String) ?? "—"
                let ts = String(((o["ts"] as? String) ?? "").prefix(19))
                return "\(ts)  \(action.padding(toLength: 10, withPad: " ", startingAt: 0))  signal=\(signal)  \(did)"
            })
            SectionTitle("Recent realizer messages (10)")
            VStack(alignment: .leading, spacing: 8) {
                ForEach(Array(model.recentRealizations.enumerated()), id: \.offset) { _, r in
                    VStack(alignment: .leading, spacing: 3) {
                        HStack {
                            Text((r["intent"] as? String) ?? "?")
                                .font(.system(size: 10.5, weight: .semibold, design: .monospaced))
                                .foregroundStyle(.secondary)
                            Text("\((r["latency_ms"] as? Int) ?? 0)ms")
                                .font(.system(size: 10, design: .monospaced))
                                .foregroundStyle(.tertiary)
                            if (r["vision_used"] as? Bool) ?? false {
                                Text("vision")
                                    .font(.system(size: 10, design: .monospaced))
                                    .foregroundStyle(.tertiary)
                            }
                            Spacer()
                            Text(String(((r["ts"] as? String) ?? "").prefix(19)))
                                .font(.system(size: 10, design: .monospaced))
                                .foregroundStyle(.tertiary)
                        }
                        Text("\"\((r["message"] as? String) ?? "")\"")
                            .font(.system(size: 12))
                    }
                    .padding(10)
                    .background(Color(nsColor: .controlBackgroundColor))
                    .cornerRadius(6)
                }
            }
            SectionTitle("Recent model calls (30)")
            DiagList(rows: model.recentModelCalls.map { r in
                let ts = String(((r["ts"] as? String) ?? "").prefix(19))
                let purpose = (r["purpose"] as? String) ?? "?"
                let modelName = (r["model"] as? String) ?? "?"
                let status = (r["status"] as? String) ?? "?"
                let http = r["http_status"] as? Int
                let latency = (r["latency_ms"] as? Int) ?? 0
                let imageBytes = (r["image_bytes"] as? Int) ?? 0
                return "\(ts)  \(purpose)  \(modelName)  status=\(status) http=\(http.map(String.init) ?? "—") \(latency)ms image=\(imageBytes)B"
            })
        }
    }
}

// MARK: - Reusable bits

struct SectionTitle: View {
    let title: String
    init(_ t: String) { self.title = t }
    var body: some View {
        Text(title.uppercased())
            .font(.system(size: 10, weight: .semibold))
            .tracking(0.5)
            .foregroundStyle(.secondary)
            .padding(.top, 4)
    }
}

struct FormRow<Content: View>: View {
    let label: String
    let hint: String?
    @ViewBuilder let content: () -> Content

    init(_ label: String, hint: String? = nil, @ViewBuilder content: @escaping () -> Content) {
        self.label = label
        self.hint = hint
        self.content = content
    }

    var body: some View {
        HStack(alignment: .center, spacing: 14) {
            Text(label).frame(width: 170, alignment: .leading).font(.system(size: 12))
            content().frame(maxWidth: 260)
            if let h = hint {
                Text(h)
                    .font(.system(size: 10.5, design: .monospaced))
                    .foregroundStyle(.tertiary)
                    .lineLimit(2)
            }
            Spacer()
        }
        .padding(.vertical, 3)
    }
}

struct Stat: View {
    let label: String
    let value: String
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label.uppercased())
                .font(.system(size: 9.5, weight: .semibold))
                .tracking(0.6)
                .foregroundStyle(.tertiary)
            Text(value)
                .font(.system(size: 24, weight: .semibold, design: .monospaced))
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(nsColor: .controlBackgroundColor))
        .cornerRadius(8)
    }
}

struct BarList: View {
    let data: [(String, Int)]
    var body: some View {
        let maxV = max(data.first?.1 ?? 1, 1)
        VStack(alignment: .leading, spacing: 4) {
            if data.isEmpty {
                Text("(no data)")
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(.tertiary)
            }
            ForEach(data, id: \.0) { (k, v) in
                HStack(spacing: 10) {
                    Text(k)
                        .font(.system(size: 11.5, design: .monospaced))
                        .frame(width: 200, alignment: .leading)
                        .foregroundStyle(.primary)
                    GeometryReader { g in
                        ZStack(alignment: .leading) {
                            Rectangle().fill(Color(nsColor: .quaternaryLabelColor)).frame(height: 4).cornerRadius(2)
                            Rectangle().fill(Color.accentColor).frame(width: g.size.width * CGFloat(Double(v) / Double(maxV)), height: 4).cornerRadius(2)
                        }
                    }
                    .frame(height: 4)
                    Text("\(v)")
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .frame(width: 40, alignment: .trailing)
                }
            }
        }
    }
}

struct DiagList: View {
    let rows: [String]
    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            if rows.isEmpty {
                Text("(no data)").font(.system(size: 11, design: .monospaced)).foregroundStyle(.tertiary)
            }
            ForEach(Array(rows.enumerated()), id: \.offset) { _, r in
                Text(r)
                    .font(.system(size: 10.5, design: .monospaced))
                    .foregroundStyle(.primary)
                    .lineLimit(1)
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(nsColor: .controlBackgroundColor))
        .cornerRadius(6)
    }
}

// MARK: - Footer

struct FooterBar: View {
    @ObservedObject var model: SettingsModel
    var body: some View {
        HStack {
            Text(model.statusLine)
                .font(.system(size: 10.5, design: .monospaced))
                .foregroundStyle(.secondary)
                .lineLimit(1)
            Spacer()
            Button("Revert") { Task { await model.refresh() } }
                .buttonStyle(.bordered)
            Button("Save") { Task { await model.save() } }
                .buttonStyle(.borderedProminent)
                .disabled(!model.dirty)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .background(Color(nsColor: .windowBackgroundColor))
        .overlay(
            Rectangle()
                .fill(Color(nsColor: .separatorColor))
                .frame(height: 0.5),
            alignment: .top
        )
    }
}

// MARK: - Formatters

let intFormatter: NumberFormatter = {
    let f = NumberFormatter(); f.numberStyle = .none; f.minimumFractionDigits = 0
    return f
}()
let doubleFormatter: NumberFormatter = {
    let f = NumberFormatter(); f.numberStyle = .decimal; f.maximumFractionDigits = 2
    return f
}()
