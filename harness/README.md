# Fisherman Harness

Proactive presence harness for macOS. Decides *when* to ping the user and uses an LLM (hermes-agent or any OpenAI-compatible endpoint) to compose the message. Reads screen context from [Fisherman](../) over HTTP. Pings the user via a notch pill matching FishermanMenu's aesthetic (uses the same `DynamicNotchKit` library).

For a complete picture, read [HANDOFF.md](HANDOFF.md) — it's the canonical doc for the system's current state. For a frontier-lab-style architecture audit and gap analysis, open [AUDIT.html](AUDIT.html).

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
# OR open Settings → Model after first start

# Run
.venv/bin/harness start --foreground
# Optional: install as a restartable LaunchAgent
.venv/bin/harness install-launchd

# Click the 🐟 in the menu bar → Open Settings → Today
# Write what you're trying to do today. Set sensitivity.
# Pings will appear in the notch.

# Force a test ping (skips the gate, calls the LLM directly)
.venv/bin/harness test --intent focus_nudge --push

# Label retro decisions to seed personalization data
.venv/bin/harness label                # opens browser to :7893/label
```

## Architecture in one paragraph

A Python daemon polls Fisherman's HTTP every 5s, builds a CandidateEvent from screen metadata + OCR, optionally enriches it with a per-candidate VLM scene tag (Gemma-3-4b-it via OpenRouter, smart-triggered for ~$1/mo), runs a rule-based gate that returns `{action, reason_codes, why_now}`, then applies deterministic experiment assignment. Each tick is also folded into an episode stream and a predict-first next-step record; once the horizon elapses, the harness compares predicted behavior against later screen observations/outcomes and writes a `prediction_errors.jsonl` residual. Low-rate holdouts are logged for counterfactual measurement; exploration pings are available but default to 0. If ping is warranted, the realizer calls an OpenAI-compatible LLM with the current screenshot + a `goal_aware_v1` prompt that incorporates the user's daily intention. A model endpoint allowlist blocks untrusted hosts before any prompt or image leaves the machine. Local OCR privacy preflight redacts secret-like text and, when the frame looks sensitive, reruns local Apple Vision OCR on the JPEG to mask key/token text boxes before any screenshot model call; if masking fails, the image is suppressed. Model calls are logged to a privacy-safe audit ledger with endpoint/model/status/image metadata but no raw prompts or screenshots. A critic vets the message, then a Swift notch app claims the pending payload via HTTP polling and renders a pill; that claim is logged separately from the original ping decision so eval can distinguish queued, claimed, and missing-outcome cases. User reactions (click / hover / approach / dismiss / timeout) feed back as signal-derived rewards and confidence-weighted implicit weak labels. Runtime events are still written to JSONL for debuggability/export and mirrored into `~/.harness/harness.db`; dashboard, metrics, replay, score, shadow comparison, next-step eval, and the eval report prefer indexed read paths where available.

## Configuration

`~/.harness/config.toml`. Edit directly or via Settings UI:

```toml
[daemon]
poll_interval_sec = 5
http_port = 7893
fisherman_url = "http://localhost:7892"

[gate]
active_policy = "rule_v0"
cooldown_min = 5
negative_feedback_backoff_min = 15

[experiment]
enabled = true
holdout_rate = 0.02                      # safe counterfactuals
explore_ping_rate = 0.0                   # opt-in random pings

[realizer]
base_url = "http://3.82.134.133:8642"    # OpenAI-compatible
model = "hermes-agent"
api_key = ""                             # set in Settings → Model or HARNESS_REALIZER_KEY
include_vision = true                     # send screenshot
skip_vision_on_sensitive_ocr = true       # do not attach image if OCR looks secret-like
redact_sensitive_screenshots = true       # first try local OCR box masking

[privacy]
block_untrusted_model_hosts = true
allowed_model_hosts = ["3.82.134.133:8642", "openrouter.ai", "localhost", "127.0.0.1", "::1"]

[scene_tagger.llm]                        # per-candidate VLM
enabled = true
base_url = "https://openrouter.ai/api/v1"
model = "google/gemma-3-4b-it"
min_interval_sec = 30
```

## File layout

```
harness/
├── harness/        Python package (daemon, gate, realizer, critic, ...)
├── policies/       Gate policies (currently just rule_v0.py)
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
harness dashboard                           open web dashboard (settings duplicates this)
harness metrics [--since 24h --json]        live outcome + retro-label metrics
harness implicit [--since 7d --json]         weak labels from notification behavior
harness eval-report [--since 7d --json]      joined eval report + failure taxonomy
harness next-steps [--since 7d --json]       predict-first next-step eval
harness info-diet [--since 7d --json]        research episodes + workflow hypotheses
harness storage-backfill [--reset]          mirror JSONL history into harness.db
harness collect --since 24h                 freeze candidates to datasets/dogfood/
harness shadow --since 24h [--full]         compare policy variants against labels
harness replay --policy rule_v0 --since 7d  shadow policy on frozen data
harness score --predictions reports/...     replay scoring + reward_v2
```

## Eval hardening

`harness eval-report --since 7d` builds an OpenAdapt-style intervention report for the harness. It joins decisions, outcomes, explicit labels, implicit weak labels, compact traces, predict-first next-step metrics, and policy-variant calibration into one sanitized JSON object. The same report is available at `GET /eval/report` and in the dashboard's Eval tab.

The report includes data coverage, claimed-ping outcome capture, explicit/implicit label readiness, policy variant scores, failure taxonomy (`false_interruption`, `missed_help`, `queued_not_claimed`, `undelivered_ping`, `soft_rejection`, `missing_outcome_signal`, etc.), next-step prediction accuracy/residuals, and recent non-green examples without raw OCR or screenshots. `harness next-steps --since 7d` shows the prediction loop directly.

`harness info-diet --since 7d` builds a conservative research/workflow report from the same candidate stream. It groups browser-like reading episodes, inferred domains, query-like phrases, dwell patterns, and tentative workflow hypotheses. Treat it as an evidence panel, not a trusted skill compiler yet: current source attribution is OCR-derived until Fisherman exposes browser URL/title ground truth.

## State storage

Runtime state lives outside the repo at `~/.harness/`. See `HANDOFF.md` for the full layout.

The canonical append path still writes JSONL files such as `candidates.jsonl`, `decisions.jsonl`, `deliveries.jsonl`, `traces.jsonl`, `outcomes.jsonl`, `retro_labels.jsonl`, `model_calls.jsonl`, `episodes.jsonl`, `next_step_predictions.jsonl`, and `prediction_errors.jsonl`. Each append is also mirrored into `~/.harness/harness.db` event history, with typed tables for the core candidate/decision/trace/outcome/model/label streams. Late outcome attachment updates both `traces.jsonl` and the typed SQLite trace row. Dashboard, metrics, replay, score, and shadow comparison read from SQLite when the sidecar is present and fall back to JSONL for old installs. Use `harness storage-backfill --reset` to mirror existing JSONL history into a fresh sidecar.

## Tests

```bash
.venv/bin/python -m pytest tests/test_smoke.py
```

The smoke suite covers schemas, config default merging, store/SQLite mirroring, privacy/trust checks, scene tagger, VLM overlay, gate, sleep/resume continuity, experiments, launchd plist generation, labeler queueing, metrics, next-step eval, information-diet reporting, shadow eval, critic, and reward.
