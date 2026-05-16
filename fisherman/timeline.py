"""Daily card as a timeline — three sources, one chronological log.

  1. Inferred activity from the backend's `/api/activity_history` endpoint
     (what the menubar notch displays, written by the server-side
     activity_categorizer). Authenticated with FishKey ed25519 sig.
  2. Your published statuses from ~/.fisherman/status-log.jsonl
     (written by ledger.publish_status when you `fisherman publish-status`).
  3. Optional friends' published statuses from the relay, when --friends
     is set. Same path as `fisherman friend status`.

No LLM, no field synthesis. The card IS the chronological log.

Output formats: plain text (default) and HTML (--html out.html).
"""

from __future__ import annotations

import datetime as _dt
import html as _html
import json
import os
import re
import time
import urllib.parse
import urllib.request
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


# ---------- inferred activity from backend ----------

def _fishkey_header(private_key_hex: str) -> str | None:
    """Build a FishKey Authorization header. Returns None on bad key."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))
        pub = priv.public_key().public_bytes_raw()
        ts = int(time.time())
        sig = priv.sign(f"fisherman:{ts}".encode())
        return f"FishKey {pub.hex()}:{ts}:{sig.hex()}"
    except Exception:
        return None


def _activity_api_url(cfg, path: str) -> str | None:
    """Derive http(s)://host:activity_port/path from cfg.backend_url + cfg.activity_port."""
    if not cfg.backend_url:
        return None
    parsed = urllib.parse.urlparse(cfg.backend_url)
    if not parsed.hostname:
        return None
    scheme = "https" if parsed.scheme in ("wss", "https") else "http"
    port = cfg.activity_port or 9998
    return f"{scheme}://{parsed.hostname}:{port}{path}"


