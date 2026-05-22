# Harness Hand-off

You're picking up a proactive presence harness for macOS. The substrate is built. The system runs end-to-end. The next big work is **dogfooding** + **iterating on signal**, with only targeted infrastructure work when it improves learning from real outcomes.

Read this doc + skim `README.md` and you'll know the system.

---

## What the harness is

A daemon that watches the user's screen via [Fisherman](../fisherman/) and decides, every ~5 seconds, whether **now is a good moment to interrupt them with a short message**. When it decides yes, an OpenAI-compatible LLM (the user has [Nous Hermes Agent](https://github.com/NousResearch/hermes-agent) running on their EC2 instance) composes the message, a critic vets it, and a separate macOS floating capsule renders it.

The user wanted this to:
1. Serve a daily intention they declare each morning
2. Surface information that helps the current task (silent research, open threads, focus drift)
3. Never feel ad-hoc or "AI-assistant warm" — terse, direct, like a colleague

The harness produces **traces**: structured logs of (state, decision, message, outcome) per tick. It also produces **workflow events**: compact app/window runs that preserve how the user got to the current screen. The current live policy is an LLM in-context binary classifier (`llm_icl_v0`) guarded by the deterministic `rule_v0` safety baseline.

---

## The five-process picture

```
┌─────────────────────────┐    HTTP :7892    ┌─────────────────────────┐
│  Fisherman daemon       │◀─────────────────│  Harness daemon (Python)│
│  (user has installed)   │   (read-only)    │  port :7893             │
│                         │                  │                         │
│  /status /frames /query │                  │  poll → scene tag →     │
│  /transcripts           │                  │  memory → gate →        │
│                         │                  │  if ping: realizer →    │
│  captures screen, runs  │                  │  critic → push          │
│  Apple Vision OCR       │                  │                         │
└─────────────────────────┘                  └─────────────────────────┘
                                                  │             ▲
                                          HTTPS   │             │  HTTP :7893
                                   to OpenRouter  │             │
                                       (VLM)      ▼             │
                              ┌─────────────────────┐    ┌──────────────────┐
                              │  google/gemma-3-4b  │    │  HarnessNotch    │
                              │  per-candidate VLM  │    │  (Swift, native) │
                              │  scene tagger       │    │                  │
                              └─────────────────────┘    │  floating pill + │
                                                         │  capsule tabs    │
                                  HTTPS to user's EC2    │  Settings/Diet   │
                              ┌─────────────────────┐    │                  │
                              │  hermes-agent       │◀───┤  uses Dynamic-   │
                              │  (their LLM)        │    │  NotchKit (same  │
                              │  ~16k+image in →    │    │  lib FishermanMenu│
                              │  ≤80 tokens out     │    │  uses for parity)│
                              └─────────────────────┘    └──────────────────┘
```

Plus a labeling web UI at `:7893/label` for retrospective labels (rewind-style scrubber).

---

## Files that matter

