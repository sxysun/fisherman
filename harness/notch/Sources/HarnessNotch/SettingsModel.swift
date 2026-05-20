import Combine
import Foundation

/// View-model bridging the settings UI to the harness daemon's HTTP config.
/// Holds a working copy that the user edits; Save serializes it back to TOML.
@MainActor
final class SettingsModel: ObservableObject {
    // Live state from /dashboard/data
    @Published var data: JSON?
    @Published var metrics: JSON?
    @Published var implicitData: JSON?
    @Published var implicitExamples: [[String: Any]] = []
    @Published var implicitWindow: String = "7d"
    @Published var labData: JSON?
    @Published var labWindow: String = "7d"
    @Published var evalData: JSON?
    @Published var nextStepData: JSON?
    @Published var informationDietData: JSON?
    @Published var pipelineWindow: String = "7d"
    @Published var dietWindow: String = "7d"
    // Policy state from /status
    @Published var snoozedUntil: String?

    // Settings, deep-copied from /dashboard/config on refresh, edited in place
    @Published var pollInterval: Int = 5
    @Published var fishermanURL: String = "http://localhost:7892"
    @Published var activePolicy: String = "rule_v0"
    @Published var cooldownMin: Double = 5
    @Published var negativeFeedbackBackoffMin: Double = 15
    @Published var quietStart: Int = 22
    @Published var quietEnd: Int = 8
    @Published var experimentEnabled: Bool = true
    @Published var experimentSalt: String = "local_v1"
    @Published var holdoutRate: Double = 0.02
    @Published var explorePingRate: Double = 0.0

    @Published var realizerBaseURL: String = ""
    @Published var realizerModel: String = ""
    @Published var realizerApiKey: String = ""
    @Published var maxTokens: Int = 80
    @Published var timeoutSec: Int = 45
    @Published var includeVision: Bool = true
    @Published var skipVisionOnSensitiveOCR: Bool = true
    @Published var redactSensitiveScreenshots: Bool = true
    @Published var blockUntrustedModelHosts: Bool = true
    @Published var allowedModelHostsText: String = "3.82.134.133:8642, openrouter.ai, localhost, 127.0.0.1, ::1"

    @Published var rewardWelcomed: Double = 3.0
    @Published var rewardAnnoying: Double = -5.0
    @Published var rewardPrivacy: Double = -8.0
    @Published var rewardDuplicate: Double = -1.0

    // Scene Reader (VLM) settings
    @Published var vlmEnabled: Bool = false
    @Published var vlmBaseURL: String = "https://openrouter.ai/api/v1"
    @Published var vlmModel: String = "google/gemma-3-4b-it"
    @Published var vlmApiKey: String = ""
    @Published var vlmMinInterval: Int = 30
    @Published var vlmTimeoutSec: Int = 12

    @Published var intentsEnabled: Set<String> = []
    @Published var intentsMuted: Set<String> = []

    // Today (goal-driven)
    @Published var dailyGoal: String = ""
    @Published var sensitivity: String = "balanced"
    @Published var goalSetAt: String? = nil

    // Diagnostics arrays
    @Published var recentDecisions: [[String: Any]] = []
    @Published var recentOutcomes: [[String: Any]] = []
    @Published var recentRealizations: [[String: Any]] = []
    @Published var recentModelCalls: [[String: Any]] = []

    // Dirty tracking
    @Published var dirty: Bool = false
    @Published var statusLine: String = ""

    private var rawConfig: JSON?
    private var pollTask: Task<Void, Never>?
    private var observers: [AnyCancellable] = []

