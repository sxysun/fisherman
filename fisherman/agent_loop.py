"""Tiny status-loop companion: read context, summarize via LLM, publish.

This is a SEPARATE concern from fisherman the protocol. The daemon ships
no LLM client. This file is the optional "make my friends see live status"
glue, intended to run as its own process.

Default LLM is OpenRouter (or any OpenAI-compatible endpoint) configured
via Fisherman settings or env:

    FISH_STATUS_LLM_API_KEY or OPENAI_API_KEY — optional; missing key uses a generic privacy-safe fallback
    FISH_STATUS_LLM_BASE_URL or OPENAI_BASE_URL
    FISH_STATUS_LLM_MODEL or AGENT_MODEL/OPENAI_MODEL

Loop: every --interval seconds, query recent context, build a prompt,
call the LLM, parse {emoji,category,status,flow}, publish. Uses the
existing CLI under the hood — no new IPC.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from typing import Any

import click

from fisherman.config import FishermanConfig
from fisherman.friends import list_friends
from fisherman.timeline import _activity_api_url, _fishkey_header


_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
_DEFAULT_MODEL = "mistralai/mistral-nemo"
_DEFAULT_INTERVAL = 300  # 5 minutes
_PROMPT = """\
Generate a short ambient status (max 30 chars) describing what this person is doing.

Recent context (newest first):
{context}

Respond with ONLY this JSON:
{{"emoji": "<single emoji>", "category": "<category>", "status": "<status, max 30 chars>", "flow": <true|false>}}

Categories: coding, debugging, code review, reading docs, design, writing, chat, email, meeting, browsing, news, reading, gaming, terminal, idle.

STATUS RULES:
- Be SPECIFIC about the domain/topic.
- Don't just name the app or filename.
- No vague "tinkering" / "exploring" / "in the zone" filler.