```
harness/
├── HANDOFF.md ← you are here
├── README.md
├── pyproject.toml                console_script: harness = harness.cli:main
│
├── harness/                      Python package
│   ├── cli.py                    `harness <verb>` CLI
│   ├── daemon.py                 the loop: poll → scene → gate → realize → critic → push
│   ├── server.py                 HTTP server on :7893 (/pending /outcome /goal etc)
│   ├── fisherman_client.py       HTTP client to Fisherman (only file aware of its API)
│   ├── candidate.py              builds CandidateEvent from Fisherman reads
│   ├── scene.py                  rule-based scene tagger (fast path)
│   ├── scene_vlm.py              per-candidate VLM scene tagger (smart-triggered)
│   ├── memory.py                 rolling 2h session + content-addressed snapshots
│   ├── workflow_events.py        local app/window run eventizer for policy trajectory
│   ├── gate.py                   loads policy module by name
│   ├── realizer.py               openai-compatible agent loop, sends vision JPEG
│   │                              unless privacy preflight suppresses it
│   ├── critic.py                 regex + LLM veto
│   ├── push.py                   notch_pill or terminal_notifier backend
│   ├── store.py                  jsonl append/tail + SQLite mirroring hook
│   ├── sql_store.py              typed SQLite sidecar and JSONL backfill helpers
│   ├── reward.py                 signal-derived reward (replaces ad-hoc weights)
│   ├── privacy.py                local OCR secret detection + text redaction
│   ├── image_redaction.py        local Apple Vision box masking for screenshots
│   ├── model_audit.py            privacy-safe model-call audit ledger
│   ├── schemas.py                ALL dataclasses
│   ├── config.py                 TOML config + default
│   ├── label_ui.py               rewind-style labeling web UI with frozen queue
│   ├── metrics.py                live outcome + retro-label quality metrics
│   ├── eval_report.py            joined OpenAdapt-style eval report
│   └── dashboard_ui.py           dashboard/settings/diag web UI; capsule settings is primary
│
├── policies/
│   ├── rule_v0.py                deterministic safety/baseline policy
│   └── llm_icl_v0.py             default LLM ICL ping/not-ping policy learner
│
├── prompts/
│   ├── realizer/goal_aware_v1.md ONLY realizer prompt. Goal-driven.
│   ├── realizer/_archive/        4 old intent prompts (deprecated, kept for ref)
│   ├── critic/productivity_v1.md
│   └── scene_tagger/llm_fallback_v1.md
│
├── eval/                         CLI-driven offline tools
│   ├── replay.py                 shadow-replay a policy on frozen candidates
│   └── score.py                  replay scoring + reward_v2 from outcomes/labels
│
├── notch/                        Swift package
│   ├── Package.swift             depends on ../../menubar/Packages/DynamicNotchKit
│   ├── Sources/HarnessNotch/
│   │   ├── App.swift             entry; installs Edit menu (for Cmd+C/V/X/A in
│   │   │                          .accessory app — non-obvious requirement)
│   │   ├── NotchCoordinator      polls /pending, drives DynamicNotch.expand/hide,
│   │   │                          tracks mouse approach + hover events
│   │   ├── NotchWindow           (now obsolete after DynamicNotchKit refactor —
│   │   │                          may be deleted; check usage)
│   │   ├── HarnessExpanded.swift expanded pill content (status dot + msg + buttons)
│   │   ├── HarnessClient.swift   minimal client for /pending + /outcome
│   │   ├── HarnessAPI.swift      richer client for settings: config/data/goal
│   │   ├── SettingsModel         ObservableObject bridging HTTP ↔ capsule settings
│   │   └── HarnessState.swift    ObservedObject for the live capsule/ping
│   └── build.sh                  → installs binary to ~/.harness/HarnessNotch
│
└── tests/test_smoke.py           52 tests; pytest passes
```

State on disk (outside the repo):

```
~/.harness/
├── config.toml                   user-editable config (TOML)
├── policy.json                   runtime state: daily_goal, sensitivity, snooze, mutes
├── HarnessNotch                  Swift binary, launched by daemon
├── candidates.jsonl              every CandidateEvent
├── workflow_events.jsonl         closed app/window workflow runs
├── decisions.jsonl               every gate decision
├── deliveries.jsonl              notch claim/display ledger
├── outcomes.jsonl                user reactions + interaction_summary
├── traces.jsonl                  joined view per tick
├── retro_labels.jsonl            from the labeling UI
├── model_calls.jsonl             privacy-safe model-call audit rows
├── harness.db                    SQLite sidecar with typed query tables
├── memory/
│   ├── session.jsonl
│   └── snapshots/mem_<sha>.json  content-addressed
└── pending/<id>.json             queue between daemon and notch app
```

---

## How to run it

