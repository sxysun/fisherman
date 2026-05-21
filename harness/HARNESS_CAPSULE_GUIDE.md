# Harness Capsule Guide

This document explains the floating Harness capsule UI, especially the **Pipeline and eval** panel shown when the capsule expands.

## What The Capsule Is

The Harness capsule is a separate macOS surface from Fisherman. Fisherman owns the main notch. Harness uses a small floating capsule so the harness can run at the same time without fighting for the same top-center notch slot.

The capsule is backed by the local harness daemon at `http://127.0.0.1:7893`. It does not compute the metrics itself; it fetches reports from daemon endpoints:

- `GET /dashboard/data?window=24h`
- `GET /eval/report?window=24h&max_examples=4`
- `GET /next-steps/report?window=24h&max_examples=4`
- `GET /information-diet/report?window=24h&max_episodes=6`

## Capsule Controls

- Hover the compact capsule to expand it.
- Drag the compact capsule, or the `Harness` title area in expanded mode, to move it.
- Release after dragging to snap it to the nearest screen edge.
- Click the pin icon to keep it open.
- Click the refresh icon inside a panel to refetch that report.
- Use `Pipeline` and `Diet` tabs to switch views.
- When a live notification is pending, a `Ping` tab appears with `Yes`, `Later`, and dismiss controls.

## Why It Sometimes Shows Loading

The panel shows `loading...` while the Swift app is waiting for daemon reports. The slowest part is usually the eval report because it joins decisions, outcomes, delivery claims, implicit labels, traces, and next-step prediction residuals.

The current UI keeps the expanded view alive behind the compact capsule so it can cache results across hover open/close cycles. A normal hover should reuse already-fetched data after the first load. It may still show `loading...` after the harness app restarts, when switching to a tab for the first time, or after clicking refresh.

## Pipeline Row

The first row shows the end-to-end intervention pipeline over the current 24-hour window.

| Card | Meaning | Source |
| --- | --- | --- |
| `Observe` | Number of screen-derived candidate moments the harness considered. A candidate is a snapshot/context event from Fisherman plus OCR/scene metadata. | `/dashboard/data`, `n_candidates` |
| `Gate` | Number of policy decisions made for candidates. This includes both pings and non-pings. | `/dashboard/data`, `n_decisions` |
| `Ping` | Number of decisions where policy said a notification was warranted. This is the “eligible to interrupt” count. | `/eval/report`, `data.n_pings` |
| `Claim` | Number of pings actually claimed/displayed by the Swift capsule app. This separates “daemon wanted to notify” from “UI actually showed it.” | `/eval/report`, `data.n_claimed_pings` |
| `Outcome` | Number of reaction rows captured after pings, including clicks, dismisses, timeouts, hover/approach-derived summaries, etc. | `/eval/report`, `data.n_outcomes` |
| `Replay` | Number of next-step predictions that have been scored against later observations. | `/next-steps/report`, `predictions.scored` |

## Eval Metric Row

These are quick health checks for whether the harness is producing usable training/eval signal.

| Card | Meaning | How To Read It |
| --- | --- | --- |
| `Claimed capture` | Percentage of displayed pings that produced an outcome row. | Should be high. If low, the UI may be showing pings but not reporting reactions back. |
| `Implicit usable` | Count of implicit weak labels considered usable for training/personalization. | Hover, approach, click, dismiss, and timeout reactions can become weak labels. More is better, but explicit labels are still cleaner. |
| `Top-1 next` | How often the harness's first predicted next step matched later observed behavior, excluding unknown cases. | Higher means the workflow prediction loop is learning plausible next moves. |
| `Unknown` | Fraction of scored next-step predictions that could not be confidently judged. | Lower is better. High unknown means the evaluator cannot map later observations back to predicted steps. |

## Residuals

Residuals explain how next-step prediction scoring came out.

Common residuals:

| Residual | Meaning |
| --- | --- |
| `top1_match` | The first predicted next step matched what happened later. This is the strongest positive prediction signal. |
| `topk_match` | A predicted step matched, but not the top-ranked one. Useful, but weaker than `top1_match`. |
| `no_future_observation` | The evaluator could not find enough later screen evidence to score the prediction. This often happens around idle time, laptop sleep, app shutdown, or insufficient future events. |
| `missed_scene` | The predicted scene/work context did not match the later observed scene. |
| `missed_app_switch` | The predicted app did not match the later active app. |
| `semantic_mismatch` | App/scene may not be enough to judge the prediction as matched; the semantic next action differed. |
| `accepted_intervention` | The user accepted/clicked the intervention. This can override normal next-step scoring because the intervention changed the path. |
| `rejected_intervention` | The user dismissed or muted the intervention. |
| `soft_rejected_intervention` | The user behavior suggested rejection/low value without a hard dismiss. |

