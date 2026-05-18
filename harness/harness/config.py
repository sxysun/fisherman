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
active_policy = "rule_v0"
cooldown_min = 5
negative_feedback_backoff_min = 15
quiet_hours_start = 22
quiet_hours_end = 8
frequency = "medium"

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
timeout_sec = 12

[memory]
session_window_min = 120

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
    with open(CONFIG_PATH, "rb") as f:
        return tomllib.load(f)


def write_default(force: bool = False) -> Path:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists() and not force:
        return CONFIG_PATH
    CONFIG_PATH.write_text(DEFAULT_CONFIG_TOML)
    return CONFIG_PATH
