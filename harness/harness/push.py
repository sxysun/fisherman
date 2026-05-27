from __future__ import annotations

import asyncio
import shutil
import time
from typing import Optional

from .schemas import Delivery, ProactiveDecision, Realization
from .store import append_jsonl, patch_trace, write_pending


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _pending_ttl_sec(config: dict) -> float:
    """How long a notch payload may wait for claim/outcome before expiry.

    `auto_dismiss_sec` used to be overloaded as this TTL, but HarnessNotch
    displays pings for at least 30s before timing them out. An 8s pending TTL
    makes valid pings expire before the native UI can report an outcome.
    """
    configured = config.get("pending_ttl_sec")
    if configured is not None:
        try:
            return max(30.0, float(configured))
        except (TypeError, ValueError):
            pass
    try:
        auto_dismiss = float(config.get("auto_dismiss_sec", 30))
    except (TypeError, ValueError):
        auto_dismiss = 30.0
    return max(120.0, auto_dismiss + 45.0)


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
    expires_at_unix = time.time() + _pending_ttl_sec(config)
    if channel == "terminal_notifier":
        payload = {
            "decision_id": decision.decision_id,
            "candidate_id": decision.candidate_id,
            "intent": decision.intent,
            "message": realization.message,
            "channel": channel,
            "claimable_by_notch": False,
            "ts": _now_iso(),
            "expires_at_unix": expires_at_unix,
        }
        await asyncio.to_thread(write_pending, decision.decision_id, payload)
        ok, error = await _push_terminal_notifier(decision, realization, config)
        if ok:
            await asyncio.to_thread(_record_terminal_notifier_display, decision)
        return Delivery(
            pushed=ok,
            channel=channel,
            displayed_at=_now_iso() if ok else None,
            error=error,
        )

    payload = {
        "decision_id": decision.decision_id,
        "candidate_id": decision.candidate_id,
        "intent": decision.intent,
        "message": realization.message,
        "ts": _now_iso(),
        "expires_at_unix": expires_at_unix,
    }
    await asyncio.to_thread(write_pending, decision.decision_id, payload)

    return Delivery(pushed=True, channel=channel, displayed_at=_now_iso())


async def _push_terminal_notifier(
    decision: ProactiveDecision,
    realization: Realization,
    config: dict,
) -> tuple[bool, str | None]:
    binary = shutil.which("terminal-notifier")
    if not binary:
        await asyncio.to_thread(
            append_jsonl,
            "outcomes.jsonl",
            {
                "decision_id": decision.decision_id,
                "user_action": "skipped",
                "ts": _now_iso(),
                "note": "terminal-notifier not installed",
            },
        )
        return False, "terminal-notifier not installed"

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
        if proc.returncode != 0:
            return False, f"terminal-notifier exited {proc.returncode}"
        return True, None
    except Exception as e:
        await asyncio.to_thread(
            append_jsonl,
            "outcomes.jsonl",
            {
                "decision_id": decision.decision_id,
                "user_action": "skipped",
                "ts": _now_iso(),
                "note": f"push_failed: {e}",
            },
        )
        return False, f"push_failed: {e}"


def _record_terminal_notifier_display(decision: ProactiveDecision) -> None:
    """Record display evidence for terminal-notifier's direct callback path."""
    delivery_row = {
        "delivery_id": f"del_{decision.decision_id}_terminal_notifier_displayed",
        "decision_id": decision.decision_id,
        "candidate_id": decision.candidate_id,
        "channel": "terminal_notifier",
        "delivery_action": "displayed_ack",
        "ack_source": "terminal_notifier_dispatch",
        "pending_attempts": 0,
        "pending_created_at": None,
        "pending_claimed_at": None,
        "ts": _now_iso(),
    }
    append_jsonl("deliveries.jsonl", delivery_row)
    patch_trace(
        decision.decision_id,
        {"delivery": {"channel": "terminal_notifier", "pushed": True, "displayed_at": delivery_row["ts"]}},
        lifecycle_stage="displayed_ack",
        lifecycle_extra={"ack_source": "terminal_notifier_dispatch"},
    )