```bash
cd ~/Desktop/suapp/fisherman/harness

# install (idempotent — won't clobber existing config)
.venv/bin/harness install --build-notch

# start daemon (will fork the notch app subprocess automatically)
.venv/bin/harness start --foreground

# in another shell, fire a test pill (skips gate, calls hermes for real)
.venv/bin/harness test --intent focus_nudge --push
.venv/bin/harness test --message "// TODO: rate limit at 100rps" --intent surface_open_thread --push

# stop cleanly
.venv/bin/harness stop
```

User flow once it's running:
1. Hover the Harness floating capsule → Settings
2. Write what you're trying to do, pick sensitivity/policy, save
3. Let the harness run; pings appear in the floating capsule when conditions match
4. Click [Yes] / [Later] / [×] on the pill, or just ignore
5. Periodically: `harness label` → open browser, drag the scrubber, label past decisions
6. Weekly: `harness collect`, `harness replay`, `harness score` to compare policies

---

## Current state — what works

```
✅ End-to-end pipeline live
   poll → scene → memory → gate → realizer → critic → push → outcome → trace

✅ Vision (VLM) in two places
   - Per-candidate scene tagger (google/gemma-3-4b-it on OpenRouter when
     enabled). Smart-triggered: only fires when app+OCR change and ≥30s since
     last call. Default config keeps this off until configured.
   - Realizer (hermes-agent, multimodal): sees current JPEG when composing messages
     unless local OCR privacy preflight suppresses image attachment; sensitive
     JPEGs are locally masked first when Apple Vision can locate matching boxes
   Together: VLM can compensate when Fisherman's frontmost_app metadata is stale

✅ Goal-driven model
   - Daily goal field in the floating capsule Settings tab
   - Sensitivity (gentle/balanced/responsive) → cooldown_min mapping
   - Single goal_aware_v1.md prompt; no fixed intent catalog
   - reason_codes from gate flow directly to the realizer
   - Sleep/resume gaps are now explicit (`capture_gap_sec`,
     `last_event_gap_sec`, `session_boundary`) so long-session messages do
     not count closed-laptop time as active work

✅ Workflow eventization
   - `workflow_events.py` groups adjacent candidates into app/window runs
   - Closes runs on app/title changes, stale/inactive/sensitive frames,
     capture gaps, time gaps, or daemon shutdown
   - Closed rows go to `workflow_events.jsonl` and the SQLite
     `workflow_events` table
   - Recent compact runs are stored in `MemorySnapshot.recent_workflow_events`
     and included in the `llm_icl_v0` ping/not-ping policy prompt

✅ Signal-derived reward (reward_v2)
   - clicked +2, considered +0.5, approached -0.2, ignored -1, dismissed -1.5
   - Computed from interaction_summary.intent_signal (hover/approach tracking)
   - Timeout hover feedback is dwell-based: the dominant hovered button wins,
     so a brief brush over Dismiss no longer overrides a longer Later/Yes hover
   - Replaces the ad-hoc 3/-5/-8/-1 weights

✅ Floating capsule settings UI
   - DynamicNotchKit-based pill matches FishermanMenu aesthetic
   - Joins all macOS Spaces/fullscreen desktops; a visibility watchdog
     reasserts placement after Space/display changes
   - Harness menubar item removed; settings, labeler, dashboard, and snooze
     controls live in the floating capsule
   - Edit menu installed (Cmd+C/V/X/A work despite .accessory policy)
   - API-key fields show masked previews and require explicit edit to reveal

✅ Retro labeling UI at :7893/label
   - Rewind-style: drag scrubber, ±2min window, ~60 thumbnails
   - Frozen review-session cutoff, so live daemon ticks do not keep jumping
     the queue to the latest candidate
   - Session skip state, action/order filters, confidence, and notes
   - Clear rubric: Should ping / Should stay quiet / Can't tell
   - Keyboard 1/2/3 + arrow scrubbing + space play + S skip
   - Feeds reward_v2 + future few-shot personalization

✅ Live lab metrics

✅ Information-diet report
   - `harness info-diet --since 7d` and Dashboard → Diet summarize
     browser-like research episodes, OCR-inferred domains/query phrases,
     dwell patterns, and tentative workflow hypotheses
   - This is deliberately conservative: useful for inspecting tacit workflow
     evidence, not a trusted skill compiler until Fisherman exposes direct
     URL/title and downstream artifact links
   - `harness metrics --since 24h` reports ping rate, outcome capture,
     avg reward, retro-label agreement, false-interruption rate, missed-help
     rate, and readiness thresholds
   - Delivery capture is split into queued vs notch-claimed pings, so eval no
     longer treats realizer/critic skips as missing user outcomes
   - `harness eval-report --since 7d` includes intervention taxonomy,
     binary policy calibration, and compact non-green examples
   - `/metrics?window=24h` exposes the same JSON from the daemon
   - Capsule Pipeline tab now shows the live lab counters and label
     readiness, so the user can tell whether the harness is learning or only
     collecting sparse anecdotes

✅ Shadow-policy comparison
   - `harness shadow --since 24h` compares rule_v0 variants against retro labels
   - Default mode evaluates labeled candidates only so it stays interactive
   - Reports labeled precision/recall/F1, false-interruption rate, missed-help
     rate, agreement, and Wilson 95% intervals in JSON mode
   - `--full` replays the full candidate set when ping-rate comparison matters

✅ Deterministic experiment assignment
   - `[experiment]` config is merged into old local configs automatically
   - 2% default holdout suppresses a small fraction of would-ping decisions
     and logs `counterfactual_action="notch_ping"`
   - Exploration pings default to 3% on eligible ambiguous moments, enough to
     learn without making random interruption the dominant behavior
   - Capsule Settings exposes the live policy choice, holdout, confidence, and
     exploration rate

✅ Outcome capture rich enough for RL
   - Per outcome: clicked/dismissed/snoozed/timed_out
   - + interaction_summary with hover targets, approach count, intent_signal tier
   - Hovering dismiss and then timing out is treated as soft rejection and
     feeds the live recent-negative-feedback backoff
   - `harness implicit --since 7d` converts notification behavior into
     confidence-weighted weak labels without polluting retro_labels.jsonl
   - Metrics now show both explicit retro-label readiness and implicit
     outcome-signal readiness

✅ Harness privacy preflight
   - OCR text is scanned locally for secret-like patterns before model prompts
   - Realizer/tool/critic OCR snippets are redacted before network calls
   - Sensitive frames are masked locally using Apple Vision text boxes before
     screenshot model calls; if masking fails, image attachment is suppressed

✅ Model endpoint trust boundary
   - `[privacy]` config has an allowlist for model hosts
   - Realizer, scene VLM, and LLM critic block untrusted endpoints before
     fetching screenshots or making network calls
   - Capsule Settings exposes model endpoints and API keys for the learner,
     realizer, and scene reader

✅ Model-call audit ledger
   - `~/.harness/model_calls.jsonl` records realizer, scene VLM, and LLM critic
     calls with purpose, endpoint, model, status, latency, token counts, image
     bytes, privacy flags, and hashes/counts only
   - Raw prompts, screenshots, OCR text, API keys, and response text are not
     written to the audit ledger

✅ Typed event-store sidecar
   - JSONL remains the compatibility/export path for local debugging
   - Every JSONL append is mirrored into `~/.harness/harness.db`
   - SQLite stores a generic `event_log` plus typed candidates, decisions,
     traces, outcomes, model calls, and retro labels
   - Late outcome attachment updates both `traces.jsonl` and the typed trace row
   - `harness storage-backfill --reset` rebuilds the sidecar from existing JSONL
   - Dashboard, metrics, replay, score, and shadow comparison prefer typed
     SQLite payload rows and fall back to JSONL when the sidecar is absent

✅ Idempotent pending delivery
   - `/pending` now leases the oldest pending payload instead of deleting it
     at poll time
   - `/outcome` removes the pending payload only after feedback is recorded
   - If HarnessNotch crashes between poll and outcome, the lease expires and
     the message can be claimed again

✅ Launchd + notch restartability
   - `harness install-launchd` writes and loads
     `~/Library/LaunchAgents/com.fisherman.harness.plist`
   - `harness launchd-status` and `harness uninstall-launchd` are available
   - The daemon relaunches HarnessNotch if the notch subprocess exits
```