In the screenshot, `top1_match` dominating the residuals means most scored predictions in the window matched at rank 1. That is a good sign for the next-step evaluator, but it should still be interpreted carefully because the scoring task is approximate.

## Recent Misses

`Recent misses` lists recent non-green examples from the eval report. These are not necessarily all failures; they are examples worth inspecting.

Common labels:

| Label | Meaning |
| --- | --- |
| `POSITIVE_OUTCOME` | The outcome looked useful or accepted. This appears in this section because the panel is showing recent examples from the eval report, not only severe misses. |
| `NOT_NOW` | The ping may have been directionally relevant, but the timing was wrong. |
| `FALSE_INTERRUPTION` | The harness interrupted when it probably should not have. |
| `MISSED_HELP` | The harness likely should have helped but did not. |
| `MISSING_OUTCOME_SIGNAL` | The daemon/policy emitted something, but the system lacks enough reaction evidence. |
| `QUEUED_NOT_CLAIMED` | A ping was produced by policy but not claimed by the UI. |
| `UNDELIVERED_PING` | A ping was expected but did not make it to a displayed user surface. |
| `SOFT_REJECTION` | User behavior suggests annoyance/low utility without a direct dismiss. |

Each row shows a short message snippet from the intervention/context so you can recognize what the event was.

## Ping Tab

The `Ping` tab appears when there is a live pending notification.

Controls:

- `Yes`: records the intervention as clicked/accepted.
- `Later`: records a snooze-style reaction.
- dismiss button: records a dismissal.

Hovering over buttons and approaching/leaving the capsule are also captured as interaction events. Those are lower-confidence signals than clicks, but they are useful because they produce much more feedback than explicit labeling.

## Diet Tab

The `Diet` tab summarizes the information-diet report. It is meant to expose what the harness thinks you have been researching or reading.

Typical fields:

- research events: screen observations classified as research/information-gathering.
- episodes: grouped research sessions.
- observed minutes: estimated time spent in those episodes.
- domains: top domains involved in research events.
- workflow patterns: inferred patterns such as search, compare, read, synthesize, or tool-switch.
- skill hypotheses: rough summaries of recurring tacit workflows.

The Diet tab is not a truth source. It is an instrumentation layer for understanding what the harness is learning about your information workflow.

## How The Data Flows

1. Fisherman captures local screen/activity context.
2. Harness daemon polls Fisherman and creates candidate events.
3. Optional vision/scene tagging enriches candidates.
4. Policy gate decides whether to ping or skip.
5. Realizer writes a candidate notification message.
6. Critic checks whether the message is safe/useful enough.
7. The Swift capsule claims pending pings and renders them.
8. User reaction is captured: click, later, dismiss, timeout, hover, approach, leave.
9. Outcomes become reward and implicit weak labels.
10. Eval reports join decisions, deliveries, outcomes, labels, traces, and next-step prediction errors.
11. Capsule fetches those reports and visualizes the current 24-hour window.

## How To Manually Inspect The Same Data

From the repo root:

```bash
cd harness
.venv/bin/harness status
.venv/bin/harness eval-report --since 24h
.venv/bin/harness next-steps --since 24h
.venv/bin/harness information-diet --since 24h
```

Or open the web dashboard:

```text
http://127.0.0.1:7893/dashboard
```

## Practical Interpretation

Good signs:

- `Claimed capture` stays near 100%.
- `Outcome` is close to `Claim` over time.
- `Implicit usable` grows without many explicit labels.
- `Top-1 next` is high and `Unknown` is low.
- Residuals are mostly `top1_match` or `topk_match`.

Watch signs:

- Many pings but few claims: UI delivery issue.
- Many claims but few outcomes: reaction capture issue.
- High unknown residuals: next-step evaluator lacks enough future evidence.
- Many `NOT_NOW` or `SOFT_REJECTION` examples: timing policy is too aggressive.
- Many `FALSE_INTERRUPTION` examples: gate is too permissive.