    init() {
        // mark dirty whenever any editable field changes
        let fields: [AnyPublisher<Void, Never>] = [
            $pollInterval.map { _ in () }.eraseToAnyPublisher(),
            $fishermanURL.map { _ in () }.eraseToAnyPublisher(),
            $activePolicy.map { _ in () }.eraseToAnyPublisher(),
            $cooldownMin.map { _ in () }.eraseToAnyPublisher(),
            $negativeFeedbackBackoffMin.map { _ in () }.eraseToAnyPublisher(),
            $quietStart.map { _ in () }.eraseToAnyPublisher(),
            $quietEnd.map { _ in () }.eraseToAnyPublisher(),
            $experimentEnabled.map { _ in () }.eraseToAnyPublisher(),
            $experimentSalt.map { _ in () }.eraseToAnyPublisher(),
            $holdoutRate.map { _ in () }.eraseToAnyPublisher(),
            $explorePingRate.map { _ in () }.eraseToAnyPublisher(),
            $realizerBaseURL.map { _ in () }.eraseToAnyPublisher(),
            $realizerModel.map { _ in () }.eraseToAnyPublisher(),
            $realizerApiKey.map { _ in () }.eraseToAnyPublisher(),
            $maxTokens.map { _ in () }.eraseToAnyPublisher(),
            $timeoutSec.map { _ in () }.eraseToAnyPublisher(),
            $includeVision.map { _ in () }.eraseToAnyPublisher(),
            $skipVisionOnSensitiveOCR.map { _ in () }.eraseToAnyPublisher(),
            $redactSensitiveScreenshots.map { _ in () }.eraseToAnyPublisher(),
            $blockUntrustedModelHosts.map { _ in () }.eraseToAnyPublisher(),
            $allowedModelHostsText.map { _ in () }.eraseToAnyPublisher(),
            $rewardWelcomed.map { _ in () }.eraseToAnyPublisher(),
            $rewardAnnoying.map { _ in () }.eraseToAnyPublisher(),
            $rewardPrivacy.map { _ in () }.eraseToAnyPublisher(),
            $rewardDuplicate.map { _ in () }.eraseToAnyPublisher(),
            $vlmEnabled.map { _ in () }.eraseToAnyPublisher(),
            $vlmBaseURL.map { _ in () }.eraseToAnyPublisher(),
            $vlmModel.map { _ in () }.eraseToAnyPublisher(),
            $vlmApiKey.map { _ in () }.eraseToAnyPublisher(),
            $vlmMinInterval.map { _ in () }.eraseToAnyPublisher(),
            $vlmTimeoutSec.map { _ in () }.eraseToAnyPublisher(),
            $intentsEnabled.map { _ in () }.eraseToAnyPublisher(),
        ]
        for p in fields {
            p.dropFirst().sink { [weak self] in self?.dirty = true }.store(in: &observers)
        }
    }

    func refresh() async {
        async let d = HarnessAPI.fetchData()
        async let c = HarnessAPI.fetchConfig()
        async let p = HarnessAPI.fetchPolicyState()
        async let m = HarnessAPI.fetchMetrics()
        async let i = HarnessAPI.fetchImplicit(window: implicitWindow, limit: 80)
        async let lab = HarnessAPI.fetchLab(window: labWindow)
        async let eval = HarnessAPI.fetchEvalReport(window: pipelineWindow)
        async let next = HarnessAPI.fetchNextSteps(window: pipelineWindow)
        async let diet = HarnessAPI.fetchInformationDiet(window: dietWindow)
        let (data, config, policy, metrics, implicit, labData, evalData, nextStepData, dietData) = await (d, c, p, m, i, lab, eval, next, diet)
        self.data = data
        self.metrics = metrics
        applyImplicit(implicit)
        self.labData = labData
        self.evalData = evalData
        self.nextStepData = nextStepData
        self.informationDietData = dietData
        self.rawConfig = config
        if let p = policy {
            self.snoozedUntil = p["snoozed_until"].string.isEmpty ? nil : p["snoozed_until"].string
            self.intentsMuted = Set(p["muted_intents"].list.compactMap { $0 as? String })
            self.dailyGoal = p["daily_goal"].string
            let s = p["sensitivity"].string
            self.sensitivity = s.isEmpty ? "balanced" : s
            self.goalSetAt = p["goal_set_at"].string.isEmpty ? nil : p["goal_set_at"].string
        }
        if let c = config {
            applyConfig(c)
        }
        if let data = data {
            self.recentDecisions = (data["recent_decisions"].list).compactMap { $0 as? [String: Any] }
            self.recentOutcomes = (data["recent_outcomes"].list).compactMap { $0 as? [String: Any] }
            self.recentRealizations = (data["recent_realizations"].list).compactMap { $0 as? [String: Any] }
            self.recentModelCalls = (data["recent_model_calls"].list).compactMap { $0 as? [String: Any] }
        }
        let labelCount = metrics?["labels"]["n"].int ?? 0
        let implicitUsable = implicit?["summary"]["usable"].int ?? 0
        self.statusLine = "daemon ok · \(data?["n_candidates"].int ?? 0) candidates / \(data?["n_decisions"].int ?? 0) decisions · \(labelCount) labels · \(implicitUsable) implicit"
        self.dirty = false
    }

