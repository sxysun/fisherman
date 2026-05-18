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

# Click the 🐟 in the menu bar → Open Settings → Today
# Write what you're trying to do today. Set sensitivity.
# Pings will appear in the notch.

# Force a test ping (skips the gate, calls the LLM directly)
.venv/bin/harness test --intent focus_nudge --push

# Label retro decisions to seed personalization data
.venv/bin/harness label                # opens browser to :7893/label
```

## Architecture in one paragraph

A Python daemon polls Fisherman's HTTP every 5s, builds a CandidateEvent from screen metadata + OCR, optionally enriches it with a per-candidate VLM scene tag (Gemma-3-4b-it via OpenRouter, smart-triggered for ~$1/mo), runs a rule-based gate that returns `{action, reason_codes, why_now}`, and if ping is warranted, calls an OpenAI-compatible LLM with the current screenshot + a `goal_aware_v1` prompt that incorporates the user's daily intention. A local OCR privacy preflight redacts secret-like text and, when the frame looks sensitive, reruns local Apple Vision OCR on the JPEG to mask key/token text boxes before any screenshot model call; if masking fails, the image is suppressed. A critic vets the message, then a Swift notch app picks it up via HTTP polling and renders a pill. User reactions (click / hover / approach / dismiss / timeout) feed back as signal-derived rewards in `~/.harness/traces.jsonl`.

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

[realizer]
base_url = "http://3.82.134.133:8642"    # OpenAI-compatible
model = "hermes-agent"
api_key = ""                             # set in Settings → Model or HARNESS_REALIZER_KEY
include_vision = true                     # send screenshot
skip_vision_on_sensitive_ocr = true       # do not attach image if OCR looks secret-like
redact_sensitive_screenshots = true       # first try local OCR box masking

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
├── tests/          smoke tests (21/21 passing)
└── HANDOFF.md      read this for the full picture
```

## CLI

```
harness install [--force] [--build-notch]   create ~/.harness/, build notch
harness build-notch                         rebuild notch only
harness start [--foreground]                start the daemon
harness stop                                stop daemon + notch
harness status                              one-line state
harness inspect [--since 1h --action no_ping --intent X]
harness test --intent X [--push --message TEXT --app APP]
harness snooze 30m | harness unsnooze
harness mute INTENT | harness unmute INTENT [--all]
harness label                               open retro labeler in browser
harness dashboard                           open web dashboard (settings duplicates this)
harness collect --since 24h                 freeze candidates to datasets/dogfood/
harness replay --policy rule_v0 --since 7d  shadow policy on frozen data
harness score --predictions reports/...     metrics + reward_v2
```

## State storage

Runtime state lives outside the repo at `~/.harness/`. See `HANDOFF.md` for the full layout.

## Tests

```bash
.venv/bin/python -m pytest tests/test_smoke.py
```

12 tests; covers schemas, store, scene tagger, VLM overlay, gate, critic, reward.
