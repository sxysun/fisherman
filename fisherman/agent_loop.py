"""Tiny status-loop companion: publish the backend activity row to friends.

This is a SEPARATE concern from fisherman the protocol. The daemon ships
no LLM client. This file is the optional "make my friends see live status"
glue, intended to run as its own process.

Loop: every --interval seconds, read the same sanitized backend
/api/current_activity and /api/activity_history payloads that drive the
local activity row, then publish them per recipient. There is no separate
per-friend LLM pass here; status generation happens upstream in the active
context home.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from typing import Any

import click

from fisherman.config import FishermanConfig
from fisherman.friends import list_friends
from fisherman.timeline import _activity_api_url, _fishkey_header


_DEFAULT_INTERVAL = 300  # 5 minutes

_CATEGORY_EMOJI = {
    "coding": "💻",
    "debugging": "🔎",
    "code review": "🧾",
    "reading docs": "📚",
    "design": "🎨",
    "writing": "✍️",
    "chat": "💬",
    "email": "✉️",
    "meeting": "📅",
    "browsing": "🌐",
    "news": "📰",
    "reading": "🧠",
    "gaming": "🎲",
    "terminal": "⌨️",
    "idle": "😴",
}

_EMOJI_SHORTCODES = {
    ":crossed_swords:": "⚔️",
    ":game_die:": "🎲",
    ":video_game:": "🎮",
    ":computer:": "💻",
    ":laptop:": "💻",
    ":mag:": "🔎",
    ":memo:": "🧾",
    ":books:": "📚",
    ":art:": "🎨",
    ":speech_balloon:": "💬",
    ":email:": "✉️",
    ":calendar:": "📅",
    ":globe_with_meridians:": "🌐",
    ":newspaper:": "📰",
    ":brain:": "🧠",
    ":keyboard:": "⌨️",
    ":zzz:": "😴",
}


def _sanitize_emoji(value: Any, category: str | None = None) -> str:
    """Keep friend-visible emoji displayable even when an LLM returns shortcode text."""
    raw = str(value or "").strip()
    category_key = (category or "").strip().lower()
    fallback = _CATEGORY_EMOJI.get(category_key, "💻")
    if not raw:
        return fallback

    lowered = raw.lower()
    if lowered in _EMOJI_SHORTCODES:
        return _EMOJI_SHORTCODES[lowered]

    # Common model failure: returns ":crossed_swords:" or truncated
    # shortcode-looking ASCII instead of an actual emoji.
    if raw.startswith(":") or raw.isascii():
        return fallback

    return raw[:8]


def _sanitize_digest(digest: dict, *, flow: bool | None = None) -> dict:
    category = str(digest.get("category") or "idle").strip()[:20] or "idle"
    status = str(digest.get("status") or "").strip()[:30]
    out = {
        "emoji": _sanitize_emoji(digest.get("emoji"), category),
        "category": category,
        "status": status,
        "flow": bool(digest.get("flow", False) if flow is None else flow),
    }
    return out


def _activity_history_entries(limit: int = 8) -> list[dict]:
    cfg = FishermanConfig()
    if not cfg.private_key:
        return []
    url = _activity_api_url(cfg, f"/api/activity_history?limit={limit}")
    auth = _fishkey_header(cfg.private_key)
    if not url or not auth:
        return []
    req = urllib.request.Request(url, headers={"Authorization": auth})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
    except Exception:
        return []
    entries = []
    for entry in data.get("entries") or []:
        category = str(entry.get("category") or "idle").strip()[:20] or "idle"
        status = str(entry.get("status") or "").strip()[:30]
        timestamp = entry.get("timestamp") or entry.get("ts")
        if not timestamp:
            continue
        entries.append({
            "emoji": _sanitize_emoji(entry.get("emoji"), category),
            "category": category,
            "status": status,
            "timestamp": timestamp,
        })
    return entries


def _current_activity() -> dict | None:
    cfg = FishermanConfig()
    if not cfg.private_key:
        return None
    url = _activity_api_url(cfg, "/api/current_activity")
    auth = _fishkey_header(cfg.private_key)
    if not url or not auth:
        return None
    req = urllib.request.Request(url, headers={"Authorization": auth})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
    except Exception:
        return None
    if data.get("activity") is None and not data.get("category"):
        return None
    return data


def _digest_from_activity(activity: dict, history: list[dict]) -> dict:
    """Build the friend-visible payload from the backend activity API.

    The backend endpoints already expose only derived activity fields. This
    clamp prevents malformed display values from turning into broken UI.
    """
    digest = _sanitize_digest(activity, flow=bool(activity.get("flow", False)))
    if history:
        digest["history"] = history
        digest["view"] = "activity_history"
    return digest


def _close_digest_from_activity(activity: dict | None, history: list[dict], fallback: dict) -> dict:
    """Backward-compatible wrapper for older tests/imports."""
    if not activity:
        digest = dict(fallback)
    else:
        digest = _digest_from_activity(activity, history)
    return digest


def _publish(digest: dict, recipients: list[str]) -> bool:
    cmd = [sys.executable, "-m", "fisherman", "publish-status", "--from-stdin"]
    for recipient in recipients:
        cmd += ["--to", recipient]
    res = subprocess.run(
        cmd, input=json.dumps(digest), capture_output=True, text=True, timeout=15
    )
    if res.returncode != 0:
        click.echo(f"  publish failed: {res.stderr.strip()}", err=True)
        return False
    return True


def _policy_key(friend: dict) -> tuple[str, str]:
    audience = (friend.get("audience") or "friends").strip().lower()
    prompt = (friend.get("policy_prompt") or "").strip()
    return audience, prompt


def run_once(api_key: str | None, base_url: str, model: str, since: str = "5m") -> bool:
    del api_key, base_url, model, since
    current_activity = _current_activity()
    if not current_activity:
        click.echo("  current activity unavailable — skipping")
        return False
    friends = list_friends()
    if not friends:
        click.echo("  no friends — skipping status publish")
        return False
    activity_history = _activity_history_entries(limit=8)
    digest = _digest_from_activity(current_activity, activity_history)

    by_policy: dict[tuple[str, str], list[dict]] = {}
    for friend in friends:
        by_policy.setdefault(_policy_key(friend), []).append(friend)

    ok_any = False
    for (audience, custom_prompt), group in by_policy.items():
        recipients = [friend["pubkey_hex"] for friend in group]
        ok = _publish(digest, recipients)
        ok_any = ok_any or ok
        if ok:
            names = ", ".join(friend["name"] for friend in group[:3])
            if len(group) > 3:
                names += f", +{len(group) - 3}"
            click.echo(
                f"  published {audience}: {digest['emoji']} "
                f"{digest['category']:<12} {digest['status']} -> {names}"
            )
    return ok_any


@click.command()
@click.option("--interval", default=_DEFAULT_INTERVAL, show_default=True,
              help="Seconds between status updates")
@click.option("--since", default="5m", show_default=True,
              help="Accepted for compatibility; status comes from the active backend")
@click.option("--model", default=None, help="Deprecated; status comes from the active backend")
@click.option("--once", is_flag=True, help="Run a single iteration and exit")
def main(interval, since, model, once):
    """Run the activity-status publishing loop."""
    click.echo(f"agent loop: backend activity every {interval}s")
    if once:
        run_once(None, "", model or "", since=since)
        return
    while True:
        try:
            ts = time.strftime("%H:%M:%S")
            click.echo(f"[{ts}] cycle")
            run_once(None, "", model or "", since=since)
        except KeyboardInterrupt:
            return
        except Exception as e:
            click.echo(f"  cycle failed: {e}", err=True)
        time.sleep(interval)


if __name__ == "__main__":
    main()
