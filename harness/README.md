# Fisherman Harness

Proactive presence harness for macOS. Decides *when* to ping the user and uses an LLM (hermes-agent or any OpenAI-compatible endpoint) to compose the message. Reads screen context from [Fisherman](../) over HTTP. Pings the user via a separate floating Harness capsule that can join all macOS Spaces while Fisherman keeps its own notch surface.

For a complete picture, read [HANDOFF.md](HANDOFF.md) — it's the canonical doc for the system's current state. For a frontier-lab-style architecture audit and gap analysis, open [AUDIT.html](AUDIT.html). For the ProAgentBench paper synthesis and rigor roadmap, read [PROAGENTBENCH_RIGOR_PLAN.md](PROAGENTBENCH_RIGOR_PLAN.md).

## Quick start

```bash
cd harness
uv sync
uv pip install -e .

# Build the Swift notch app
cd notch && ./build.sh && cd ..

# Configure
.venv/bin/harness install        # writes ~/.harness/config.toml + builds notch
# edit ~/.harness/config.toml to set [realizer] api_key + base_url
# OR use the floating capsule Settings tab after first start

# Run
.venv/bin/harness start --foreground
# Optional: install as a restartable LaunchAgent
.venv/bin/harness install-launchd

# Hover the Harness floating capsule → Settings.
# Write what you're trying to do today, set sensitivity, and save.
# Pings will appear in the floating capsule.

# Force a test ping (skips the gate, calls the LLM directly)
.venv/bin/harness test --intent focus_nudge --push

# Label retro decisions to seed personalization data
.venv/bin/harness label                # opens browser to :7893/label
```

## Architecture in one paragraph

A Python daemon polls Fisherman's HTTP every 5s, builds a CandidateEvent from screen metadata + OCR, optionally enriches it with a per-candidate VLM scene tag (Gemma-3-4b-it via OpenRouter when enabled), and groups adjacent candidates into local workflow runs keyed by active app/window plus sleep/capture-gap boundaries. It then runs a binary gate that returns `{action, reason_codes, why_now}` and applies deterministic experiment assignment. The default live policy is `llm_icl_v0`: it runs `rule_v0` first for hard gates/fallback, then asks an OpenAI-compatible LLM to choose `notch_ping` or `no_ping` from the current context, recent workflow trajectory, local KG-style priors, and recent explicit/implicit examples. Low-rate holdouts are logged for counterfactual measurement; exploration pings default to 3% on eligible ambiguous moments so the harness can learn when it is too timid without becoming noisy. Every decision immediately gets a trace row with a lifecycle stage before realization starts, then that trace is patched through realizer, critic, dispatch, claim, and outcome stages. If ping is warranted, the realizer calls an OpenAI-compatible LLM with the current screenshot + a `goal_aware_v1` prompt that incorporates the user's daily intention. A model endpoint allowlist blocks untrusted hosts before any prompt or image leaves the machine. Local OCR privacy preflight redacts secret-like text and, when the frame looks sensitive, reruns local Apple Vision OCR on the JPEG to mask key/token text boxes before any screenshot model call; if masking fails, the image is suppressed. Model calls are logged to a privacy-safe audit ledger with endpoint/model/status/image metadata but no raw prompts or screenshots. A critic vets the message, then a Swift floating capsule claims the pending payload via HTTP polling and renders it; that claim is logged separately from the original ping decision so eval can distinguish queued, claimed, and missing-outcome cases. User reactions (click / hover / approach / dismiss / timeout) feed back as signal-derived rewards and confidence-weighted implicit weak labels. Runtime events are still written to JSONL for debuggability/export and mirrored into `~/.harness/harness.db`; dashboard, metrics, replay, score, shadow comparison, dataset mining, and the eval report prefer indexed read paths where available.

## Configuration

`~/.harness/config.toml`. Edit directly or via Settings UI:

