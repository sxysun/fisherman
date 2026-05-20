from __future__ import annotations

import calendar
import re
import time
from collections import Counter
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from . import metrics as metrics_mod
from .store import iter_jsonl


REPORT_VERSION = "information_diet_v1"
RESEARCH_APPS = {"Google Chrome", "Safari", "Arc", "Firefox", "Brave Browser", "Chromium"}
RESEARCH_SCENES = {"reading_browser", "reading", "browsing", "other"}
MAX_EVENT_DELTA_SEC = 30
EPISODE_GAP_SEC = 180

STOPWORDS = {
    "about", "after", "again", "also", "and", "are", "because", "been", "but",
    "can", "code", "com", "from", "github", "have", "how", "http", "https",
    "into", "like", "not", "open", "page", "search", "that", "the", "this",
    "with", "what", "when", "where", "while", "will", "you", "your",
    "arc", "bin", "bookmarks", "brave", "chrome", "claude", "codex", "edit",
    "file", "firefox", "help", "history", "menu", "node", "nvm", "profile",
    "profiles", "safari", "shell", "tab", "terminal", "versions", "view",
    "window", "approvals", "bypass", "dangerously", "dangerously-skip",
    "sandbox", "skip", "wed",
}

COMMON_TLDS = {
    "ai", "co", "com", "dev", "edu", "fr", "gov", "io", "jp", "me", "net", "pe",
    "org", "uk", "us",
}


def build_report(*, window: str = "7d", max_episodes: int = 20) -> dict[str, Any]:
    since = metrics_mod.since_iso(window)
    candidates = [
        row for row in iter_jsonl("candidates.jsonl")
        if row.get("ts", "") >= since
    ]
    candidates.sort(key=lambda row: row.get("ts") or "")
    events = [_research_event(row) for row in candidates]
    episodes = _segment([event for event in events if event is not None])
    episode_rows = [_episode_summary(ep) for ep in episodes]
    skill_hypotheses = _skill_hypotheses(episode_rows)

    domains = Counter()
    terms = Counter()
    patterns = Counter()
    for episode in episode_rows:
        domains.update(episode["source_domains"])
        terms.update(episode["top_terms"])
        patterns.update(episode["workflow_patterns"])

    return {
        "version": REPORT_VERSION,
        "generated_at": _now_iso(),
        "window": window,
        "since": since,
        "summary": {
            "n_research_events": len([event for event in events if event is not None]),
            "n_episodes": len(episode_rows),
            "observed_research_min": round(sum(ep["observed_duration_min"] for ep in episode_rows), 2),
            "top_domains": dict(domains.most_common(12)),
            "top_terms": dict(terms.most_common(12)),
            "workflow_patterns": dict(patterns.most_common(12)),
        },
        "episodes": episode_rows[-max_episodes:][::-1],
        "skill_hypotheses": skill_hypotheses[:12],
        "gaps": _gaps(candidates, episode_rows),
    }


def _research_event(row: dict[str, Any]) -> dict[str, Any] | None:
    screen = row.get("screen") or {}
    scene = row.get("scene") or {}
    app = screen.get("frontmost_app")
    label = scene.get("label") or "unknown"
    if app not in RESEARCH_APPS and label not in RESEARCH_SCENES:
        return None
    if screen.get("sensitive_scene") or label == "sensitive":
        return None
    if float(screen.get("frame_age_sec") or 0.0) > 60:
        return None
    ts = _iso_to_unix(row.get("ts"))
    if ts is None:
        return None
    text = str(screen.get("ocr_snippet") or "")
    domains = _domains(text)
    queries = _queries(text)
    terms = _terms(text)
    if not domains and not queries and len(terms) < 2:
        return None
    return {
        "candidate_id": row.get("candidate_id"),
        "ts": row.get("ts"),
        "ts_unix": ts,
        "app": app,
        "scene": label,
        "domains": domains,
        "queries": queries,
        "terms": terms,
        "fingerprint": _fingerprint(domains, queries, terms),
    }


