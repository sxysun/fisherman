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


_CATEGORY_PALETTE = {
    "coding":        "#a8d4a8",
    "terminal":      "#a8d4a8",
    "implementation":"#a8d4a8",
    "debugging":     "#f5a5a5",
    "fixing bugs":   "#f5a5a5",
    "design":        "#d4b0e0",
    "ux":            "#d4b0e0",
    "browsing":      "#9cb8d8",
    "reading":       "#f0d489",
    "reading docs":  "#f0d489",
    "documentation": "#f0d489",
    "chat":          "#f5b8d4",
    "messaging":     "#f5b8d4",
    "authentication":"#e8c8a8",
    "data viz":      "#9cb8d8",
    "research":      "#c0d8b8",
    "writing":       "#e0c8a8",
    "review":        "#cccccc",
    "planning":      "#b8d4c8",
    "meeting":       "#f5b8d4",
    "video":         "#c8a8d0",
}


def _category_color(category: str | None) -> str:
    if not category:
        return "#bcbcc4"
    key = (category or "").lower().strip()
    return _CATEGORY_PALETTE.get(key, "#bcbcc4")


def _group_runs(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse adjacent same-category events into a single session run."""
    runs: list[dict[str, Any]] = []
    for ev in events:
        cat = (ev["digest"].get("category") or "").lower().strip()
        emoji = (ev["digest"].get("emoji") or "").strip()
        status = (ev["digest"].get("status") or "").strip()
        if runs and runs[-1]["category"] == cat:
            runs[-1]["end_ts"] = ev["ts"]
            runs[-1]["events"].append({"ts": ev["ts"], "emoji": emoji, "status": status})
            runs[-1]["emojis"][emoji] = runs[-1]["emojis"].get(emoji, 0) + 1
        else:
            runs.append({
                "category": cat,
                "start_ts": ev["ts"],
                "end_ts": ev["ts"],
                "events": [{"ts": ev["ts"], "emoji": emoji, "status": status}],
                "emojis": {emoji: 1} if emoji else {},
            })
    for r in runs:
        if r["emojis"]:
            r["dominant_emoji"] = max(r["emojis"].items(), key=lambda kv: kv[1])[0] or "·"
        else:
            r["dominant_emoji"] = "·"
        r["duration_s"] = max(60, int(r["end_ts"] - r["start_ts"]))
        r["color"] = _category_color(r["category"])
        seen_status: set[str] = set()
        r["distinct_statuses"] = []
        for e in r["events"]:
            s = e["status"]
            if s and s not in seen_status:
                seen_status.add(s)
                r["distinct_statuses"].append(s)
    return runs


def _fmt_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    rem = minutes % 60
    return f"{hours}h{rem:02d}m" if rem else f"{hours}h"


def _category_stats(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return [{category, color, count, share}] sorted by count desc."""
    if not events:
        return []
    counts: dict[str, int] = {}
    for ev in events:
        cat = ((ev["digest"].get("category") or "").lower().strip() or "other")
        counts[cat] = counts.get(cat, 0) + 1
    total = sum(counts.values()) or 1
    rows = [{"category": c, "count": n, "share": n / total, "color": _category_color(c)}
            for c, n in counts.items()]
    rows.sort(key=lambda r: r["count"], reverse=True)
    return rows


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

    # ----- hero header -----
    day_obj: _dt.date | None = None
    if day_label:
        try:
            day_obj = _dt.date.fromisoformat(day_label.split(" ")[0])
        except ValueError:
            day_obj = None
    if day_obj:
        weekday_str = day_obj.strftime("%A")
        date_str = day_obj.strftime("%B %-d")
        year_str = day_obj.strftime("%Y")
    else:
        weekday_str = ""
        date_str = day_label or "today"
        year_str = ""

    # ----- stats -----
    all_inferred = inferred_events
    runs = _group_runs(all_inferred)
    cat_stats = _category_stats(all_inferred)
    n_events = len(all_inferred)
    n_categories = len(cat_stats)
    hours_touched = len({_dt.datetime.fromtimestamp(e["ts"]).hour for e in all_inferred})
    top_cat = cat_stats[0]["category"] if cat_stats else "—"
    time_span = ""
    if all_inferred:
        first_ts = all_inferred[0]["ts"]
        last_ts = all_inferred[-1]["ts"]
        time_span = f"{_fmt_hhmm(first_ts)} → {_fmt_hhmm(last_ts)}"

    # ----- category share bar -----
    cat_bar_html = ""
    if cat_stats:
        segments = "".join(
            f"<div class='seg' style='flex: {r['share']:.4f}; background: {r['color']};' "
            f"title='{esc(r['category'])} · {r['count']}'></div>"
            for r in cat_stats
        )
        chips = "".join(
            f"<span class='chip'>"
            f"<span class='swatch' style='background:{r['color']}'></span>"
            f"<span class='chip-label'>{esc(r['category'])}</span>"
            f"<span class='chip-count'>{r['count']}</span>"
            f"</span>"
            for r in cat_stats
        )
        cat_bar_html = (
            "<div class='cat-section'>"
            f"<div class='cat-bar'>{segments}</div>"
            f"<div class='cat-chips'>{chips}</div>"
            "</div>"
        )

    # ----- activity timeline (grouped into runs) -----
    runs_html = ""
    if runs:
        rendered_runs = []
        for r in runs:
            ev_count = len(r["events"])
            start_label = _fmt_hhmm(r["start_ts"])
            duration = _fmt_duration(r["duration_s"])
            n_distinct = len(r["distinct_statuses"])
            # single-status run → compact one-liner
            if n_distinct == 1 and ev_count == 1:
                rendered_runs.append(f"""
  <article class='run run-single' style='--accent: {r["color"]};'>
    <span class='run-time'>{esc(start_label)}</span>
    <span class='run-emoji'>{esc(r['dominant_emoji'])}</span>
    <span class='run-cat'>{esc(r['category'] or 'other')}</span>
    <span class='run-status'>{esc(r['distinct_statuses'][0])}</span>
  </article>""")
                continue
            # multi-status run → header + bullet list
            statuses_html = "".join(
                f"<li>{esc(s)}</li>" for s in r["distinct_statuses"][:8]
            )
            if not statuses_html:
                statuses_html = "<li class='dim'>(no status text)</li>"
            ev_more = ""
            if n_distinct > 8:
                ev_more = f"<li class='dim'>+ {n_distinct - 8} more</li>"
            meta_bits = []
            if ev_count > 1:
                meta_bits.append(f"{ev_count} events")
            if r["duration_s"] > 60:
                meta_bits.append(esc(duration))
            meta_html = ""
            if meta_bits:
                meta_html = (
                    "<span class='run-meta'>"
                    + "".join(f"<span class='dot'>·</span><span>{m}</span>" for m in meta_bits)
                    + "</span>"
                )
            rendered_runs.append(f"""
  <article class='run run-multi' style='--accent: {r["color"]};'>
    <header class='run-head'>
      <span class='run-time'>{esc(start_label)}</span>
      <span class='run-emoji'>{esc(r['dominant_emoji'])}</span>
      <span class='run-cat'>{esc(r['category'] or 'other')}</span>
      {meta_html}
    </header>
    <ul class='run-body'>{statuses_html}{ev_more}</ul>
  </article>""")
        runs_html = "".join(rendered_runs)

    # ----- published statuses -----
    published_html = ""
    if my_events:
        rows = []
        for ev in my_events:
            left, right = _digest_one_line(ev["digest"])
            t = _fmt_hhmm(ev["ts"])
            names = [friend_idx.get(r, (r or "?")[:8]) for r in (ev.get("recipients") or [])]
            recipients_html = ""
            if names:
                recipients_html = (
                    "<span class='recipients'>→ "
                    + " ".join(f"<span class='handle'>@{esc(n)}</span>" for n in names)
                    + "</span>"
                )
            rows.append(
                f"<div class='pub-row'>"
                f"<span class='ts'>{esc(t)}</span>"
                f"<span class='pub-left'>{esc(left)}</span>"
                f"<span class='pub-right'>{esc(right)}</span>"
                f"{recipients_html}"
                f"</div>"
            )
        published_html = (
            "<section class='block'>"
            "<h2>published</h2>"
            f"<div class='pub-list'>{''.join(rows)}</div>"
            "</section>"
        )
    elif my_events is not None and not my_events:
        # Only show empty published section if user hinted they care (had any events at all)
        pass

    # ----- friends section -----
    friends_html = ""
    if friend_events is not None:
        if friend_events:
            rows = []
            for ev in friend_events:
                left, right = _digest_one_line(ev["digest"])
                t = _fmt_hhmm(ev["ts"])
                handle = ev.get("friend_name") or "?"
                rows.append(
                    f"<div class='pub-row'>"
                    f"<span class='ts'>{esc(t)}</span>"
                    f"<span class='handle big-handle'>@{esc(handle)}</span>"
                    f"<span class='pub-left'>{esc(left)}</span>"
                    f"<span class='pub-right'>{esc(right)}</span>"
                    f"</div>"
                )
            friends_html = (
                "<section class='block'>"
                "<h2>friends</h2>"
                f"<div class='pub-list'>{''.join(rows)}</div>"
                "</section>"
            )
        else:
            friends_html = (
                "<section class='block'>"
                "<h2>friends</h2>"
                "<p class='empty'>no friend statuses in this window — "
                "is the relay reachable?</p>"
                "</section>"
            )

    activity_section = ""
    if runs_html:
        activity_section = (
            "<section class='block'>"
            "<h2>activity</h2>"
            f"<div class='runs'>{runs_html}</div>"
            "</section>"
        )
    elif inferred_events is not None:
        activity_section = (
            "<section class='block'>"
            "<h2>activity</h2>"
            "<p class='empty'>no activity inferred yet — "
            "categorizer may still be warming up</p>"
            "</section>"
        )

    if not (activity_section or published_html or friends_html):
        body_html = "<p class='empty very-big'>nothing for this day</p>"
    else:
        body_html = activity_section + published_html + friends_html

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Daily Card · {esc(date_str)}</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #0a0a0c;
      --surface: #131318;
      --surface-2: #1b1b22;
      --border: rgba(255,255,255,.06);
      --border-2: rgba(255,255,255,.04);
      --text: #f4f4f5;
      --text-2: #d4d4d8;
      --muted: #a1a1aa;
      --muted-2: #71717a;
      --muted-3: #52525b;
      --accent: #c8e0a8;
      --info: #9bb5e8;
      --love: #f5b4d4;

      --font-sans: -apple-system, BlinkMacSystemFont, "SF Pro Text",
                    "Inter", system-ui, sans-serif;
      --font-display: "SF Pro Display", -apple-system, BlinkMacSystemFont,
                       system-ui, sans-serif;
      --font-mono: ui-monospace, SFMono-Regular, "JetBrains Mono", Menlo, monospace;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{
      margin: 0; padding: 0;
      background: var(--bg);
      color: var(--text);
      font-family: var(--font-sans);
      font-size: 14.5px;
      line-height: 1.55;
      -webkit-font-smoothing: antialiased;
      text-rendering: optimizeLegibility;
    }}
    body {{
      padding: 40px 24px 56px;
      display: flex;
      justify-content: center;
    }}
    main {{
      width: min(760px, 100%);
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 18px;
      padding: 36px 40px 38px;
      box-shadow: 0 24px 70px rgba(0,0,0,.36);
    }}

    /* ----- hero ----- */
    .hero {{
      display: flex; align-items: baseline; justify-content: space-between;
      gap: 18px; flex-wrap: wrap;
      padding-bottom: 22px;
      border-bottom: 1px solid var(--border);
      margin-bottom: 26px;
    }}
    .hero .left {{ display: flex; flex-direction: column; gap: 4px; }}
    .hero .kicker {{
      font-size: 11px; font-weight: 600;
      letter-spacing: .12em; text-transform: uppercase;
      color: var(--muted-2);
    }}
    .hero .date {{
      font-family: var(--font-display);
      font-size: 36px; font-weight: 600;
      letter-spacing: -.02em;
      line-height: 1.05;
      color: var(--text);
    }}
    .hero .weekday {{
      color: var(--accent);
      font-weight: 500;
    }}
    .hero .year {{ color: var(--muted-2); font-weight: 400; margin-left: 6px; }}
    .hero .right {{
      font-family: var(--font-mono);
      font-size: 12px;
      color: var(--muted-2);
      text-align: right;
    }}
    .hero .span {{ display: block; margin-top: 4px; color: var(--muted); }}

    /* ----- stat tiles ----- */
    .stats {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
      gap: 10px;
      margin-bottom: 24px;
    }}
    .stat {{
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: 9px;
      padding: 11px 14px;
    }}
    .stat .label {{
      font-size: 10px; color: var(--muted-2);
      text-transform: uppercase; letter-spacing: .08em;
    }}
    .stat .value {{
      font-family: var(--font-display);
      font-size: 22px; font-weight: 500;
      letter-spacing: -.01em;
      margin-top: 4px;
      color: var(--text);
    }}
    .stat .value.text {{ font-size: 16px; }}

    /* ----- category share ----- */
    .cat-section {{ margin-bottom: 24px; }}
    .cat-bar {{
      display: flex; width: 100%;
      height: 8px; border-radius: 4px;
      overflow: hidden;
      background: var(--surface-2);
      margin-bottom: 10px;
    }}
    .cat-bar .seg {{ height: 100%; min-width: 2px; }}
    .cat-chips {{
      display: flex; flex-wrap: wrap; gap: 6px;
    }}
    .chip {{
      display: inline-flex; align-items: center; gap: 6px;
      padding: 3px 9px 3px 7px;
      border-radius: 999px;
      background: var(--surface-2);
      border: 1px solid var(--border);
      font-size: 12px;
    }}
    .chip .swatch {{
      display: inline-block;
      width: 7px; height: 7px;
      border-radius: 50%;
    }}
    .chip-label {{ color: var(--text-2); }}
    .chip-count {{ color: var(--muted-2); font-family: var(--font-mono); font-size: 11px; }}

    /* ----- sections ----- */
    .block {{ margin-top: 28px; }}
    h2 {{
      font-size: 11px; font-weight: 600;
      text-transform: uppercase; letter-spacing: .1em;
      color: var(--muted-2);
      margin: 0 0 14px;
    }}

    /* ----- activity runs ----- */
    .runs {{ display: flex; flex-direction: column; gap: 1px; }}
    .run {{
      border-left: 3px solid var(--accent);
      padding-left: 14px;
      margin-left: 2px;
    }}
    /* single-event row — compact one-liner */
    .run-single {{
      display: grid;
      grid-template-columns: 50px 22px minmax(110px, 0.30fr) 1fr;
      align-items: baseline;
      gap: 12px;
      padding: 6px 0;
    }}
    .run-single .run-time {{
      font-family: var(--font-mono);
      font-size: 12px;
      color: var(--muted-2);
    }}
    .run-single .run-emoji {{ font-size: 15px; line-height: 1; text-align: center; }}
    .run-single .run-cat {{
      font-size: 13px; color: var(--muted);
      letter-spacing: .005em;
    }}
    .run-single .run-status {{
      color: var(--text);
      font-size: 13.5px;
    }}

    /* multi-event session block */
    .run-multi {{
      padding: 14px 0 12px;
    }}
    .run-multi + .run-multi {{ margin-top: 4px; }}
    .run-head {{
      display: flex; align-items: baseline; gap: 10px;
      flex-wrap: wrap;
      margin-bottom: 8px;
    }}
    .run-head .run-time {{
      font-family: var(--font-mono);
      font-size: 12px;
      color: var(--muted-2);
      min-width: 40px;
    }}
    .run-head .run-emoji {{ font-size: 17px; line-height: 1; }}
    .run-head .run-cat {{
      font-weight: 600; font-size: 14px;
      color: var(--text);
      letter-spacing: .005em;
    }}
    .run-meta {{
      color: var(--muted-2); font-size: 11px;
      font-family: var(--font-mono);
      display: inline-flex; align-items: center; gap: 6px;
    }}
    .run-meta .dot {{ color: var(--muted-3); }}
    .run-body {{
      list-style: none; padding: 0; margin: 0;
      display: flex; flex-direction: column; gap: 3px;
      font-size: 13.5px;
      color: var(--text-2);
      padding-left: 4px;
    }}
    .run-body li {{
      position: relative;
      padding-left: 14px;
    }}
    .run-body li::before {{
      content: "·";
      position: absolute; left: 2px; top: -1px;
      color: var(--muted-3);
    }}
    .run-body li.dim {{ color: var(--muted-3); font-style: italic; }}

    /* ----- published / friends rows ----- */
    .pub-list {{ display: flex; flex-direction: column; gap: 0; }}
    .pub-row {{
      display: flex; align-items: baseline; gap: 12px;
      padding: 8px 0;
      border-bottom: 1px solid var(--border-2);
      font-size: 13.5px;
    }}
    .pub-row:last-child {{ border-bottom: none; }}
    .pub-row .ts {{
      font-family: var(--font-mono);
      font-size: 12px;
      color: var(--muted-2);
      min-width: 44px;
    }}
    .pub-row .pub-left {{
      color: var(--text-2);
      min-width: 130px;
    }}
    .pub-row .pub-right {{ color: var(--text); flex: 1; }}
    .pub-row .recipients {{
      color: var(--muted-2); font-size: 12px;
    }}
    .pub-row .handle {{
      color: var(--love);
      font-size: 12px;
    }}
    .pub-row .handle.big-handle {{
      color: var(--info);
      font-weight: 500;
      font-size: 13px;
      min-width: 80px;
    }}

    .empty {{ color: var(--muted-3); font-style: italic; }}
    .empty.very-big {{ text-align: center; padding: 40px 0; font-size: 16px; }}

    @media (max-width: 480px) {{
      body {{ padding: 22px 12px; }}
      main {{ padding: 24px 20px; }}
      .hero .date {{ font-size: 28px; }}
      .run {{ padding-left: 12px; }}
    }}
  </style>
</head>
<body>
  <main>
    <header class='hero'>
      <div class='left'>
        <div class='kicker'>Daily Card</div>
        <div class='date'>
          {f"<span class='weekday'>{esc(weekday_str)}</span> " if weekday_str else ""}<span>{esc(date_str)}</span>
          {f"<span class='year'>{esc(year_str)}</span>" if year_str else ""}
        </div>
      </div>
      <div class='right'>
        {f"<div>{n_events} event{'' if n_events == 1 else 's'}</div>" if n_events else ""}
        {f"<span class='span'>{esc(time_span)}</span>" if time_span else ""}
      </div>
    </header>

    {(
      "<div class='stats'>"
      f"<div class='stat'><div class='label'>events</div><div class='value'>{n_events}</div></div>"
      f"<div class='stat'><div class='label'>categories</div><div class='value'>{n_categories}</div></div>"
      f"<div class='stat'><div class='label'>active hours</div><div class='value'>{hours_touched}</div></div>"
      f"<div class='stat'><div class='label'>top focus</div><div class='value text'>{esc(top_cat)}</div></div>"
      "</div>"
    ) if n_events else ""}

    {cat_bar_html}

    {body_html}
  </main>
</body>
</html>
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