def load_activity_history(
    *,
    since_ts: float,
    until_ts: float,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Fetch inferred activity from the backend's activity_categorizer.

    Returns list of {ts, digest:{emoji,category,status,flow}, source:'inferred'}.
    Empty list on network/auth/parsing failure (best-effort).
    """
    try:
        from fisherman.config import FishermanConfig
    except Exception:
        return []
    cfg = FishermanConfig()
    if not cfg.private_key:
        return []
    url = _activity_api_url(cfg, f"/api/activity_history?limit={limit}")
    if not url:
        return []
    auth = _fishkey_header(cfg.private_key)
    if not auth:
        return []
    req = urllib.request.Request(url, headers={"Authorization": auth})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for entry in data.get("entries") or []:
        ts_iso = entry.get("timestamp") or entry.get("ts")
        if isinstance(ts_iso, (int, float)):
            ts = float(ts_iso)
        elif isinstance(ts_iso, str):
            try:
                ts = _dt.datetime.fromisoformat(ts_iso.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
        else:
            continue
        if not (since_ts <= ts <= until_ts):
            continue
        out.append({
            "ts": ts,
            "digest": {
                "emoji": entry.get("emoji"),
                "category": entry.get("category"),
                "status": entry.get("status"),
                "flow": entry.get("flow", False),
            },
            "source": "inferred",
        })
    out.sort(key=lambda r: r["ts"])
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
    inferred_events: list[dict[str, Any]],
    my_events: list[dict[str, Any]],
    friend_events: list[dict[str, Any]] | None,
    day_label: str | None,
    friend_idx: dict[str, str] | None = None,
) -> str:
    friend_idx = friend_idx or {}
    lines: list[str] = []
    n_inferred = len(inferred_events)
    n_mine = len(my_events)
    n_friends = len(friend_events) if friend_events else 0
    bits: list[str] = []
    if n_inferred:
        bits.append(f"{n_inferred} activit{'y' if n_inferred == 1 else 'ies'}")
    if n_mine:
        bits.append(f"{n_mine} published")
    if friend_events:
        bits.append(f"{n_friends} friend{'' if n_friends == 1 else 's'}")
    summary = ", ".join(bits) if bits else "empty"
    lines.append(f"Daily Card · {day_label or '(today)'} · {summary}")
    lines.append("")

    if inferred_events:
        lines.append("activity")
        for ev in inferred_events:
            left, right = _digest_one_line(ev["digest"])
            t = _fmt_hhmm(ev["ts"])
            lines.append(f"  {t}  {left:18s}  {right}" if right else f"  {t}  {left}")

    if friend_events is not None or my_events:
        lines.append("")
        lines.append("published")
        if my_events:
            for ev in my_events:
                left, right = _digest_one_line(ev["digest"])
                t = _fmt_hhmm(ev["ts"])
                suffix = ""
                if ev.get("recipients"):
                    names = [friend_idx.get(r, (r or "?")[:8]) for r in ev["recipients"]]
                    suffix = "  → " + ", ".join("@" + n for n in names)
                if right:
                    lines.append(f"  {t}  {left:18s}  {right}{suffix}")
                else:
                    lines.append(f"  {t}  {left}{suffix}")
        else:
            lines.append("  (you haven't published a status in this window)")

    if friend_events is not None:
        lines.append("")
        lines.append("friends")
        if friend_events:
            for ev in friend_events:
                left, right = _digest_one_line(ev["digest"])
                t = _fmt_hhmm(ev["ts"])
                handle = "@" + (ev.get("friend_name") or "?")
                if right:
                    lines.append(f"  {t}  {handle:14s}  {left:18s}  {right}")
                else:
                    lines.append(f"  {t}  {handle:14s}  {left}")
        else:
            lines.append("  (no friend statuses in this window — relay reachable?)")

    if not inferred_events and not my_events and not friend_events:
        lines.append("  (nothing for this day)")

    return "\n".join(lines)


def render_html(
    *,
    inferred_events: list[dict[str, Any]],
    my_events: list[dict[str, Any]],
    friend_events: list[dict[str, Any]] | None,
    day_label: str | None,
    friend_idx: dict[str, str] | None = None,
) -> str:
    friend_idx = friend_idx or {}

    def esc(value: object) -> str:
        return _html.escape(str(value or ""))

    def row_html(ev: dict[str, Any], prefix_html: str = "") -> str:
        left, right = _digest_one_line(ev["digest"])
        return (
            f"<tr><td class='ts'>{esc(_fmt_hhmm(ev['ts']))}</td>"
            f"<td>{prefix_html}"
            f"<span class='left'>{esc(left)}</span>"
            f"<span class='right'>{esc(right)}</span>"
            f"</td></tr>"
        )

    bits = []
    if inferred_events: bits.append(f"{len(inferred_events)} activities")
    if my_events: bits.append(f"{len(my_events)} published")
    if friend_events: bits.append(f"{len(friend_events)} friends")
    summary = ", ".join(bits) if bits else "empty"

    activity_rows = "".join(row_html(ev) for ev in inferred_events)
    mine_rows = []
    for ev in my_events:
        names = [friend_idx.get(r, (r or "?")[:8]) for r in (ev.get("recipients") or [])]
        suffix = ""
        if names:
            suffix = "<span class='suffix'>→ " + ", ".join("@" + esc(n) for n in names) + "</span>"
        mine_rows.append(row_html(ev) + suffix)
    mine_table = "".join(mine_rows)

    friend_rows = ""
    if friend_events:
        for ev in friend_events:
            handle = f"<span class='handle'>@{esc(ev.get('friend_name') or '?')}</span> "
            friend_rows += row_html(ev, prefix_html=handle)

    sections = []
    if inferred_events:
        sections.append(f"<h2>activity</h2><table>{activity_rows}</table>")
    if my_events or friend_events is not None:
        sections.append(
            f"<h2>published</h2>"
            f"<table>{mine_table}</table>" if my_events else
            f"<h2>published</h2><p class='empty'>nothing published in this window</p>"
        )
    if friend_events is not None:
        if friend_events:
            sections.append(f"<h2>friends</h2><table>{friend_rows}</table>")
        else:
            sections.append("<h2>friends</h2><p class='empty'>no friend statuses in window</p>")

    body_html = "".join(sections)
    if not body_html:
        body_html = "<p class='empty'>nothing for this day</p>"

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
  .empty {{ color: #52525b; font-style: italic; }}
</style></head>
<body><main>
  <h1>Daily Card · {esc(day_label or 'today')}</h1>
  <div class="summary">{esc(summary)}</div>
  {body_html}
</main></body></html>
"""


# ---------- top-level helper ----------

def build_card(
    *,
    day: str | None = None,
    since: str | None = None,
    with_friends: bool = False,
    with_inferred: bool = True,
    inferred_limit: int = 200,
) -> dict[str, Any]:
    since_ts, until_ts = window_bounds(day=day, since=since)
    inferred_events: list[dict[str, Any]] = []
    if with_inferred:
        inferred_events = load_activity_history(
            since_ts=since_ts, until_ts=until_ts, limit=inferred_limit,
        )
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
        "inferred_events": inferred_events,
        "my_events": my_events,
        "friend_events": friend_events,
        "friend_idx": friends_index(),
    }
