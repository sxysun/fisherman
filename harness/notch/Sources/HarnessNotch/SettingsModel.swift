import Combine
import Foundation

/// View-model bridging the settings UI to the harness daemon's HTTP config.
/// Holds a working copy that the user edits; Save serializes it back to TOML.
@MainActor
final class SettingsModel: ObservableObject {
    // Live state from /dashboard/data
    @Published var data: JSON?
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

    @Published var realizerBaseURL: String = ""
    @Published var realizerModel: String = ""
    @Published var realizerApiKey: String = ""
    @Published var maxTokens: Int = 80
    @Published var timeoutSec: Int = 45
    @Published var includeVision: Bool = true
    @Published var skipVisionOnSensitiveOCR: Bool = true
    @Published var redactSensitiveScreenshots: Bool = true

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
            $realizerBaseURL.map { _ in () }.eraseToAnyPublisher(),
            $realizerModel.map { _ in () }.eraseToAnyPublisher(),
            $realizerApiKey.map { _ in () }.eraseToAnyPublisher(),
            $maxTokens.map { _ in () }.eraseToAnyPublisher(),
            $timeoutSec.map { _ in () }.eraseToAnyPublisher(),
            $includeVision.map { _ in () }.eraseToAnyPublisher(),
            $skipVisionOnSensitiveOCR.map { _ in () }.eraseToAnyPublisher(),
            $redactSensitiveScreenshots.map { _ in () }.eraseToAnyPublisher(),
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
        let (data, config, policy) = await (d, c, p)
        self.data = data
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
        }
        self.statusLine = "daemon ok · \(data?["n_candidates"].int ?? 0) candidates / \(data?["n_decisions"].int ?? 0) decisions today"
        self.dirty = false
    }

    func startPolling() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 5_000_000_000)
                if Task.isCancelled { break }
                // Refresh non-editable data only, don't clobber user edits.
                let d = await HarnessAPI.fetchData()
                let p = await HarnessAPI.fetchPolicyState()
                await MainActor.run {
                    guard let self = self else { return }
                    self.data = d
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
                    }
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

        c["realizer"]["base_url"] = JSON(any: realizerBaseURL)
        c["realizer"]["model"] = JSON(any: realizerModel)
        c["realizer"]["api_key"] = JSON(any: realizerApiKey)
        c["realizer"]["max_tokens"] = JSON(any: maxTokens)
        c["realizer"]["timeout_sec"] = JSON(any: timeoutSec)
        c["realizer"]["include_vision"] = JSON(any: includeVision)
        c["realizer"]["skip_vision_on_sensitive_ocr"] = JSON(any: skipVisionOnSensitiveOCR)
        c["realizer"]["redact_sensitive_screenshots"] = JSON(any: redactSensitiveScreenshots)

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