```toml
[daemon]
poll_interval_sec = 5
http_port = 7893
fisherman_url = "http://localhost:7892"

[gate]
active_policy = "llm_icl_v0"
cooldown_min = 5
negative_feedback_backoff_min = 15

[experiment]
enabled = true
holdout_rate = 0.02                      # safe counterfactuals
explore_ping_rate = 0.03                  # low-rate exploration on eligible ambiguous moments

[policy_learner]                         # used when active_policy="llm_icl_v0"
enabled = true
base_url = "http://3.82.134.133:8642"
model = "hermes-agent"
max_examples = 16
kg_window = "30d"                         # local app/scene/keyword priors
min_confidence_to_ping = 0.55

[realizer]
base_url = "http://3.82.134.133:8642"    # OpenAI-compatible
model = "hermes-agent"
api_key = ""                             # set in floating capsule Settings or HARNESS_REALIZER_KEY
include_vision = true                     # send screenshot
skip_vision_on_sensitive_ocr = true       # do not attach image if OCR looks secret-like
redact_sensitive_screenshots = true       # first try local OCR box masking

[privacy]
block_untrusted_model_hosts = true
allowed_model_hosts = ["3.82.134.133:8642", "openrouter.ai", "localhost", "127.0.0.1", "::1"]

[scene_tagger.llm]                        # per-candidate VLM
enabled = false                           # enable from capsule Settings when OpenRouter is configured
base_url = "https://openrouter.ai/api/v1"
model = "google/gemma-3-4b-it"
min_interval_sec = 30
error_backoff_sec = 120
rate_limit_backoff_sec = 300

[workflow_events]
enabled = true
max_gap_sec = 90                           # closes runs across sleep/capture gaps
recent_context_sec = 300                   # policy sees the last 5 minutes of runs
max_recent_context = 6
max_ocr_preview_chars = 500
```

The Settings tab also exposes learner controls. `Examples` is `[policy_learner].max_examples`: the maximum number of explicit/implicit few-shot examples sent to the LLM ping/not-ping learner. It is a cap, not the current label count. `Label coverage` in Pipeline/Eval is explicit retro labels divided by decisions in the selected window, so `0.0%` means no human labels in that window even if implicit hover/dismiss/timeout signal exists.

## File layout

```
harness/
├── harness/        Python package (daemon, gate, realizer, critic, ...)
├── policies/       Gate policies (rule_v0.py, llm_icl_v0.py)
├── prompts/        Realizer + critic prompts
├── notch/          Swift app (HarnessNotch.app)
├── eval/           replay.py, score.py (offline policy analysis)
├── tests/          smoke tests
└── HANDOFF.md      read this for the full picture
```

## CLI

```
harness install [--force] [--build-notch]   create ~/.harness/, build notch
harness build-notch                         rebuild notch only
harness start [--foreground]                start the daemon
harness install-launchd [--load]            install/restart LaunchAgent
harness launchd-status                      show LaunchAgent status
harness uninstall-launchd                   unload/remove LaunchAgent
harness stop                                stop daemon + notch
harness status                              one-line state
harness inspect [--since 1h --action no_ping --intent X]
harness test --intent X [--push --message TEXT --app APP]
harness snooze 30m | harness unsnooze
harness mute INTENT | harness unmute INTENT [--all]
harness label                               open retro labeler in browser
harness event-examples [--since 30d]        mined workflow-event review rows
harness dashboard                           open web dashboard (settings duplicates this)
harness metrics [--since 24h --json]        live outcome + retro-label metrics
harness implicit [--since 7d --json]         weak labels from notification behavior
harness eval-report [--since 7d --json]      joined eval report + failure taxonomy
harness info-diet [--since 7d --json]        research episodes + workflow hypotheses
harness kg-priors [--since 30d --json]       local app/scene/keyword priors
harness hard-examples [--since 30d --json]   balanced candidate positives, hard negatives, misses
harness freeze-eval [--since 30d]            write frozen candidate + event eval dataset
harness curate TYPE ID --action exclude      curation ledger for eval/training rows
harness train-policy [--since 30d]           propose a canary policy from labels/outcomes
harness activate-canary | harness rollback-canary
harness storage-backfill [--reset]          mirror JSONL history into harness.db
harness collect --since 24h                 freeze candidates to datasets/dogfood/
harness shadow --since 24h [--full]         compare policy variants against labels
harness replay --policy rule_v0 --since 7d  shadow policy on frozen data
harness score --predictions reports/...     replay scoring + reward_v2
```

