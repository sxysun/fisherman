"""Score predictions against outcomes and retro labels.

Loads:
- predictions.json (from eval/replay.py)
- ~/.harness/outcomes.jsonl (live outcomes — what users actually did)
- ~/.harness/retro_labels.jsonl (from `harness label` — retrospective labels)

Computes:
- n_pings / day
- false_int_rate         (pings whose outcome was dismissed/muted, or retro labeled would_annoy)
- precision_when_pinged  (pings welcomed / total pings with any signal)
- recall_at_strong       (of retro-labeled "would have helped" no_pings, how many would current policy ping?)
- total_reward           (signal-derived per harness/reward.py — replaces the old ad-hoc weighted-sum)
- agreement_rate         (predictions == actual decisions, if available)
- per_intent breakdown
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


DEFAULT_WEIGHTS = {
    "welcomed": 3.0,
    "helpful": 2.0,
    "annoying": -5.0,
    "privacy": -8.0,
    "duplicate": -1.0,
}


def _import_reward_module():
    """Lazy import so eval/score.py can run standalone."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from harness import reward as r
    return r


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


def _load_predictions(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def score(
    predictions_doc: dict,
    outcomes: list[dict],
    retro_labels: list[dict],
    weights: dict[str, float] = None,
) -> dict:
    weights = weights or DEFAULT_WEIGHTS
    preds = predictions_doc["predictions"]

    # Index outcomes & retro labels by candidate_id (via decision_id stem) or by ts
    outcomes_by_decision: dict[str, dict] = {}
    for o in outcomes:
        did = o.get("decision_id")
        if did:
            outcomes_by_decision[did] = o

    retro_by_candidate: dict[str, dict] = {}
    for r in retro_labels:
        cid = r.get("candidate_id")
        if cid:
            retro_by_candidate[cid] = r

    n_total = len(preds)
    n_pings = sum(1 for p in preds if p["decision"]["action"] == "notch_ping")
    n_no_pings = n_total - n_pings

    welcomed = 0
    annoying = 0
    privacy = 0
    duplicate = 0
    pings_with_signal = 0
    pings_clicked = 0
    pings_dismissed_or_timed_out = 0

    per_intent_pings: dict[str, int] = defaultdict(int)
    per_intent_welcomed: dict[str, int] = defaultdict(int)

    # Live outcome signal — only available for pings that actually fired in production
    # (replay doesn't fire pings; this section uses the live decisions log).
    for p in preds:
        if p["decision"]["action"] != "notch_ping":
            continue
        cid = p.get("candidate_id")
        # decision_id derived: see policies/rule_v0.py — pd_<hex>
        guessed_did = "pd_" + (cid.split("_", 1)[-1] if cid else "")
        outcome = outcomes_by_decision.get(guessed_did)
        intent = p["decision"]["intent"] or "(none)"
        per_intent_pings[intent] += 1
        if outcome is None:
            continue
        pings_with_signal += 1
        action = outcome.get("user_action", "")
        if action == "clicked":
            welcomed += 1
            pings_clicked += 1
            per_intent_welcomed[intent] += 1
        elif action in ("dismissed", "muted"):
            annoying += 1
            pings_dismissed_or_timed_out += 1
        elif action == "timed_out":
            # ambiguous — count partial annoying penalty
            annoying += 0.3
            pings_dismissed_or_timed_out += 1

    # Retro-label signal — fills in no_ping evaluation
    retro_would_help_no_pings = 0
    retro_would_help_total = 0
    retro_good_no_ping = 0
    retro_would_annoy_pings = 0
    cant_tell = 0
    for p in preds:
        cid = p.get("candidate_id")
        r = retro_by_candidate.get(cid)
        if not r:
            continue
        label = r.get("label")
        conf = float(r.get("confidence", 1.0))
        action = p["decision"]["action"]
        if label == "would_help":
            retro_would_help_total += 1
            if action == "no_ping":
                retro_would_help_no_pings += 1
        elif label == "good_no_ping":
            if action == "no_ping":
                retro_good_no_ping += 1
        elif label == "would_annoy":
            if action == "notch_ping":
                retro_would_annoy_pings += 1
                annoying += conf  # weight retro labels by confidence
        elif label == "cant_tell":
            cant_tell += 1

    # Cost-weighted utility (legacy ad-hoc weights). Kept for back-compat
    # so older traces remain comparable.
    utility = (
        weights["welcomed"] * welcomed
        + weights["annoying"] * annoying
        + weights["privacy"] * privacy
        + weights["duplicate"] * duplicate
    )

    # Signal-derived reward (the new, principled metric — replaces utility going forward).
    try:
        reward_mod = _import_reward_module()
        reward_agg = reward_mod.aggregate_rewards(outcomes)
    except Exception as e:
        reward_agg = {"error": str(e)}

    precision = (welcomed / pings_with_signal) if pings_with_signal else None
    false_int_rate = (
        (pings_dismissed_or_timed_out / pings_with_signal) if pings_with_signal else None
    )
    recall_at_strong = (
        (retro_would_help_total - retro_would_help_no_pings) / max(retro_would_help_total, 1)
        if retro_would_help_total
        else None
    )

    report = {
        "policy": predictions_doc.get("policy"),
        "n_candidates": n_total,
        "n_pings": n_pings,
        "ping_rate": n_pings / max(n_total, 1),
        "n_no_pings": n_no_pings,
        "pings_with_outcome_signal": pings_with_signal,
        "pings_clicked": pings_clicked,
        "pings_dismissed_or_timed_out": pings_dismissed_or_timed_out,
        "retro_would_help_total": retro_would_help_total,
        "retro_would_help_missed_by_policy": retro_would_help_no_pings,
        "retro_good_no_ping": retro_good_no_ping,
        "retro_would_annoy_pings": retro_would_annoy_pings,
        "retro_cant_tell": cant_tell,
        "precision_when_pinged": precision,
        "false_int_rate": false_int_rate,
        "recall_at_strong": recall_at_strong,
        "cost_weighted_utility": utility,            # legacy reward_v1
        "reward_v2_signal_derived": reward_agg,      # principled, signal-based
        "per_intent": {
            k: {"pings": per_intent_pings[k], "welcomed": per_intent_welcomed[k]}
            for k in per_intent_pings
        },
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Score replay predictions vs outcomes + retro labels.")
    parser.add_argument("--predictions", required=True, help="Path to predictions JSON from replay.py")
    parser.add_argument(
        "--outcomes",
        default=os.path.expanduser("~/.harness/outcomes.jsonl"),
    )
    parser.add_argument(
        "--retro",
        default=os.path.expanduser("~/.harness/retro_labels.jsonl"),
    )
    parser.add_argument("--out", default=None, help="Where to write the report JSON. Stdout if omitted.")
    args = parser.parse_args()

    preds = _load_predictions(Path(args.predictions))
    outcomes = _load_jsonl(Path(args.outcomes))
    retro = _load_jsonl(Path(args.retro))

    report = score(preds, outcomes, retro)
    serialized = json.dumps(report, indent=2)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(serialized)
        print(f"wrote {args.out}")
    else:
        print(serialized)
    return 0


if __name__ == "__main__":
    sys.exit(main())
