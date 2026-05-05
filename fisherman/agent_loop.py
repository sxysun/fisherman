"""Tiny status-loop companion: read context, summarize via LLM, publish.

This is a SEPARATE concern from fisherman the protocol. The daemon ships
no LLM client. This file is the optional "make my friends see live status"
glue, intended to run as its own process.

Default LLM is OpenRouter (or any OpenAI-compatible endpoint) configured
via env:

    OPENAI_API_KEY    — required (your OpenRouter / OpenAI / Anthropic-via-OR key)
    OPENAI_BASE_URL   — defaults to https://openrouter.ai/api/v1
    AGENT_MODEL       — model id (default: openai/gpt-4o-mini)

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
from typing import Any

import click


_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
_DEFAULT_MODEL = "openai/gpt-4o-mini"
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


def _build_context(rows: list[dict], max_rows: int = 8) -> str:
    lines = []
    for r in rows[:max_rows]:
        app = r.get("app") or "?"
        win = r.get("window") or ""
        ocr = (r.get("ocr_text") or "").replace("\n", " ")[:200]
        lines.append(f"  {app} — {win}: {ocr}")
    return "\n".join(lines) or "(no recent context)"


def _call_llm(api_key: str, base_url: str, model: str, prompt: str) -> dict | None:
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


def _publish(digest: dict) -> bool:
    cmd = [sys.executable, "-m", "fisherman", "publish-status", "--from-stdin"]
    res = subprocess.run(
        cmd, input=json.dumps(digest), capture_output=True, text=True, timeout=15
    )
    if res.returncode != 0:
        click.echo(f"  publish failed: {res.stderr.strip()}", err=True)
        return False
    return True


def run_once(api_key: str, base_url: str, model: str, since: str = "5m") -> bool:
    rows = _run_query(since=since)
    if not rows:
        click.echo("  no recent context — skipping")
        return False
    prompt = _PROMPT.format(context=_build_context(rows))
    digest = _call_llm(api_key, base_url, model, prompt)
    if digest is None:
        return False
    # Drop unknown keys + clamp lengths
    digest = {
        "emoji": (digest.get("emoji") or "❓")[:8],
        "category": (digest.get("category") or "idle")[:20],
        "status": (digest.get("status") or "")[:30],
        "flow": bool(digest.get("flow", False)),
    }
    ok = _publish(digest)
    if ok:
        click.echo(f"  published: {digest['emoji']} {digest['category']:<12} {digest['status']}")
    return ok


@click.command()
@click.option("--interval", default=_DEFAULT_INTERVAL, show_default=True,
              help="Seconds between status updates")
@click.option("--since", default="5m", show_default=True,
              help="Time window of context to feed the LLM each cycle")
@click.option("--model", default=None, help="Model id (default: $AGENT_MODEL or openai/gpt-4o-mini)")
@click.option("--once", is_flag=True, help="Run a single iteration and exit")
def main(interval, since, model, once):
    """Run a status-publishing loop using OpenRouter/OpenAI."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        click.echo("OPENAI_API_KEY not set", err=True)
        sys.exit(2)
    base_url = os.environ.get("OPENAI_BASE_URL", _DEFAULT_BASE_URL)
    model = model or os.environ.get("AGENT_MODEL", _DEFAULT_MODEL)

    click.echo(f"agent loop: model={model} every {interval}s window={since}")
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