## Eval hardening

`harness eval-report --since 7d` builds an OpenAdapt-style intervention report for the harness. It joins decisions, outcomes, explicit labels, implicit weak labels, compact traces, delivery claims, and policy-variant calibration into one sanitized JSON object. The same report is available at `GET /eval/report` and in the dashboard's Eval tab.

The report includes trace completeness, data coverage, claimed-ping outcome capture, explicit precision/recall/F1 where labels exist, implicit label readiness, policy variant scores, failure taxonomy (`false_interruption`, `missed_help`, `trace_gap_before_delivery`, `incomplete_trace_before_delivery`, `queued_not_claimed`, `undelivered_ping`, `soft_rejection`, `missing_outcome_signal`, etc.), and recent non-green examples without raw OCR or screenshots.

`workflow_events.jsonl` is the local LifeTrace-style event layer. It groups candidate ticks into closed workflow runs whenever the active app/window changes, the frame goes stale, the screen becomes sensitive/inactive, or a capture gap suggests laptop sleep/resume. The LLM ICL learner sees the recent compact workflow sequence, so the binary policy can reason about trajectory rather than one isolated frame.

`harness hard-examples --since 30d` mines a cleaner timing dataset from real dogfood data: useful pings, dismissed/ignored pings, context-matched hard negatives, and missed-help candidates where a no-ping context is followed by help-seeking behavior. The sampler is balanced so positives do not crowd out hard negatives. `harness event-examples --since 30d` performs the same review mining at the workflow-event level, where a whole app/window run can be labeled as missed help, good silence, not-now, or false interruption.

The browser labeler now has two scopes: `/label` for exact decision moments and `/label/events` for workflow-event review. Event labels are stored in `retro_labels.jsonl` with `label_scope="workflow_event"` and are reported separately from candidate labels, so event-level recall does not corrupt tick-level precision.

`harness freeze-eval` writes both `examples.jsonl` and `event_examples.jsonl` into a time-ordered train/validation/test manifest under `harness/datasets/`. The manifest includes split timestamp bounds and a no-future-leakage rule: policy replay may use only candidates, outcomes, labels, priors, and workflow events whose timestamps are at or before the example timestamp. The export omits raw screenshots and raw OCR by default and respects `curation.jsonl` exclusions.

`harness info-diet --since 7d` builds a conservative research/workflow report from the same candidate stream. It groups browser-like reading episodes, inferred domains, query-like phrases, dwell patterns, and tentative workflow hypotheses. Treat it as an evidence panel, not a trusted skill compiler yet: current source attribution is OCR-derived until Fisherman exposes browser URL/title ground truth.

## State storage

Runtime state lives outside the repo at `~/.harness/`. See `HANDOFF.md` for the full layout.

The canonical append path still writes JSONL files such as `candidates.jsonl`, `workflow_events.jsonl`, `decisions.jsonl`, `deliveries.jsonl`, `traces.jsonl`, `outcomes.jsonl`, `retro_labels.jsonl`, `curation.jsonl`, and `model_calls.jsonl`. Each append is also mirrored into `~/.harness/harness.db` event history, with typed tables for the core candidate/workflow/decision/trace/delivery/outcome/model/label/curation streams. Trace lifecycle patching and late outcome attachment update both `traces.jsonl` and the typed SQLite trace row. Dashboard, metrics, replay, score, dataset mining, and shadow comparison read from SQLite when the sidecar is present and fall back to JSONL for old installs. Use `harness storage-backfill --reset` to mirror existing JSONL history into a fresh sidecar.

## Tests

```bash
.venv/bin/python -m pytest tests/test_smoke.py
```

The smoke suite covers schemas, config default merging, workflow eventization, trace lifecycle patching, store/SQLite mirroring, privacy/trust checks, scene tagger, VLM overlay/backoff, gate, LLM ICL policy, KG priors, hard-example mining, sleep/resume continuity, experiments, launchd plist generation, labeler queueing, metrics, information-diet reporting, shadow eval, critic, and reward.
