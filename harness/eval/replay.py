"""Shadow-policy replay.

Loads frozen CandidateEvents (from candidates.jsonl or a dogfood dataset),
runs a chosen policy against each, writes predictions to a JSON report.

Usage:
    python -m eval.replay --policy rule_v1 \\
        --dataset ~/.harness/candidates.jsonl \\
        --out reports/rule_v1.json [--since 7d]

The replay is INPUT-ONLY: it does not call the LLM, the critic, or push.
Use eval/score.py to compute metrics from the predictions.
"""

from __future__ import annotations

import argparse
import calendar
import importlib
import json
import os
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional


def _parse_since(spec: Optional[str]) -> Optional[str]:
    if not spec:
        return None
    if spec.endswith(("s", "m", "h", "d")):
        unit_map = {"s": 1, "m": 60, "h": 3600, "d": 86400}
        try:
            n = int(spec[:-1])
        except ValueError:
            return None
        secs = unit_map[spec[-1]] * n
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - secs))
    return spec


def _load_policy(name: str):
    try:
        mod = importlib.import_module(f"policies.{name}")
    except ImportError as e:
        raise SystemExit(f"policy {name!r} not found: {e}")
    if not hasattr(mod, "decide"):
        raise SystemExit(f"policy {name!r} has no decide() function")
    return mod.decide


def _load_dataset(path: Path, since_iso: Optional[str]) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"dataset not found: {path}")
    rows: list[dict] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = row.get("ts", "")
            if since_iso and ts < since_iso:
                continue
            rows.append(row)
    return rows


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _load_live_payloads(table: str, path: Path, filename: str, since_iso: Optional[str] = None) -> list[dict] | None:
    """Use the SQLite sidecar for default live logs when available."""
    if path.expanduser() != Path(os.path.expanduser(f"~/.harness/{filename}")):
        return None
    try:
        from harness import sql_store

        if not sql_store.db_path().exists() or sql_store.count_rows(table) <= 0:
            return None
        return sql_store.payload_rows(table, since_iso=since_iso)
    except Exception:
        return None