PRIVACY — friends see this. Never include people's names, message content, health, finances, legal, or NSFW topics.
"""

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


def _llm_settings(model_override: str | None = None) -> tuple[str | None, str, str, str]:
    """Read the one status-LLM configuration used by Settings and the loop."""
    cfg = FishermanConfig()
    mode = (cfg.status_llm_mode or "managed").strip().lower()
    if mode not in {"managed", "byo", "none"}:
        mode = "managed"
    base_url = (
        os.environ.get("FISH_STATUS_LLM_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or cfg.status_llm_base_url
        or _DEFAULT_BASE_URL
    ).strip()
    model = (
        model_override
        or os.environ.get("FISH_STATUS_LLM_MODEL")
        or cfg.status_llm_model
        or os.environ.get("OPENAI_MODEL")
        or os.environ.get("AGENT_MODEL")
        or _DEFAULT_MODEL
    ).strip()
    api_key = ""
    if mode != "none":
        api_key = (
            os.environ.get("FISH_STATUS_LLM_API_KEY")
            or cfg.status_llm_api_key
            or os.environ.get("OPENROUTER_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or ""
        ).strip()
    return api_key or None, base_url, model, mode

_AUDIENCE_RULES = {
    "work": (
        "Audience: work friend. Share only work-relevant activity: project area, "
        "tooling, coding/design/review/docs state, and broad availability. Hide "
        "personal apps, entertainment, private messages, health, finance, and "
        "relationship context."
    ),
    "friends": (
        "Audience: regular friend. Share lightweight activity and availability. "
        "Prefer broad vibe/category over work details. Hide private messages, "
        "sensitive work/client details, health, finance, and legal context."
    ),
    "close": (
        "Audience: close friend or partner. You may share richer activity and "
        "availability, but still hide secrets, passwords, private message content, "
        "health, finance, legal, NSFW, and sensitive documents."
    ),
    "custom": (
        "Audience: custom. Follow the custom sharing instruction below, after "
        "applying the hard privacy rules."
    ),
}


def _build_context(rows: list[dict], max_rows: int = 8) -> str:
    lines = []
    for r in rows[:max_rows]:
        app = r.get("app") or "?"
        win = r.get("window") or ""
        ocr = (r.get("ocr_text") or "").replace("\n", " ")[:200]
        lines.append(f"  {app} — {win}: {ocr}")
    return "\n".join(lines) or "(no recent context)"


def _activity_context(activity: dict | None) -> str:
    if not activity:
        return "(no recent context)"
    category = activity.get("category") or "idle"
    status = activity.get("status") or ""
    return f"  current activity — {category}: {status}"


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


def _close_digest_from_activity(activity: dict | None, history: list[dict], fallback: dict) -> dict:
    if not activity:
        digest = dict(fallback)
    else:
        digest = _sanitize_digest(activity, flow=bool(activity.get("flow", False)))
    if history:
        digest["history"] = history
        digest["view"] = "activity_history"
    return digest


def _call_llm(api_key: str | None, base_url: str, model: str, prompt: str) -> dict | None:
    if not api_key:
        return None
    import urllib.request, urllib.error
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
        "max_tokens": 100,
    }).encode()
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=body, method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        click.echo(f"  llm http error {e.code}: {e.read()[:200]!r}", err=True)
        return None
    except Exception as e:
        click.echo(f"  llm error: {e}", err=True)
        return None

    try:
        content = data["choices"][0]["message"]["content"]
        return json.loads(content)
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        click.echo(f"  llm parse error: {e}", err=True)
        return None


def _fallback_digest(rows: list[dict]) -> dict:
    """Conservative no-key fallback based on app/window class, not OCR text."""
    row = rows[0] if rows else {}
    app = (row.get("app") or "").lower()
    window = (row.get("window") or "").lower()
    haystack = f"{app} {window}"
    rules = [
        (("cursor", "code", "xcode", "pycharm", "zed"), {
            "emoji": "💻", "category": "coding", "status": "coding",
        }),
        (("terminal", "iterm", "warp"), {
            "emoji": "⌨️", "category": "terminal", "status": "using terminal",
        }),
        (("slack", "discord", "messages", "wechat", "telegram", "whatsapp"), {
            "emoji": "💬", "category": "chat", "status": "in chat",
        }),
        (("zoom", "meet", "teams", "facetime"), {
            "emoji": "📞", "category": "meeting", "status": "in meeting",
        }),
        (("preview", "pdf", "books", "kindle"), {
            "emoji": "📖", "category": "reading", "status": "reading",
        }),
        (("chrome", "safari", "arc", "browser", "firefox"), {
            "emoji": "🌐", "category": "browsing", "status": "browsing",
        }),
        (("mail", "gmail", "outlook"), {
            "emoji": "✉️", "category": "email", "status": "checking email",
        }),
    ]
    for needles, digest in rules:
        if any(needle in haystack for needle in needles):
            return {**digest, "flow": False}
    return {"emoji": "💻", "category": "working", "status": "active on Mac", "flow": False}


def _run_query(since: str = "5m", limit: int = 10) -> list[dict]:
    cmd = [sys.executable, "-m", "fisherman", "query",
           "--since", since, "--limit", str(limit)]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        click.echo(f"  query failed: {res.stderr.strip()}", err=True)
        return []
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return []


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


def _policy_prompt(audience: str, prompt: str) -> str:
    rules = _AUDIENCE_RULES.get(audience, _AUDIENCE_RULES["friends"])
    if prompt:
        return f"{rules}\n\nCustom sharing instruction:\n{prompt}"
    return rules


def run_once(api_key: str | None, base_url: str, model: str, since: str = "5m") -> bool:
    rows = _run_query(since=since)
    current_activity = _current_activity()
    deterministic_flow = (
        bool(current_activity.get("flow", False))
        if current_activity is not None else None
    )

    if not rows and not current_activity:
        click.echo("  no recent context — skipping")
        return False
    friends = list_friends()
    if not friends:
        click.echo("  no friends — skipping status publish")
        return False
    has_close_friend = any(_policy_key(friend)[0] == "close" for friend in friends)
    activity_history = (
        _activity_history_entries(limit=8)
        if has_close_friend and current_activity is not None else []
    )

    by_policy: dict[tuple[str, str], list[dict]] = {}
    for friend in friends:
        by_policy.setdefault(_policy_key(friend), []).append(friend)

    ok_any = False
    context = _build_context(rows) if rows else _activity_context(current_activity)
    for (audience, custom_prompt), group in by_policy.items():
        if audience == "close":
            if current_activity is None:
                click.echo("  close audience skipped: current activity unavailable")
                continue
            # Close friends get the same sanitized activity status/history that
            # drives the local card. No second LLM pass and no audience rewrite.
            digest = _close_digest_from_activity(current_activity, activity_history, {})
        else:
            policy = _policy_prompt(audience, custom_prompt)
            prompt = _PROMPT.format(context=context) + "\n\n" + policy
            digest = _call_llm(api_key, base_url, model, prompt)
            if digest is None:
                digest = _fallback_digest(rows)
                click.echo(
                    f"  using heuristic fallback for {audience}: "
                    f"{digest['emoji']} {digest['category']} {digest['status']}"
                )
            digest = _sanitize_digest(digest, flow=deterministic_flow)
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
              help="Time window of context to feed the LLM each cycle")
@click.option("--model", default=None, help="Model id (default: $AGENT_MODEL or mistralai/mistral-nemo)")
@click.option("--once", is_flag=True, help="Run a single iteration and exit")
def main(interval, since, model, once):
    """Run a status-publishing loop using OpenRouter/OpenAI."""
    api_key, base_url, model, mode = _llm_settings(model)

    click.echo(f"agent loop: mode={mode} model={model} every {interval}s window={since}")
    if not api_key:
        click.echo("agent loop: status LLM key not set; using safe heuristic fallback")
    if once:
        run_once(api_key, base_url, model, since=since)
        return
    while True:
        try:
            ts = time.strftime("%H:%M:%S")
            click.echo(f"[{ts}] cycle")
            run_once(api_key, base_url, model, since=since)
        except KeyboardInterrupt:
            return
        except Exception as e:
            click.echo(f"  cycle failed: {e}", err=True)
        time.sleep(interval)


if __name__ == "__main__":
    main()