---

## Known issues / things to verify

```
⚠ Fisherman frontmost_app source fix is not part of the harness commit
   Earlier local work explored using the CG window owner stack before
   NSWorkspace fallback, but the pushed harness commits intentionally avoid
   Fisherman runtime paths. If frontmost_app is still stale in dogfood, make
   and ship that Fisherman change as a separate app/runtime commit.

⚠ Hermes does its tool-using server-side
   We send the brief plus screenshot when privacy preflight allows it, then
   get a message back. Hermes may search its own memory (the user saw
   "rolling summary" in a curl response earlier) but doesn't expose tool_calls
   in the response. We have nothing to surface beyond the final message.
   Tracking field `provider_reasoning` if hermes ever adds it (none right now).

⚠ Settings reload is partial
   Goal, sensitivity, policy state, and several capsule-facing controls update
   live through HTTP. Some TOML-backed endpoint/model/privacy settings are still
   read by the daemon at boot, so restart the launchd job/daemon after changing
   those until config hot reload is complete.

⚠ Reward weights for v1 still in config.toml
   [reward.weights] section is dead code now — reward_v2 is signal-derived
   and doesn't use them. score.py still emits the legacy cost_weighted_utility
   for back-compat. Could remove entirely after a deprecation cycle.

⚠ Negative-feedback backoff is time-bounded
   A dismiss/mute now suppresses organic pings for
   gate.negative_feedback_backoff_min (default 15 min), not forever.

⚠ Ping volume may still be conservative
   Check the current Pipeline/Eval window before tuning. The default policy is
   now `llm_icl_v0` guarded by `rule_v0`, with low-rate exploration, but real
   organic ping quality still needs dogfood data.

⚠ Retro labels still sparse
   The user has started labeling, but the current count is still below the
   20-label personalization threshold and far below the 500-label learned-gate
   threshold. Metrics exist now, but they are not statistically meaningful yet.
```