    func refreshImplicit() async {
        let implicit = await HarnessAPI.fetchImplicit(window: implicitWindow, limit: 80)
        applyImplicit(implicit)
    }

    func refreshLab() async {
        self.labData = await HarnessAPI.fetchLab(window: labWindow)
    }

    func refreshPipeline() async {
        async let eval = HarnessAPI.fetchEvalReport(window: pipelineWindow)
        async let next = HarnessAPI.fetchNextSteps(window: pipelineWindow)
        let (evalData, nextStepData) = await (eval, next)
        self.evalData = evalData
        self.nextStepData = nextStepData
    }

    func refreshDiet() async {
        self.informationDietData = await HarnessAPI.fetchInformationDiet(window: dietWindow)
    }

    func promoteImplicit(decisionID: String, label: String, implicitLabel: String, implicitDirection: String) async {
        let ok = await HarnessAPI.promoteImplicit(
            decisionID: decisionID,
            label: label,
            implicitLabel: implicitLabel,
            implicitDirection: implicitDirection
        )
        statusLine = ok ? "promoted implicit example · \(label)" : "promote failed"
        if ok {
            async let m = HarnessAPI.fetchMetrics()
            async let lab = HarnessAPI.fetchLab(window: labWindow)
            let (metrics, labData) = await (m, lab)
            self.metrics = metrics
            self.labData = labData
        }
    }

    func runTrainer() async {
        let result = await HarnessAPI.runTrainer(window: "30d")
        labData = await HarnessAPI.fetchLab(window: labWindow)
        let canary = result?["canary_policy"]
        statusLine = "trainer \(canary?["status"].string ?? "done") · \(canary?["variant"].string ?? "n/a")"
    }

    func activateCanary() async {
        let result = await HarnessAPI.activateCanary()
        labData = await HarnessAPI.fetchLab(window: labWindow)
        statusLine = result?["ok"].bool == true ? "canary active" : "canary activation failed"
    }

    func rollbackCanary() async {
        let result = await HarnessAPI.rollbackCanary()
        labData = await HarnessAPI.fetchLab(window: labWindow)
        statusLine = result?["ok"].bool == true ? "canary rolled back" : "rollback failed"
    }

