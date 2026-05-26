from __future__ import annotations

import re
import time
from collections import defaultdict
from typing import Any

from . import implicit as implicit_mod
from . import metrics as metrics_mod
from . import app_identity, privacy
from .store import iter_jsonl


KG_PRIOR_VERSION = "kg_priors_v1"
_CACHE: tuple[float, dict[str, Any]] | None = None
_CACHE_TTL_SEC = 60.0


def build_priors(window: str = "30d", *, min_count: float = 0.5) -> dict[str, Any]:
    """Build interpretable local timing priors from labels and live outcomes."""
    since = metrics_mod.since_iso(window)
    candidates = [row for row in iter_jsonl("candidates.jsonl") if row.get("ts", "") >= since]
    candidate_by_id = {row.get("candidate_id"): row for row in candidates if row.get("candidate_id")}
    decisions = [row for row in iter_jsonl("decisions.jsonl") if row.get("ts", "") >= since]
    decisions_by_id = {row.get("decision_id"): row for row in decisions if row.get("decision_id")}
    decisions_by_candidate = {row.get("candidate_id"): row for row in decisions if row.get("candidate_id")}
    labels = metrics_mod.latest_label_rows([
        row for row in iter_jsonl("retro_labels.jsonl") if row.get("ts", "") >= since
    ])
    outcomes = [row for row in iter_jsonl("outcomes.jsonl") if row.get("ts", "") >= since]
    weak = [
        row for row in implicit_mod.weak_labels_from_outcomes(outcomes, decisions_by_id)
        if row.get("usable_for_training")
    ]
    excluded = _excluded_ids()

    stats: dict[str, dict[str, float]] = defaultdict(lambda: {"pos": 0.0, "neg": 0.0, "n": 0.0})
    examples = 0

    for label in [*labels, *weak]:
        target = _target(label.get("label"))
        if target is None:
            continue
        decision = (
            decisions_by_id.get(label.get("decision_id") or "")
            or decisions_by_candidate.get(label.get("candidate_id") or "")
            or {}
        )
        if not decision:
            continue
        candidate_id = decision.get("candidate_id") or label.get("candidate_id")
        if _is_excluded(excluded, decision.get("decision_id"), candidate_id, decision.get("workflow_event_id")):
            continue
        candidate = candidate_by_id.get(candidate_id or "") or {}
        if not candidate:
            continue
        weight = float(label.get("confidence") or 1.0)
        weight = max(0.1, min(1.0, weight))
        for feature in _features(candidate):
            bucket = stats[feature]
            bucket["n"] += weight
            if target == "notch_ping":
                bucket["pos"] += weight
            else:
                bucket["neg"] += weight
        examples += 1

    features = []
    for feature, row in stats.items():
        n = row["n"]
        if n < min_count:
            continue
        pos = row["pos"]
        # Jeffreys-style smoothing avoids false certainty from tiny samples.
        p_should_ping = (pos + 0.5) / (n + 1.0)
        features.append({
            "feature": feature,
            "p_should_ping": round(p_should_ping, 3),
            "n": round(n, 3),
            "pos": round(pos, 3),
            "neg": round(row["neg"], 3),
        })
    features.sort(key=lambda row: (row["n"], abs(row["p_should_ping"] - 0.5)), reverse=True)
    return {
        "version": KG_PRIOR_VERSION,
        "window": window,
        "generated_at": _now_iso(),
        "n_examples": examples,
        "features": features[:200],
    }


def priors_for_event(
    event: Any,
    *,
    window: str = "30d",
    max_items: int = 8,
) -> dict[str, Any]:
    priors = _cached_priors(window)
    feature_rows = {row.get("feature"): row for row in priors.get("features") or []}
    candidate = event.to_dict() if hasattr(event, "to_dict") else dict(event or {})
    matches = []
    for feature in _features(candidate):
        row = feature_rows.get(feature)
        if row:
            matches.append(row)
    matches.sort(key=lambda row: (row.get("n") or 0, abs((row.get("p_should_ping") or 0.5) - 0.5)), reverse=True)
    return {
        "version": KG_PRIOR_VERSION,
        "window": window,
        "n_examples": priors.get("n_examples", 0),
        "matches": matches[:max(0, max_items)],
    }


def _cached_priors(window: str) -> dict[str, Any]:
    global _CACHE
    now = time.time()
    if _CACHE is not None:
        ts, payload = _CACHE
        if now - ts < _CACHE_TTL_SEC and payload.get("window") == window:
            return payload
    payload = build_priors(window)
    _CACHE = (now, payload)
    return payload


def _target(label: str | None) -> str | None:
    if label == "would_help":
        return "notch_ping"
    if label in {"would_annoy", "good_no_ping"}:
        return "no_ping"
    return None


def _features(candidate: dict) -> list[str]:
    screen = candidate.get("screen") or {}
    scene = candidate.get("scene") or {}
    app = _norm(app_identity.effective_app_from_candidate_dict(candidate) or screen.get("bundle_id") or "")
    label = _norm(scene.get("label") or "")
    features: list[str] = []
    if app:
        features.append(f"app:{app}")
    if label:
        features.append(f"scene:{label}")
    if app and label:
        features.append(f"app_scene:{app}|{label}")
    text = " ".join([
        str(screen.get("window_title") or ""),
        str(scene.get("specificity") or ""),
        str(screen.get("ocr_snippet") or "")[:500],
    ])
    for kw in _keywords(text)[:8]:
        features.append(f"kw:{kw}")
        if app:
            features.append(f"app_kw:{app}|{kw}")
    return list(dict.fromkeys(features))


def _keywords(text: str) -> list[str]:
    redacted = privacy.redact_text(text or "").lower()
    tokens = re.findall(r"[a-z][a-z0-9_+-]{3,}", redacted)
    stop = {
        "this", "that", "with", "from", "have", "your", "what", "when", "where",
        "would", "could", "should", "there", "their", "about", "into", "using",
        "http", "https", "com", "localhost",
    }
    out = [tok[:40] for tok in tokens if tok not in stop and not tok.startswith("redacted")]
    return list(dict.fromkeys(out))


def _excluded_ids() -> set[tuple[str, str]]:
    excluded: set[tuple[str, str]] = set()
    for row in iter_jsonl("curation.jsonl"):
        action = str(row.get("action") or "")
        if action not in {"exclude", "delete", "blur"}:
            continue
        target_type = str(row.get("target_type") or "")
        target_id = str(row.get("target_id") or "")
        if target_type and target_id:
            excluded.add((target_type, target_id))
    return excluded


def _is_excluded(
    excluded: set[tuple[str, str]],
    decision_id: object,
    candidate_id: object,
    workflow_event_id: object,
) -> bool:
    return any((
        ("decision", str(decision_id)) in excluded if decision_id else False,
        ("candidate", str(candidate_id)) in excluded if candidate_id else False,
        ("workflow_event", str(workflow_event_id)) in excluded if workflow_event_id else False,
    ))


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())[:80]


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
