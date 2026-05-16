"""Simple timeline view over published status events — the new daily card.

This replaces the old generative daily-card pipeline. There's no LLM,
no inference, no fields synthesis. The card IS the chronological log
of status events: yours by default, friends' statuses too with --friends.

Reads ~/.fisherman/status-log.jsonl (written by ledger.publish_status)
and groups same-digest publishes within a 5-second window into one
display row (so publishing a status to four friends shows as one row
with four recipients).

Output formats: plain text (default) and HTML (--html out.html).
"""

from __future__ import annotations

import datetime as _dt
import html as _html
import json
import os
import re
import time
from pathlib import Path
from typing import Any


def status_log_path() -> Path:
    return Path.home() / ".fisherman" / "status-log.jsonl"


# ---------- time bounds ----------

def _parse_duration(value: str) -> float | None:
    m = re.match(r"^(\d+)([smhd])$", str(value).strip().lower())
    if not m:
        return None
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def window_bounds(*, day: str | None = None, since: str | None = None) -> tuple[float, float]:
    """Resolve --day / --since into (start_ts, end_ts). Default: today."""
    now = time.time()
    if day:
        start = _dt.datetime.fromisoformat(f"{day}T00:00:00").timestamp()
        end = _dt.datetime.fromisoformat(f"{day}T23:59:59.999999").timestamp()
        return start, end
    if since:
        delta = _parse_duration(since)
        if delta is None:
            try:
                delta = float(since)
            except ValueError:
                delta = 86400.0
        return now - delta, now
    today = _dt.datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    return (
        _dt.datetime.fromisoformat(f"{today}T00:00:00").timestamp(),
        _dt.datetime.fromisoformat(f"{today}T23:59:59.999999").timestamp(),
    )


# ---------- my events ----------

def load_my_events(*, since_ts: float, until_ts: float) -> list[dict[str, Any]]:
    path = status_log_path()
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            ts = float(row.get("ts") or 0)
            if since_ts <= ts <= until_ts:
                rows.append(row)
    return rows


