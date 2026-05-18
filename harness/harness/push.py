from __future__ import annotations

import asyncio
import shutil
import time
from typing import Optional

from .schemas import Delivery, ProactiveDecision, Realization
from .store import append_jsonl, write_pending


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


async def dispatch(
    decision: ProactiveDecision,
    realization: Realization,
    config: dict,
) -> Delivery:
    """Write the message to ~/.harness/pending/<id>.json and (optionally) trigger a channel.

    notch_pill (default): just write pending. The Swift app polls /pending and
                          renders the pill itself.
    terminal_notifier:    write pending AND fire a banner via terminal-notifier
                          (kept as a fallback when the notch app isn't running).
    """
    channel = config.get("channel", "notch_pill")
    payload = {
        "decision_id": decision.decision_id,
        "candidate_id": decision.candidate_id,
        "intent": decision.intent,
        "message": realization.message,
        "ts": _now_iso(),
        "expires_at_unix": time.time() + float(config.get("auto_dismiss_sec", 8)),
    }
    write_pending(decision.decision_id, payload)

    if channel == "terminal_notifier":
        await _push_terminal_notifier(decision, realization, config)
    # notch_pill: no work here — the Swift app polls /pending

    return Delivery(pushed=True, channel=channel, displayed_at=_now_iso())


async def _push_terminal_notifier(
    decision: ProactiveDecision,
    realization: Realization,
    config: dict,
) -> None:
    binary = shutil.which("terminal-notifier")
    if not binary:
        append_jsonl(
            "outcomes.jsonl",
            {
                "decision_id": decision.decision_id,
                "user_action": "skipped",
                "ts": _now_iso(),
                "note": "terminal-notifier not installed",
            },
        )
        return

    title = f"Fisherman · {decision.intent}"
    message = realization.message or "(empty realizer output)"
    callback_url = (
        f"http://localhost:{config.get('harness_port', 7893)}/outcome"
        f"?id={decision.decision_id}&user_action=clicked"
    )
    cmd = [
        binary,
        "-title", title,
        "-message", message,
        "-sound", "default",
        "-open", callback_url,
        "-group", "fisherman-harness",
        "-sender", "com.apple.Terminal",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
    except Exception as e:
        append_jsonl(
            "outcomes.jsonl",
            {
                "decision_id": decision.decision_id,
                "user_action": "skipped",
                "ts": _now_iso(),
                "note": f"push_failed: {e}",
            },
        )