def _iso_to_unix(ts: str | None) -> Optional[float]:
    if not ts:
        return None
    try:
        return float(calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")))
    except Exception:
        return None


def _to_event(row: dict):
    from harness.schemas import (
        CandidateEvent,
        ContextSignals,
        ScreenContext,
        SceneTag,
        UserPref,
    )

    screen_d = row.get("screen") or {}
    scene_d = row.get("scene") or {}
    ctx_d = row.get("context") or {}
    pref_d = row.get("user_pref") or {}

    event = CandidateEvent(
        candidate_id=row.get("candidate_id", ""),
        ts=row.get("ts", ""),
        screen=ScreenContext(
            active=screen_d.get("active", True),
            frontmost_app=screen_d.get("frontmost_app"),
            bundle_id=screen_d.get("bundle_id"),
            window_title=screen_d.get("window_title"),
            ocr_snippet=screen_d.get("ocr_snippet", ""),
            capture_ts_unix=screen_d.get("capture_ts_unix"),
            capture_gap_sec=float(screen_d.get("capture_gap_sec", 0.0) or 0.0),
            frame_age_sec=float(screen_d.get("frame_age_sec", 0.0)),
            sensitive_scene=screen_d.get("sensitive_scene", False),
        ),
        scene=SceneTag(
            label=scene_d.get("label", "unknown"),
            strength=scene_d.get("strength", "unknown"),
            source=scene_d.get("source", "unknown"),
            confidence=float(scene_d.get("confidence", 0.0)),
            specificity=scene_d.get("specificity"),
            intent_signals=scene_d.get("intent_signals") or {},
            load_bearing_text=scene_d.get("load_bearing_text"),
        ),
        context=ContextSignals(
            in_call=ctx_d.get("in_call", False),
            on_battery=ctx_d.get("on_battery", False),
            minutes_since_last_push=float(ctx_d.get("minutes_since_last_push", 9999.0)),
            minutes_since_last_user_action=float(ctx_d.get("minutes_since_last_user_action", 0.0)),
        ),
        user_pref=UserPref(
            frequency=pref_d.get("frequency", "medium"),
            allowed_intents=pref_d.get("allowed_intents", []),
            quiet_hours=tuple(pref_d.get("quiet_hours", (22, 8))),
            snoozed_until=pref_d.get("snoozed_until"),
            muted_intents=pref_d.get("muted_intents", []),
        ),
    )
    return event


def _trim_window(events: deque[tuple[Any, float]], cutoff_ts: float) -> None:
    while events:
        if events[0][1] >= cutoff_ts:
            break
        events.popleft()


def _memory_for_windows(
    recent_2h: list[tuple[Any, float]],
    recent_15m: list[tuple[Any, float]],
    now_ts: float | None,
):
    from harness.schemas import MemorySnapshot

    if not recent_2h:
        return MemorySnapshot.build([], [], [], 0, 0.0)

    valid_2h = [(event, ts) for event, ts in recent_2h if _is_valid_work_event(event)]
    valid_15m = [(event, ts) for event, ts in recent_15m if _is_valid_work_event(event)]

    switches = 0
    prev = None
    for event, _ in valid_15m:
        app = event.screen.frontmost_app
        if prev is not None and app != prev:
            switches += 1
        prev = app

    latest_capture_gap = float(getattr(recent_2h[-1][0].screen, "capture_gap_sec", 0.0) or 0.0)
    if not valid_2h or not _is_valid_work_event(recent_2h[-1][0]) or latest_capture_gap > 90:
        return MemorySnapshot.build(
            recent_apps=[event.screen.frontmost_app or "" for event, _ in valid_2h[-30:]],
            recent_scenes=[event.scene.label for event, _ in valid_2h[-30:]],
            recent_outcomes=[],
            app_switches_last_15m=switches,
            minutes_on_current_app=0.0,
            last_event_gap_sec=_last_gap_sec(recent_2h),
            session_boundary=_session_boundary(recent_2h),
        )

    current_app = recent_2h[-1][0].screen.frontmost_app
    start_ts = now_ts or recent_2h[-1][1]
    last_ts = start_ts
    for event, event_ts in reversed(recent_2h[:-1]):
        if last_ts - event_ts > 90:
            break
        if not _is_valid_work_event(event):
            break
        if event.screen.frontmost_app != current_app:
            break
        if float(getattr(event.screen, "capture_gap_sec", 0.0) or 0.0) > 90:
            break
        start_ts = event_ts
        last_ts = event_ts

    now_ts = now_ts or start_ts
    return MemorySnapshot.build(
        recent_apps=[event.screen.frontmost_app or "" for event, _ in valid_2h[-30:]],
        recent_scenes=[event.scene.label for event, _ in valid_2h[-30:]],
        recent_outcomes=[],
        app_switches_last_15m=switches,
        minutes_on_current_app=max(0.0, (now_ts - start_ts) / 60.0),
        last_event_gap_sec=_last_gap_sec(recent_2h),
        session_boundary=_session_boundary(recent_2h),
    )


def _is_valid_work_event(event: Any) -> bool:
    return (
        bool(event.screen.active)
        and not bool(event.screen.sensitive_scene)
        and event.scene.label != "sensitive"
        and float(event.screen.frame_age_sec or 0.0) <= 60
    )


def _last_gap_sec(recent: list[tuple[Any, float]]) -> float:
    if len(recent) < 2:
        return 0.0
    return max(0.0, recent[-1][1] - recent[-2][1])


def _session_boundary(recent: list[tuple[Any, float]]) -> str | None:
    if not recent:
        return None
    if float(getattr(recent[-1][0].screen, "capture_gap_sec", 0.0) or 0.0) > 90:
        return "capture_gap"
    if _last_gap_sec(recent) > 90:
        return "idle_gap"
    return None


def _recent_outcomes_for(outcomes: list[dict], event_ts_iso: str, n: int = 5) -> list[dict]:
    event_ts = _iso_to_unix(event_ts_iso)
    if event_ts is None:
        return []
    eligible = []
    for outcome in outcomes:
        outcome_ts = _iso_to_unix(outcome.get("ts"))
        if outcome_ts is not None and outcome_ts <= event_ts:
            eligible.append(outcome)
    return eligible[-n:]


def _live_gate_config() -> dict:
    cfg = {
        "cooldown_min": 5,
        "negative_feedback_backoff_min": 15,
        "resume_suppression_sec": 90,
        "quiet_hours_start": 22,
        "quiet_hours_end": 8,
        "allowed_intents": [
            "focus_nudge",
            "offer_research",
            "surface_open_thread",
            "summarize_session",
        ],
        "daily_goal": "",
        "sensitivity": "balanced",
    }
    config_path = Path(os.path.expanduser("~/.harness/config.toml"))
    if config_path.exists():
        try:
            import tomllib
            with open(config_path, "rb") as f:
                live = tomllib.load(f)
            gate = live.get("gate") or {}
            intents = live.get("intents") or {}
            cfg.update({
                "cooldown_min": gate.get("cooldown_min", cfg["cooldown_min"]),
                "negative_feedback_backoff_min": gate.get(
                    "negative_feedback_backoff_min",
                    cfg["negative_feedback_backoff_min"],
                ),
                "resume_suppression_sec": gate.get(
                    "resume_suppression_sec",
                    cfg.get("resume_suppression_sec", 90),
                ),
                "quiet_hours_start": gate.get("quiet_hours_start", cfg["quiet_hours_start"]),
                "quiet_hours_end": gate.get("quiet_hours_end", cfg["quiet_hours_end"]),
                "allowed_intents": intents.get("enabled", cfg["allowed_intents"]),
            })
        except Exception:
            pass
    state_path = Path(os.path.expanduser("~/.harness/policy.json"))
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
            cfg["daily_goal"] = (state.get("daily_goal") or "").strip()
            cfg["sensitivity"] = state.get("sensitivity") or cfg["sensitivity"]
        except Exception:
            pass
    return cfg


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay a policy over frozen candidates.")
    parser.add_argument("--policy", required=True, help="Policy module name in policies/ (e.g., rule_v0).")
    parser.add_argument(
        "--dataset",
        default=os.path.expanduser("~/.harness/candidates.jsonl"),
        help="Path to candidate jsonl (default: live candidates.jsonl).",
    )
    parser.add_argument("--since", default=None, help="Duration like 7d / 24h, or ISO timestamp.")
    parser.add_argument("--out", required=True, help="Output predictions JSON path.")
    parser.add_argument(
        "--outcomes",
        default=os.path.expanduser("~/.harness/outcomes.jsonl"),
        help="Outcome jsonl to replay recent feedback gates against.",
    )
    parser.add_argument("--config-overrides", default=None, help="JSON dict to merge into gate config.")
    args = parser.parse_args()

    decide = _load_policy(args.policy)
    since_iso = _parse_since(args.since)
    dataset_path = Path(args.dataset)
    dataset = _load_live_payloads("candidates", dataset_path, "candidates.jsonl", since_iso)
    if dataset is None:
        dataset = _load_dataset(dataset_path, since_iso)

    outcomes_path = Path(args.outcomes)
    outcomes = _load_live_payloads("outcomes", outcomes_path, "outcomes.jsonl")
    if outcomes is None:
        outcomes = _load_jsonl(outcomes_path)
    cfg = _live_gate_config()
    if args.config_overrides:
        cfg.update(json.loads(args.config_overrides))

    predictions: list[dict] = []
    recent_2h: deque[tuple[Any, float]] = deque()
    recent_15m: deque[tuple[Any, float]] = deque()
    simulated_last_push_ts: Optional[float] = None
    for row in dataset:
        event = _to_event(row)
        event_ts = _iso_to_unix(event.ts)
        if event_ts is not None:
            event.context.minutes_since_last_push = (
                9999.0
                if simulated_last_push_ts is None
                else max(0.0, (event_ts - simulated_last_push_ts) / 60.0)
            )
            _trim_window(recent_2h, event_ts - 2 * 60 * 60)
            _trim_window(recent_15m, event_ts - 15 * 60)
        event_ts_for_window = event_ts if event_ts is not None else time.time()
        recent_2h.append((event, event_ts_for_window))
        recent_15m.append((event, event_ts_for_window))
        mem = _memory_for_windows(list(recent_2h), list(recent_15m), event_ts)
        recent_outcomes = _recent_outcomes_for(outcomes, event.ts)
        decision = decide(event, mem, recent_outcomes, cfg)
        if decision.action == "notch_ping" and event_ts is not None:
            simulated_last_push_ts = event_ts
        predictions.append(
            {
                "candidate_id": row.get("candidate_id"),
                "ts": row.get("ts"),
                "scene": row.get("scene", {}).get("label"),
                "frontmost_app": row.get("screen", {}).get("frontmost_app"),
                "decision": {
                    "action": decision.action,
                    "intent": decision.intent,
                    "reason_codes": decision.reason_codes,
                    "confidence": decision.confidence,
                    "propensity": decision.propensity,
                    "policy_version": decision.policy_version,
                },
            }
        )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(
            {
                "policy": args.policy,
                "dataset": args.dataset,
                "since": since_iso,
                "gate_config": cfg,
                "n_candidates": len(dataset),
                "n_pings": sum(1 for p in predictions if p["decision"]["action"] == "notch_ping"),
                "predictions": predictions,
            },
            f,
            indent=2,
            default=str,
        )

    pings = sum(1 for p in predictions if p["decision"]["action"] == "notch_ping")
    print(f"replayed {len(dataset)} candidates → {pings} pings ({pings/max(len(dataset),1):.1%})")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
