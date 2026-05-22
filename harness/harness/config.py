from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any


CONFIG_PATH = Path(os.path.expanduser("~/.harness/config.toml"))


DEFAULT_CONFIG_TOML = """\
[daemon]
# Matches Fisherman's default capture_interval (5s on AC, 15s on battery).
# Most ticks are no_ping so the cost is negligible.
poll_interval_sec = 5
http_port = 7893
fisherman_url = "http://localhost:7892"

[gate]
active_policy = "llm_icl_v0"
cooldown_min = 5
negative_feedback_backoff_min = 15
resume_suppression_sec = 90
quiet_hours_start = 22
quiet_hours_end = 8
frequency = "medium"

[experiment]
# Deterministic live assignment for counterfactual measurement. Holdout is
# safe: a small fraction of would-ping decisions are intentionally silent and
# logged with counterfactual_action="notch_ping". Exploration pings stay low,
# but nonzero, so the harness can learn when it is being too timid.
enabled = true
salt = "local_v1"
holdout_rate = 0.02
explore_ping_rate = 0.03
respect_hard_gates = true
explore_eligible_reasons = ["no_clear_help"]

[trainer]
# Safe daily trainer: proposes a canary policy from implicit outcomes +
# explicit retro labels. It never auto-activates; activate from settings after
# reviewing the report.
enabled = true
window = "30d"
interval_hours = 24
initial_delay_sec = 60
min_implicit_usable = 20
min_explicit_labels = 0

[policy_learner]
# LLM in-context policy. rule_v0 still runs first for hard safety gates and
# fallback; the model only chooses ping/not-ping after those guardrails pass.
enabled = true
base_url = "http://3.82.134.133:8642"
model = "hermes-agent"
api_key = ""
api_key_env = "HARNESS_REALIZER_KEY"
timeout_sec = 8
max_tokens = 220
temperature = 0.0
max_examples = 16
kg_window = "30d"
min_interval_sec = 15
min_confidence_to_ping = 0.55

[scene]
llm_fallback_enabled = false

# Per-candidate VLM scene tagger. Sends the current frame to a cheap
# multimodal endpoint (default: mistralai/mistral-nemo on OpenRouter) to
# enrich the scene tag with what's actually visible. Smart-triggered:
# only runs when the app or OCR has changed since the last VLM call, and
# at most once per min_interval_sec.
[scene_tagger.llm]
enabled = false
base_url = "https://openrouter.ai/api/v1"
# Must be a vision-capable model. Cheap options as of 2026-05:
#   google/gemma-3-4b-it                ~$0.04/M  (default — works well, fast)
#   google/gemma-3-12b-it               ~$0.04/M in, $0.13/M out
#   amazon/nova-lite-v1                 ~$0.06/M
#   nvidia/nemotron-nano-12b-v2-vl:free FREE (rate-limited)
#   openai/gpt-5-nano                   ~$0.05/M in, $0.40/M out
model = "google/gemma-3-4b-it"
api_key = ""
api_key_env = "OPENROUTER_API_KEY"
min_interval_sec = 30
error_backoff_sec = 120
rate_limit_backoff_sec = 300
timeout_sec = 12

[memory]
session_window_min = 120
# Break continuity after a likely laptop sleep, lock-screen pause, or capture
# outage. This keeps "minutes on current app" tied to observed active frames
# instead of wall-clock time.
idle_boundary_sec = 90
active_frame_max_age_sec = 60

[workflow_events]
# Deterministic local eventization inspired by LifeTrace-style data collection:
# group adjacent candidates into app/window workflow runs so policy sees the
# trajectory that produced the current screen, not only one frame.
enabled = true
max_gap_sec = 90
recent_context_sec = 300
max_recent_context = 6
max_ocr_preview_chars = 500

# Realizer points at the hermes-agent endpoint. Hermes handles its own
# agentic loop server-side, so client-side tool calls are disabled by default.
# To swap providers, change base_url + model + api_key.
[realizer]
base_url = "http://3.82.134.133:8642"
model = "hermes-agent"
api_key = ""
api_key_env = "HARNESS_REALIZER_KEY"
max_tool_calls = 1
max_tokens = 80
timeout_sec = 45            # longer to allow vision processing
temperature = 0.3
# Send the actual screen JPEG to hermes as a multimodal content block.
# Robust to Fisherman's sometimes-stale frontmost_app metadata.
include_vision = true
# Local privacy preflight: if OCR looks like a key/token/password, redact OCR
# in text prompts and do not attach the screenshot to external model calls.
skip_vision_on_sensitive_ocr = true
# If a sensitive frame would otherwise be suppressed, try local Apple Vision OCR
# on the JPEG and mask matching text boxes before attaching the image. Failure
# still falls back to skip_vision_on_sensitive_ocr.
redact_sensitive_screenshots = true

[realizer.tools]
query_fisherman_history = false
get_recent_screen_ocr = false

[privacy]
# Endpoint allowlist for any model call that could receive OCR, prompt state,
# or a screenshot. Redaction protects sensitive content inside an allowed call;
# this check blocks accidental model traffic to unknown hosts.
block_untrusted_model_hosts = true
allow_local_model_hosts = true
allowed_model_hosts = [
  "3.82.134.133:8642",
  "openrouter.ai",
  "localhost",
  "127.0.0.1",
  "::1",
]

[critic]
# Critic is a single-shot LLM pass. Reuses the realizer endpoint by default.
enabled = false
base_url = "http://3.82.134.133:8642"
model = "hermes-agent"
api_key = ""

[push]
channel = "notch_pill"
auto_dismiss_sec = 8

[reward]
version = "v2"

[reward.weights]
welcomed = 3.0
helpful = 2.0
annoying = -5.0
privacy = -8.0
duplicate = -1.0

[intents]
enabled = [
  "focus_nudge",
  "offer_research",
  "surface_open_thread",
  "summarize_session",
]

[debug]
dry_run = false
"""


def load() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"config not found at {CONFIG_PATH}. Run `harness install` first."
        )
    defaults = tomllib.loads(DEFAULT_CONFIG_TOML)
    with open(CONFIG_PATH, "rb") as f:
        user_config = tomllib.load(f)
    return _deep_merge(defaults, user_config)


def write_default(force: bool = False) -> Path:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists() and not force:
        return CONFIG_PATH
    CONFIG_PATH.write_text(DEFAULT_CONFIG_TOML)
    return CONFIG_PATH


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out