---

## What to do next, in priority order

### Tier 1 — actually use it

1. **Dogfood for a day.** Set a goal in the floating capsule Settings tab in
   the morning. Let it run. Pay attention to what fires, what should have fired,
   and whether ignored timeouts are correctly treated as attention-cost signal.

2. **Label 20-30 retros.** Open `:7893/label`, drag, press 1-3. This is
   the single highest-leverage thing right now — it unlocks recall metrics
   AND few-shot personalization.

### Tier 2 — small, immediate wins after Tier 1

3. **Tune gate thresholds based on what label data says.** If labels show
   "would have helped" cases that didn't ping, lower the relevant threshold
   in `policies/rule_v0.py`. If "would have annoyed" cases pinged, raise it.

4. **Finish live config reload.** When Settings saves config.toml, the daemon
   should pick up endpoint/model/privacy changes within 5s without restart.
   Watch mtime; reload `[gate]`, `[realizer]`, `[policy_learner]`,
   `[scene_tagger]`, `[experiment]`, and `[privacy]` blocks.

5. **Wire up few-shot exemplars in the realizer.** Once retro_labels.jsonl
   has 20+ rows, inject the top 5-8 most-confident "would_help" examples
   into the realizer system prompt as exemplars. Free personalization, no
   training.

### Tier 3 — when there's data

7. **Learned gate (rule_v1.py or learned_v0.py).** Once you have ~500+
   labeled candidates, featurize them and fit a calibrated classifier.
   Replace rule_v0's intent map with the classifier's expected_utility.