    func startPolling() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 5_000_000_000)
                if Task.isCancelled { break }
                // Refresh non-editable data only, don't clobber user edits.
                let window = await MainActor.run { self?.implicitWindow ?? "7d" }
                let d = await HarnessAPI.fetchData()
                let p = await HarnessAPI.fetchPolicyState()
                let m = await HarnessAPI.fetchMetrics()
                let i = await HarnessAPI.fetchImplicit(window: window, limit: 80)
                let pipelineWindow = await MainActor.run { self?.pipelineWindow ?? "7d" }
                let dietWindow = await MainActor.run { self?.dietWindow ?? "7d" }
                async let eval = HarnessAPI.fetchEvalReport(window: pipelineWindow)
                async let next = HarnessAPI.fetchNextSteps(window: pipelineWindow)
                async let diet = HarnessAPI.fetchInformationDiet(window: dietWindow)
                let (evalData, nextStepData, dietData) = await (eval, next, diet)
                await MainActor.run {
                    guard let self = self else { return }
                    self.data = d
                    self.metrics = m
                    self.applyImplicit(i)
                    self.evalData = evalData
                    self.nextStepData = nextStepData
                    self.informationDietData = dietData
                    if let p = p {
                        self.snoozedUntil = p["snoozed_until"].string.isEmpty ? nil : p["snoozed_until"].string
                        self.intentsMuted = Set(p["muted_intents"].list.compactMap { $0 as? String })
                        // Don't clobber dailyGoal while user might be typing —
                        // only refresh "set at" timestamp.
                        self.goalSetAt = p["goal_set_at"].string.isEmpty ? nil : p["goal_set_at"].string
                    }
                    if let data = d {
                        self.recentDecisions = (data["recent_decisions"].list).compactMap { $0 as? [String: Any] }
                        self.recentOutcomes = (data["recent_outcomes"].list).compactMap { $0 as? [String: Any] }
                        self.recentRealizations = (data["recent_realizations"].list).compactMap { $0 as? [String: Any] }
                        self.recentModelCalls = (data["recent_model_calls"].list).compactMap { $0 as? [String: Any] }
                    }
                    let labelCount = m?["labels"]["n"].int ?? 0
                    let implicitUsable = i?["summary"]["usable"].int ?? 0
                    self.statusLine = "daemon ok · \(d?["n_candidates"].int ?? 0) candidates / \(d?["n_decisions"].int ?? 0) decisions · \(labelCount) labels · \(implicitUsable) implicit"
                }
            }
        }
    }

    func stopPolling() {
        pollTask?.cancel()
        pollTask = nil
    }

    private func applyConfig(_ c: JSON) {
        pollInterval = c["daemon"]["poll_interval_sec"].int
        fishermanURL = c["daemon"]["fisherman_url"].string
        activePolicy = c["gate"]["active_policy"].string
        cooldownMin = c["gate"]["cooldown_min"].double
        let backoff = c["gate"]["negative_feedback_backoff_min"].double
        negativeFeedbackBackoffMin = backoff == 0 ? 15 : backoff
        quietStart = c["gate"]["quiet_hours_start"].int
        quietEnd = c["gate"]["quiet_hours_end"].int

        let exp = c["experiment"]
        experimentEnabled = exp["enabled"].raw is NSNull ? true : exp["enabled"].bool
        experimentSalt = exp["salt"].string.isEmpty ? "local_v1" : exp["salt"].string
        holdoutRate = exp["holdout_rate"].raw is NSNull ? 0.02 : exp["holdout_rate"].double
        explorePingRate = exp["explore_ping_rate"].raw is NSNull ? 0.0 : exp["explore_ping_rate"].double

        realizerBaseURL = c["realizer"]["base_url"].string
        realizerModel = c["realizer"]["model"].string
        realizerApiKey = c["realizer"]["api_key"].string
        maxTokens = c["realizer"]["max_tokens"].int
        timeoutSec = c["realizer"]["timeout_sec"].int
        includeVision = c["realizer"]["include_vision"].bool
        skipVisionOnSensitiveOCR = c["realizer"]["skip_vision_on_sensitive_ocr"].raw is NSNull
            ? true
            : c["realizer"]["skip_vision_on_sensitive_ocr"].bool
        redactSensitiveScreenshots = c["realizer"]["redact_sensitive_screenshots"].raw is NSNull
            ? true
            : c["realizer"]["redact_sensitive_screenshots"].bool

        let privacy = c["privacy"]
        blockUntrustedModelHosts = privacy["block_untrusted_model_hosts"].raw is NSNull
            ? true
            : privacy["block_untrusted_model_hosts"].bool
        let allowed = privacy["allowed_model_hosts"].list.compactMap { $0 as? String }
        if !allowed.isEmpty {
            allowedModelHostsText = allowed.joined(separator: ", ")
        }

        rewardWelcomed = c["reward"]["weights"]["welcomed"].double
        rewardAnnoying = c["reward"]["weights"]["annoying"].double
        rewardPrivacy = c["reward"]["weights"]["privacy"].double
        rewardDuplicate = c["reward"]["weights"]["duplicate"].double

        // Scene Reader (VLM) — config path is scene_tagger.llm
        let vlm = c["scene_tagger"]["llm"]
        vlmEnabled    = vlm["enabled"].bool
        vlmBaseURL    = vlm["base_url"].string.isEmpty ? "https://openrouter.ai/api/v1" : vlm["base_url"].string
        vlmModel      = vlm["model"].string.isEmpty ? "google/gemma-3-4b-it" : vlm["model"].string
        vlmApiKey     = vlm["api_key"].string
        vlmMinInterval = vlm["min_interval_sec"].int == 0 ? 30 : vlm["min_interval_sec"].int
        vlmTimeoutSec = vlm["timeout_sec"].int == 0 ? 12 : vlm["timeout_sec"].int

        intentsEnabled = Set(c["intents"]["enabled"].list.compactMap { $0 as? String })
        dirty = false
    }

    private func applyImplicit(_ implicit: JSON?) {
        self.implicitData = implicit
        self.implicitExamples = (implicit?["examples"].list ?? []).compactMap { $0 as? [String: Any] }
    }

    func save() async {
        guard var c = rawConfig else {
            statusLine = "save failed: no config loaded"
            return
        }
        c["daemon"]["poll_interval_sec"] = JSON(any: pollInterval)
        c["daemon"]["fisherman_url"] = JSON(any: fishermanURL)
        c["gate"]["active_policy"] = JSON(any: activePolicy)
        c["gate"]["cooldown_min"] = JSON(any: cooldownMin)
        c["gate"]["negative_feedback_backoff_min"] = JSON(any: negativeFeedbackBackoffMin)
        c["gate"]["quiet_hours_start"] = JSON(any: quietStart)
        c["gate"]["quiet_hours_end"] = JSON(any: quietEnd)

        var expBlock = c["experiment"].dict
        expBlock["enabled"] = experimentEnabled
        expBlock["salt"] = experimentSalt.isEmpty ? "local_v1" : experimentSalt
        expBlock["holdout_rate"] = holdoutRate
        expBlock["explore_ping_rate"] = explorePingRate
        expBlock["respect_hard_gates"] = (expBlock["respect_hard_gates"] as? Bool) ?? true
        expBlock["explore_eligible_reasons"] = (expBlock["explore_eligible_reasons"] as? [String]) ?? ["no_clear_help"]
        c["experiment"] = JSON(any: expBlock)

        c["realizer"]["base_url"] = JSON(any: realizerBaseURL)
        c["realizer"]["model"] = JSON(any: realizerModel)
        c["realizer"]["api_key"] = JSON(any: realizerApiKey)
        c["realizer"]["max_tokens"] = JSON(any: maxTokens)
        c["realizer"]["timeout_sec"] = JSON(any: timeoutSec)
        c["realizer"]["include_vision"] = JSON(any: includeVision)
        c["realizer"]["skip_vision_on_sensitive_ocr"] = JSON(any: skipVisionOnSensitiveOCR)
        c["realizer"]["redact_sensitive_screenshots"] = JSON(any: redactSensitiveScreenshots)

        var privacyBlock = c["privacy"].dict
        privacyBlock["block_untrusted_model_hosts"] = blockUntrustedModelHosts
        privacyBlock["allow_local_model_hosts"] = (privacyBlock["allow_local_model_hosts"] as? Bool) ?? true
        privacyBlock["allowed_model_hosts"] = allowedModelHostsText
            .split { $0 == "," || $0 == "\n" || $0 == " " || $0 == "\t" }
            .map { String($0).trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        c["privacy"] = JSON(any: privacyBlock)

        var rewardBlock = c["reward"].dict
        var weights = (rewardBlock["weights"] as? [String: Any]) ?? [:]
        weights["welcomed"] = rewardWelcomed
        weights["annoying"] = rewardAnnoying
        weights["privacy"] = rewardPrivacy
        weights["duplicate"] = rewardDuplicate
        rewardBlock["weights"] = weights
        c["reward"] = JSON(any: rewardBlock)

        // Scene Reader (VLM) config — nested table scene_tagger.llm
        var sceneTaggerBlock = c["scene_tagger"].dict
        var llmBlock = (sceneTaggerBlock["llm"] as? [String: Any]) ?? [:]
        llmBlock["enabled"] = vlmEnabled
        llmBlock["base_url"] = vlmBaseURL
        llmBlock["model"] = vlmModel
        llmBlock["api_key"] = vlmApiKey
        llmBlock["api_key_env"] = (llmBlock["api_key_env"] as? String) ?? "OPENROUTER_API_KEY"
        llmBlock["min_interval_sec"] = vlmMinInterval
        llmBlock["timeout_sec"] = vlmTimeoutSec
        sceneTaggerBlock["llm"] = llmBlock
        c["scene_tagger"] = JSON(any: sceneTaggerBlock)

        c["intents"]["enabled"] = JSON(any: Array(intentsEnabled))

        let ok = await HarnessAPI.saveConfig(c)
        if ok {
            rawConfig = c
            dirty = false
            statusLine = "saved · restart daemon to apply"
        } else {
            statusLine = "save failed"
        }
    }
}