def group_my_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse same-digest publishes within 5s into one event with a recipients list."""
    rows = sorted(rows, key=lambda r: r.get("ts") or 0)
    out: list[dict[str, Any]] = []
    for row in rows:
        digest = row.get("digest") or {}
        ts = float(row.get("ts") or 0)
        recipient = row.get("recipient_pubkey")
        if out:
            last = out[-1]
            if last["digest"] == digest and abs(last["ts"] - ts) < 5.0:
                if recipient and recipient not in last["recipients"]:
                    last["recipients"].append(recipient)
                continue
        out.append({
            "ts": ts,
            "digest": digest,
            "recipients": [recipient] if recipient else [],
        })
    return out


# ---------- friend events (optional) ----------

def load_friend_events(*, since_ts: float, until_ts: float,
                        limit_per_friend: int = 30) -> list[dict[str, Any]]:
    """Fetch friends' recent statuses for me from each friend's relay.

    Returns flat list of {ts, friend_pubkey_hex, friend_name, digest}.
    Skips friends we can't fetch (relay unreachable, missing keys, etc.).

    Uses the same key + relay resolution pattern as `fisherman friend status`.
    """
    try:
        from fisherman.friends import list_friends
        from fisherman.ledger import fetch_friend_status, LedgerError
        from fisherman.cli import _load_keys, _ledger_url
    except Exception:
        return []
    try:
        _priv, my_pub, my_x_priv, _my_x_pub = _load_keys()
    except Exception:
        return []
    default_relay = _ledger_url()
    rows: list[dict[str, Any]] = []
    for friend in list_friends() or []:
        friend_pubkey = friend.get("pubkey_hex")
        friend_x = friend.get("encryption_pubkey")
        if not friend_pubkey or not friend_x:
            continue
        relay = friend.get("relay_url") or default_relay
        if not relay:
            continue
        try:
            events = fetch_friend_status(
                relay_url=relay,
                friend_pubkey_hex=friend_pubkey,
                friend_x25519_pubkey_hex=friend_x,
                recipient_pubkey_bytes=my_pub,
                recipient_x25519_priv=my_x_priv,
                since_ts=since_ts,
                limit=limit_per_friend,
            )
        except LedgerError:
            continue
        except Exception:
            continue
        for ev in events or []:
            ts = float(ev.get("ts") or 0)
            if not (since_ts <= ts <= until_ts):
                continue
            rows.append({
                "ts": ts,
                "friend_pubkey_hex": friend_pubkey,
                "friend_name": friend.get("name") or friend_pubkey[:8],
                "digest": ev.get("digest") or {},
            })
    rows.sort(key=lambda r: r["ts"])
    return rows


# ---------- friend name lookup ----------

def friends_index() -> dict[str, str]:
    """Map pubkey_hex → friend name, for rendering recipient lists."""
    try:
        from fisherman.friends import list_friends
    except Exception:
        return {}
    out: dict[str, str] = {}
    for f in list_friends() or []:
        pk = f.get("pubkey_hex")
        name = f.get("name") or (pk[:8] if pk else "?")
        if pk:
            out[pk] = name
    return out


# ---------- rendering ----------

def _fmt_hhmm(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts).strftime("%H:%M")


def _digest_one_line(digest: dict[str, Any]) -> tuple[str, str]:
    """Return (left, right) where left is `<emoji> <category>` and right is the status text."""
    emoji = (digest.get("emoji") or "").strip()
    category = (digest.get("category") or "").strip()
    status = (digest.get("status") or "").strip()
    flow = digest.get("flow")
    left_parts = []
    if emoji:
        left_parts.append(emoji)
    if category:
        left_parts.append(category)
    if flow:
        left_parts.append("(flow)")
    left = " ".join(left_parts) or "(no label)"
    return left, status


def render_text(
    *,
    my_events: list[dict[str, Any]],
    friend_events: list[dict[str, Any]] | None,
    day_label: str | None,
    friend_idx: dict[str, str] | None = None,
) -> str:
    friend_idx = friend_idx or {}
    lines: list[str] = []
    n_mine = len(my_events)
    n_friends = len(friend_events) if friend_events else 0
    if friend_events:
        summary = f"{n_mine} mine + {n_friends} friend{'' if n_friends == 1 else 's'}"
    else:
        summary = f"{n_mine} event{'' if n_mine == 1 else 's'}"
    header = "Daily Card · " + (day_label or "(today)") + " · " + summary
    lines.append(header)
    lines.append("")

    if friend_events is not None:
        lines.append("mine")
    for ev in my_events:
        left, right = _digest_one_line(ev["digest"])
        time_s = _fmt_hhmm(ev["ts"])
        suffix = ""
        if ev.get("recipients"):
            names = [friend_idx.get(r, (r or "?")[:8]) for r in ev["recipients"]]
            suffix = "  → " + ", ".join("@" + n for n in names)
        if right:
            lines.append(f"  {time_s}  {left:18s}  {right}{suffix}")
        else:
            lines.append(f"  {time_s}  {left}{suffix}")
    if not my_events:
        lines.append("  (no statuses published)")

    if friend_events is not None and friend_events:
        lines.append("")
        lines.append("friends")
        for ev in friend_events:
            left, right = _digest_one_line(ev["digest"])
            time_s = _fmt_hhmm(ev["ts"])
            handle = "@" + (ev.get("friend_name") or "?")
            if right:
                lines.append(f"  {time_s}  {handle:14s}  {left:18s}  {right}")
            else:
                lines.append(f"  {time_s}  {handle:14s}  {left}")
    elif friend_events is not None and not friend_events:
        lines.append("")
        lines.append("friends")
        lines.append("  (no statuses from friends in this window)")

    return "\n".join(lines)


def render_html(
    *,
    my_events: list[dict[str, Any]],
    friend_events: list[dict[str, Any]] | None,
    day_label: str | None,
    friend_idx: dict[str, str] | None = None,
) -> str:
    friend_idx = friend_idx or {}

    def esc(value: object) -> str:
        return _html.escape(str(value or ""))

    def row_text(digest: dict[str, Any], suffix: str = "") -> str:
        left, right = _digest_one_line(digest)
        return (
            f"<span class='left'>{esc(left)}</span>"
            f"<span class='right'>{esc(right)}</span>"
            f"<span class='suffix'>{esc(suffix)}</span>"
        )

    n_mine = len(my_events)
    n_friends = len(friend_events) if friend_events else 0
    if friend_events:
        summary = f"{n_mine} mine + {n_friends} friend{'' if n_friends == 1 else 's'}"
    else:
        summary = f"{n_mine} event{'' if n_mine == 1 else 's'}"

    mine_rows = []
    for ev in my_events:
        names = [friend_idx.get(r, (r or "?")[:8]) for r in (ev.get("recipients") or [])]
        suffix = "→ " + ", ".join("@" + n for n in names) if names else ""
        mine_rows.append(
            f"<tr><td class='ts'>{esc(_fmt_hhmm(ev['ts']))}</td>"
            f"<td>{row_text(ev['digest'], suffix)}</td></tr>"
        )

    friend_rows = []
    if friend_events:
        for ev in friend_events:
            handle = "@" + esc(ev.get("friend_name") or "?")
            friend_rows.append(
                f"<tr><td class='ts'>{esc(_fmt_hhmm(ev['ts']))}</td>"
                f"<td><span class='handle'>{handle}</span> {row_text(ev['digest'])}</td></tr>"
            )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Daily Card · {esc(day_label or 'today')}</title>
<style>
  :root {{ color-scheme: dark; font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif; }}
  body {{ background:#0b0b0d; color:#f4f4f5; margin:0; padding:32px;
          display:flex; justify-content:center; }}
  main {{ width:min(720px, 100%); background:#121216; border:1px solid #1d1d22;
           border-radius:18px; padding:28px 32px; }}
  h1 {{ font-size:22px; margin:0 0 4px; letter-spacing:-.01em; }}
  .summary {{ color:#a1a1aa; font-size:13px; margin-bottom:22px; }}
  h2 {{ font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:.08em;
        color:#71717a; margin:22px 0 8px; }}
  table {{ width:100%; border-collapse:collapse; }}
  td {{ padding:6px 0; vertical-align:top; line-height:1.45; font-size:14px; }}
  td.ts {{ color:#71717a; font-family:ui-monospace, monospace; width:60px; padding-right:14px; }}
  .left {{ display:inline-block; min-width:140px; color:#d4d4d8; }}
  .right {{ color:#f4f4f5; }}
  .suffix {{ color:#52525b; margin-left:8px; font-size:13px; }}
  .handle {{ color:#9bb5e8; margin-right:10px; }}
</style></head>
<body><main>
  <h1>Daily Card · {esc(day_label or 'today')}</h1>
  <div class="summary">{esc(summary)}</div>
  {('<h2>mine</h2><table>' + ''.join(mine_rows) + '</table>') if friend_rows else ('<table>' + ''.join(mine_rows) + '</table>')}
  {('<h2>friends</h2><table>' + ''.join(friend_rows) + '</table>') if friend_rows else ''}
</main></body></html>
"""


# ---------- top-level helper ----------

def build_card(
    *,
    day: str | None = None,
    since: str | None = None,
    with_friends: bool = False,
) -> dict[str, Any]:
    since_ts, until_ts = window_bounds(day=day, since=since)
    my_raw = load_my_events(since_ts=since_ts, until_ts=until_ts)
    my_events = group_my_events(my_raw)
    friend_events = None
    if with_friends:
        friend_events = load_friend_events(since_ts=since_ts, until_ts=until_ts)
    label = day or (
        _dt.datetime.fromtimestamp(since_ts).strftime("%Y-%m-%d")
        if (until_ts - since_ts) <= 86400 + 1
        else (
            _dt.datetime.fromtimestamp(since_ts).strftime("%Y-%m-%d")
            + " → "
            + _dt.datetime.fromtimestamp(until_ts).strftime("%Y-%m-%d")
        )
    )
    return {
        "day_label": label,
        "since_ts": since_ts,
        "until_ts": until_ts,
        "my_events": my_events,
        "friend_events": friend_events,
        "friend_idx": friends_index(),
    }