8. **Long-term memory integration.** When Fisherman exposes `/mind/*`
   routes (or the user maintains rolling summary somewhere accessible),
   add a tool to the realizer for "search past days." This unlocks the
   "you researched X 4d ago, conclusion was Y" intent.

### Tier 4 — polish / nice-to-haves

9. **Capsule inspector mode.** The capsule already hover-expands and carries
   Pipeline/Diet/Settings. The next useful UI pass is richer inspection of the
   last decisions: context, reason codes, policy confidence, and outcome.

10. **Decision drill-down.** Cmd-click a Pipeline row or recent example to see
    the exact candidate, decision, delivery, outcome, and label rows.

11. **Config hot reload.** The remaining operational papercut is restart
    after Save. Launchd handles process restart, but the daemon still reads
    TOML only at boot.

---

## Architectural decisions you should know about

1. **One-way dependency.** The `harness/` package never imports anything
   from `fisherman/`. All Fisherman access goes through HTTP. This is
   deliberate — keeps the harness portable and Fisherman's API surface
   visible.

2. **Hermes is just a `base_url`.** The realizer talks OpenAI chat-
   completions. The user's hermes-agent is at `http://3.82.134.133:8642`
   but any OpenAI-compatible endpoint works. The API key is not shipped
   in repo defaults; set it in the floating capsule Settings tab or via
   `HARNESS_REALIZER_KEY`.

3. **VLM is available + smart-triggered.** The default config keeps the
   per-candidate scene VLM disabled until configured. When enabled, cost is
   bounded because the call is skipped when neither the app nor OCR has changed
   since the last call and the minimum call gap has not elapsed.

4. **DynamicNotchKit is vendored** at `../menubar/Packages/DynamicNotchKit/`
   — same library FishermanMenu uses. The harness path-depends on it.

5. **Goal-driven model is the only model.** The 4-intent catalog is dead.
   Rule_v0 still has rules but they emit reason_codes (not intents). The
   realizer reads reason_codes + daily_goal + screenshot, writes the
   message. If you want the old intents back, they're in
   `prompts/realizer/_archive/`.

6. **Reward is signal-derived (v2).** No more hand-tuned weights. See
   `harness/reward.py` for the table. The v1 weighted-sum is still in
   `score.py` for backwards compat with old reports.

---

## Honest open questions

These don't have answers yet — the next agent (or the user) should resolve them:

1. **Is the goal-aware realizer actually producing useful messages?**
   We've only seen a few test pills. Real-world quality requires
   dogfooding.

2. **Are the intent_signal tiers calibrated correctly?**
   `considered=+0.5, approached=-0.2` are reasonable defaults but
   not validated. Hover targets are now dwell-based, but once you have ≥50
   outcomes, check if the reward correlates with the user's after-the-fact
   "was this useful?"

3. **Does the policy fire often enough?**
   Use current Pipeline/Eval data instead of old fixed counts. If ignored
   timeouts and dismisses dominate, conserve attention. If labels show many
   `would_help` no-ping moments, increase exploration or lower the confidence
   threshold.

4. **Should the notch app and the daemon be split as separate launchd jobs?**
   The daemon now respawns the notch subprocess, which is good enough for
   dogfood. A separate notch job could still be cleaner if the UI grows.

---

## Quick smoke test (5 min)

```bash
cd ~/Desktop/suapp/fisherman/harness
.venv/bin/python -m pytest tests/test_smoke.py        # should pass
.venv/bin/harness install                              # creates ~/.harness/
.venv/bin/harness start --foreground &                 # in another shell
sleep 5
curl -s http://localhost:7893/status                   # daemon alive
.venv/bin/harness test --intent focus_nudge --push     # see the pill
.venv/bin/harness stop                                 # clean shutdown
```

If all four work, you have a healthy system to iterate on.

— end of hand-off —