def _segment(events: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    episodes: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for event in events:
        if not current:
            current = [event]
            continue
        gap = float(event["ts_unix"]) - float(current[-1]["ts_unix"])
        if gap > EPISODE_GAP_SEC:
            episodes.append(current)
            current = [event]
            continue
        current.append(event)
    if current:
        episodes.append(current)
    return episodes


def _episode_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    domains = Counter()
    queries: list[str] = []
    terms = Counter()
    apps = Counter()
    scenes = Counter()
    duration_sec = 0.0
    for idx, event in enumerate(events):
        domains.update(event["domains"])
        terms.update(event["terms"])
        apps[event.get("app") or "unknown"] += 1
        scenes[event.get("scene") or "unknown"] += 1
        for query in event["queries"]:
            if query not in queries:
                queries.append(query)
        if idx + 1 < len(events):
            delta = max(0.0, float(events[idx + 1]["ts_unix"]) - float(event["ts_unix"]))
            duration_sec += min(delta, MAX_EVENT_DELTA_SEC)
    top_terms = [term for term, _ in terms.most_common(8)]
    query_terms = _terms(" ".join(queries))
    if query_terms:
        merged = query_terms + [term for term in top_terms if term not in set(query_terms)]
        top_terms = merged[:8]
    patterns = _patterns(events, domains, queries, duration_sec)
    return {
        "episode_id": f"diet_{events[0]['candidate_id'] or int(events[0]['ts_unix'])}",
        "ts_start": events[0]["ts"],
        "ts_end": events[-1]["ts"],
        "observed_duration_min": round(duration_sec / 60.0, 2),
        "n_events": len(events),
        "apps": dict(apps.most_common(4)),
        "scenes": dict(scenes.most_common(4)),
        "source_domains": [domain for domain, _ in domains.most_common(8)],
        "query_candidates": queries[:6],
        "top_terms": top_terms,
        "workflow_patterns": patterns,
        "task_hypothesis": _task_hypothesis(top_terms, domains),
        "evidence_candidate_ids": [event["candidate_id"] for event in events if event.get("candidate_id")][-8:],
    }


def _patterns(
    events: list[dict[str, Any]],
    domains: Counter[str],
    queries: list[str],
    duration_sec: float,
) -> list[str]:
    out: list[str] = []
    if len(queries) >= 2:
        out.append("query_reformulation")
    if len(domains) >= 3 and duration_sec / max(len(domains), 1) < 180:
        out.append("source_triage")
    if duration_sec >= 8 * 60 and len(domains) <= 2:
        out.append("deep_reading")
    if any(_is_primary_source(domain) for domain in domains):
        out.append("primary_source_preference")
    if len(events) >= 5 and duration_sec < 3 * 60:
        out.append("rapid_scan")
    return out or ["ambient_reading"]


def _skill_hypotheses(episodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_topic: dict[str, list[dict[str, Any]]] = {}
    for episode in episodes:
        topic = " ".join(episode.get("top_terms") or [])[:80] or "general research"
        key = " ".join(topic.split()[:3]) or "general research"
        by_topic.setdefault(key, []).append(episode)

    rows: list[dict[str, Any]] = []
    for topic, eps in by_topic.items():
        patterns = Counter()
        domains = Counter()
        evidence: list[str] = []
        duration = 0.0
        for ep in eps:
            patterns.update(ep.get("workflow_patterns") or [])
            domains.update(ep.get("source_domains") or [])
            evidence.extend(ep.get("evidence_candidate_ids") or [])
            duration += float(ep.get("observed_duration_min") or 0.0)
        if not evidence:
            continue
        rows.append({
            "topic": topic,
            "hypothesis": _hypothesis_text(topic, patterns, domains),
            "confidence": _confidence(len(eps), duration, patterns),
            "observed_episode_count": len(eps),
            "observed_duration_min": round(duration, 2),
            "patterns": dict(patterns.most_common(5)),
            "domains": [domain for domain, _ in domains.most_common(5)],
            "evidence_candidate_ids": evidence[-10:],
        })
    return sorted(rows, key=lambda row: (row["confidence"], row["observed_duration_min"]), reverse=True)


def _hypothesis_text(topic: str, patterns: Counter[str], domains: Counter[str]) -> str:
    dominant = patterns.most_common(1)[0][0] if patterns else "ambient_reading"
    source = domains.most_common(1)[0][0] if domains else "visible sources"
    if dominant == "query_reformulation":
        return f"When exploring {topic}, you refine the question through multiple searches before settling on sources like {source}."
    if dominant == "source_triage":
        return f"When exploring {topic}, you scan several sources quickly and appear to select based on relevance and authority."
    if dominant == "deep_reading":
        return f"When exploring {topic}, you dwell on a small set of sources long enough to extract working context."
    if dominant == "primary_source_preference":
        return f"When exploring {topic}, you appear to prefer primary or implementation-adjacent sources such as {source}."
    return f"When exploring {topic}, you gather context from {source} before returning to the work surface."


def _confidence(n_episodes: int, duration_min: float, patterns: Counter[str]) -> float:
    score = 0.25 + min(0.3, n_episodes * 0.08) + min(0.25, duration_min / 60.0)
    if patterns:
        score += min(0.2, sum(patterns.values()) * 0.03)
    return round(max(0.05, min(0.9, score)), 2)


def _gaps(candidates: list[dict[str, Any]], episodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "name": "browser_url_ground_truth",
            "status": "missing",
            "detail": "The report infers domains/queries from OCR. A direct Fisherman URL/title field would make source attribution and dwell measurement much stronger.",
        },
        {
            "name": "downstream_artifact_link",
            "status": "partial" if episodes else "missing",
            "detail": "The report detects reading episodes, but does not yet prove which draft, commit, note, or message consumed that research.",
        },
        {
            "name": "raw_capture_coverage",
            "status": "pass" if candidates else "missing",
            "detail": f"{len(candidates)} candidate frames available in this window.",
        },
    ]


def _domains(text: str) -> list[str]:
    found: set[str] = set()
    for match in re.finditer(r"https?://[^\s<>()\"']+", text, flags=re.IGNORECASE):
        host = urlparse(match.group(0)).netloc.lower()
        if _valid_domain(host):
            found.add(host.removeprefix("www."))
    for match in re.finditer(r"\b([a-z0-9-]+(?:\.[a-z0-9-]+)+)\b", text, flags=re.IGNORECASE):
        host = match.group(1).lower().removeprefix("www.")
        if _valid_domain(host):
            found.add(host)
    return sorted(found)[:8]


def _valid_domain(host: str) -> bool:
    host = host.lower().strip().removeprefix("www.")
    if not host or "." not in host:
        return False
    if host.endswith((".local", ".app")):
        return False
    labels = host.split(".")
    tld = labels[-1]
    if tld not in COMMON_TLDS:
        return False
    if all(label.isdigit() for label in labels[:-1]):
        return False
    if len(labels) == 2 and labels[0].isdigit():
        return False
    if all(len(label) <= 2 for label in labels):
        return False
    return True


def _queries(text: str) -> list[str]:
    out: list[str] = []
    for match in re.finditer(r"[?&]q=([^&\s]+)", text, flags=re.IGNORECASE):
        query = unquote(match.group(1)).replace("+", " ").strip()
        if query:
            out.append(_compact_phrase(query))
    for line in text.splitlines():
        lower = line.lower()
        if "google search" in lower or "search" == lower.strip():
            continue
        if 8 <= len(line) <= 90 and any(marker in lower for marker in (" - google search", "search results", "site:")):
            out.append(_compact_phrase(line))
    return _dedupe(out)[:6]


def _terms(text: str) -> list[str]:
    words = [
        word.lower()
        for word in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", text)
        if (
            word.lower() not in STOPWORDS
            and not _looks_secretish(word)
            and not _looks_ocr_noise(word)
        )
    ]
    return [word for word, _ in Counter(words).most_common(16)]


def _fingerprint(domains: list[str], queries: list[str], terms: list[str]) -> str:
    basis = domains[:2] or queries[:1] or terms[:4]
    return "|".join(basis)


def _task_hypothesis(terms: list[str], domains: Counter[str]) -> str:
    topic = " ".join(terms[:4]) if terms else "current research thread"
    domain = domains.most_common(1)[0][0] if domains else "visible sources"
    return f"{topic} via {domain}"


def _is_primary_source(domain: str) -> bool:
    return (
        "github.com" in domain
        or domain.endswith(".edu")
        or "docs." in domain
        or domain.startswith("docs.")
        or "arxiv.org" in domain
        or "pubmed.ncbi.nlm.nih.gov" in domain
    )


def _looks_secretish(word: str) -> bool:
    return len(word) > 28 and bool(re.search(r"[A-Z]", word)) and bool(re.search(r"\d", word))


def _looks_ocr_noise(word: str) -> bool:
    lower = word.lower()
    if lower.startswith("v") and lower[1:].isdigit():
        return True
    if "--" in lower:
        return True
    if len(lower) > 18 and "-" in lower:
        return True
    if len(lower) > 12:
        digits = sum(1 for ch in lower if ch.isdigit())
        if digits / max(len(lower), 1) > 0.25:
            return True
    return False


def _compact_phrase(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text[:90]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _iso_to_unix(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return float(calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")))
    except Exception:
        return None


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
