# Harness Capsule Guide

This document explains the floating Harness capsule UI, especially the **Pipeline and eval** panel shown when the capsule expands.

## What The Capsule Is

The Harness capsule is a separate macOS surface from Fisherman. Fisherman owns the main notch. Harness uses a small floating capsule so the harness can run at the same time without fighting for the same top-center notch slot.

The capsule is backed by the local harness daemon at `http://127.0.0.1:7893`. It does not compute the metrics itself; it fetches reports from daemon endpoints:

- `GET /dashboard/data?window=24h`
- `GET /eval/report?window=24h&max_examples=4`
- `GET /information-diet/report?window=24h&max_episodes=6`
- `GET /dashboard/config`
- `GET /status`

## Capsule Controls

- Hover the compact capsule to expand it.
- Drag the compact capsule, or the `Harness` title area in expanded mode, to move it.
- Release after dragging to snap it to the nearest screen edge.
- Click the pin icon to keep it open.
- Click the refresh icon inside a panel to refetch that report.
- Use `Pipeline`, `Diet`, and `Settings` tabs to switch views.
- When a live notification is pending, a `Ping` tab appears with `Yes`, `Later`, and dismiss controls.
- The old Harness menubar item has been removed. Settings, snooze, labeler, and dashboard links now live in the floating capsule Settings tab.
- The capsule is configured to join all macOS Spaces and fullscreen desktops. A visibility watchdog reasserts that placement after Space or display changes.

## Why It Sometimes Shows Loading

The panel shows `loading...` while the Swift app is waiting for daemon reports. The slowest part is usually the eval report because it joins decisions, outcomes, delivery claims, implicit labels, compact traces, and policy calibration data. The Diet report is also heavier than a normal status call because it groups research-like observations into episodes.

The current UI keeps the expanded view alive behind the compact capsule so it can cache results across hover open/close cycles. A normal hover should reuse already-fetched data after the first load. It may still show `loading...` after the harness app restarts, when switching to a tab for the first time, or after clicking refresh.

The Settings tab should be much faster than Pipeline or Diet. It fetches only `/dashboard/config` and `/status`; if Settings itself hangs for seconds, that points to the daemon being unavailable, a local HTTP problem, or the Swift app being restarted while the request is in flight.

## Pipeline Row

The first row shows the end-to-end intervention pipeline over the current 24-hour window.

| Card | Meaning | Source |
| --- | --- | --- |
| `Observe` | Number of screen-derived candidate moments the harness considered. A candidate is a snapshot/context event from Fisherman plus OCR/scene metadata. | `/dashboard/data`, `n_candidates` |
| `Gate` | Number of policy decisions made for candidates. This includes both pings and non-pings. | `/dashboard/data`, `n_decisions` |
| `Ping` | Number of decisions where policy said a notification was warranted. This is the “eligible to interrupt” count. | `/eval/report`, `data.n_pings` |
| `Claim` | Number of pings actually claimed/displayed by the Swift capsule app. This separates “daemon wanted to notify” from “UI actually showed it.” | `/eval/report`, `data.n_claimed_pings` |
| `Outcome` | Number of reaction rows captured after pings, including clicks, dismisses, timeouts, hover/approach-derived summaries, etc. | `/eval/report`, `data.n_outcomes` |

## Eval Metric Row

These are quick health checks for whether the harness is producing usable training/eval signal.

| Card | Meaning | How To Read It |
| --- | --- | --- |
| `Claimed capture` | Percentage of displayed pings that produced an outcome row. | Should be high. If low, the UI may be showing pings but not reporting reactions back. |
| `Implicit usable` | Count of implicit weak labels considered usable for training/personalization. | Hover, approach, click, dismiss, and timeout reactions can become weak labels. More is better, but explicit labels are still cleaner. |
| `Explicit labels` | Count of human retro labels in the selected window. | The cleanest eval signal for the ping/not-ping classifier. |
| `Label coverage` | Explicit retro labels divided by decisions in the selected window. | `0.0%` means no decisions in that window have human labels. It does not count implicit labels or hover/dismiss outcomes. |

## Settings Tab

The Settings tab edits local config and policy state. API key fields are masked in the collapsed view: the UI shows a small prefix/suffix preview so you can recognize the key without displaying the full value. Use the edit control beside a key field to reveal/edit it intentionally.

`Learner endpoint` configures the LLM-ICL policy learner in `harness/policies/llm_icl_v0.py`, not the message realizer. The learner is the component that decides `notch_ping` versus `no_ping`.

| Field | Config / Code | Meaning |
| --- | --- | --- |
| `Base URL` | `[policy_learner].base_url` | OpenAI-compatible endpoint used for the ping/not-ping learner. |
| `Model` | `[policy_learner].model` | Model name sent to that endpoint. |
| `API key` | `[policy_learner].api_key` | Local credential for the learner endpoint. Masked in the capsule. |
| `Examples` | `[policy_learner].max_examples` | Maximum few-shot examples to include in each learner call. These examples are pulled from explicit retro labels plus usable implicit weak labels. This number is a cap, not the current label count. If the cap is 18 but only 6 usable examples exist, the learner gets 6. |
| `Call gap` | `[policy_learner].min_interval_sec` | Minimum spacing between learner model calls. Within the gap, the policy falls back to the guarded baseline so the daemon does not call the LLM on every tick. |
| `Min confidence` | `[policy_learner].min_confidence_to_ping` | The LLM must choose `notch_ping` with at least this confidence before the harness interrupts. Lower values explore more; higher values conserve attention. |

The few-shot example builder reads `decisions.jsonl`, `traces.jsonl`, `retro_labels.jsonl`, and `outcomes.jsonl`. Explicit labels are preferred. Implicit labels are accepted only when the outcome can be mapped into a useful binary target, such as `would_help`, `would_annoy`, or `good_no_ping`.

## Implicit Usable In Detail

`Implicit usable` comes from `harness/harness/implicit.py`.

Every outcome row can become a weak label if it has a `decision_id`. The conversion is:

| User action / signal | Weak label | Direction | Confidence | Usable? |
| --- | --- | --- | --- | --- |
| `clicked` | `would_help` | `positive` | `0.95` | yes |
| `dismissed` or `muted` | `would_annoy` | `negative` | `0.90` | yes |
| `snoozed` | `not_now` | `neutral` | `0.55` | yes |
| `timed_out` + `positive_considered` | `would_help` | `positive` | `0.45` | yes |
| `timed_out` + `rejection_considered` | `would_annoy` | `negative` | `0.65` | yes |
| `timed_out` + `snooze_considered` | `not_now` | `neutral` | `0.35` | yes |
| `timed_out` + `approached` | `ignored_after_notice` | `weak_negative` | `0.25` | yes |
| `timed_out` + no meaningful signal | `would_annoy` | `weak_negative` | `0.40` | yes |
| unknown action | `unknown` | `unknown` | `0.0` | no |

This means `Implicit usable` can still be lower than `Ping` or `Claim` because not every ping produces a usable behavioral label. Examples:

- A ping can be claimed but not yet have an outcome row in the selected window.
- A ping can be queued but not claimed/displayed by the Swift app.
- A ping can have an unknown or malformed outcome action.
- The panel uses a rolling 24-hour window, so older usable labels disappear from this view as time passes.
- `Ping` is a policy decision count; `Implicit usable` is an outcome-derived training-signal count.

If you see 27 pings and 16 implicit usable, that usually means 16 of those pinged moments produced a usable outcome-derived weak label, while the rest were missing outcomes, outside the window, queued but not claimed, or malformed.

## Binary Ping / Not-Ping Frame

The core harness decision is binary:

```text
given current context, should the system ping or stay quiet?
```

The code models that in three layers:

- `harness/policies/rule_v0.py` is the deterministic safety/baseline gate. It owns hard suppressions such as quiet hours, snooze, calls, sleep/resume boundaries, cooldown, and recent negative feedback.
- `harness/policies/llm_icl_v0.py` is the default live LLM in-context policy learner. It runs `rule_v0` first for hard gates/fallback, then asks an OpenAI-compatible text model to choose `notch_ping` or `no_ping` from current context plus recent labeled examples.
- `harness/harness/trainer.py` replays candidate contexts against alternative policy settings and scores them using explicit labels plus confidence-weighted implicit labels.

For labels, the binary interpretation is:

| Label | Binary target |
| --- | --- |
| `would_help` | should ping |
| `would_annoy` | should not ping |
| `good_no_ping` | should not ping |
| `not_now` | timing was bad; useful as weak timing signal, not a clean binary target |
| `ignored_after_notice` | weak should-not-ping signal |

No-signal timeouts are treated as weak `would_annoy` examples because attention is conserved: if the system displayed a ping and there was no meaningful reaction before timeout, that context is evidence against interrupting again. The confidence is lower than an active dismiss because absence of signal is noisier than direct rejection.

The current system is therefore a rule baseline plus a default live ICL policy learner and calibration trainer. It is not yet a trained local classifier, but the data path now points in the right direction: ignored timeouts affect reward, implicit training, and live recent-negative-feedback backoff.

## Why N/A And Empty Recent Misses Happen

`n/a` is not necessarily a bug. It often means the denominator is zero.

In the screenshot with `Ping = 0`, `Claim = 0`, and `Outcome = 0`:

- `Claimed capture` is `n/a` because there were no claimed pings in that 24-hour window.
- `Implicit usable` is `0` because there were no outcome-derived weak labels in that 24-hour window.
- `Recent misses` is empty because there were no ping examples to classify in that 24-hour window.

The capsule now shows `no pings` and an explanatory banner for this state. Use the web dashboard or native Pipeline window if you want longer-window inspection.

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

## Recent-Miss Classification Code

The classification is in `harness/harness/eval_report.py`, function `classify_decision()`.

The decision tree is:

1. If there is an explicit retro label, it wins.
   - `would_help` + `notch_ping` → `true_positive_helpful_ping`
   - `would_help` + no ping → `missed_help`
   - `would_annoy` + `notch_ping` → `false_interruption`
   - `would_annoy` + no ping → `true_negative_good_silence`
   - `good_no_ping` + no ping → `true_negative_good_silence`
   - `good_no_ping` + ping → `false_interruption`
   - `cant_tell` → `ambiguous_label`
2. Else if there is an outcome, classify by user action and interaction signal.
   - `clicked` → `positive_outcome`
   - `dismissed` or `muted` → `negative_outcome`
   - `snoozed` → `not_now`
   - `timed_out` + `rejection_considered` → `soft_rejection`
   - `timed_out` + `positive_considered` → `soft_positive`
   - `timed_out` + `snooze_considered` → `soft_not_now`
   - `timed_out` + `approached` → `approached_then_ignored`
   - `timed_out` + no clear signal → `ignored_ping` (attention-cost negative)
3. Else if there is a usable implicit weak label, classify by weak label.
   - `would_help` → `positive_implicit_only`
   - `would_annoy` → `negative_implicit_only`
   - `not_now` → `not_now_implicit_only`
4. Else if policy chose `notch_ping` but delivery/outcome is incomplete:
   - claimed by UI but no outcome → `missing_outcome_signal`
   - skipped or blocked before display → `undelivered_ping`
   - pushed/queued but not claimed → `queued_not_claimed`
   - otherwise ping with no outcome → `missing_outcome_signal`
5. Else no ping and no label/signal → `unlabeled_silence`.

The “Recent misses” list is filtered by `_surface_example()`. It hides obvious green/boring rows like `true_positive_helpful_ping`, `true_negative_good_silence`, and `unlabeled_silence`; it keeps high/medium severity rows and ping rows that are worth inspecting.

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

Where to see it:

- In the capsule, use the visible `Diet` switch in the header.
- In the browser, open `http://127.0.0.1:7893/dashboard` and click `Diet`.

## Web Dashboard

The web dashboard is served by `harness/harness/dashboard_ui.py` at:

```text
http://127.0.0.1:7893/dashboard
```

Tabs:

- `Activity`: current aggregate activity/decision counts from `/dashboard/data`.
- `Eval`: joined eval report from `/eval/report?window=7d&max_examples=20`.
- `Diet`: information-diet report from `/information-diet/report?window=7d&max_episodes=20`.
- `Settings`: editable config from `/dashboard/config`.
- `Diagnostics`: recent trace/debug rows.

### Dashboard Eval Tab

The Eval tab calls `loadEval()` in `dashboard_ui.py`, which fetches `/eval/report`. The renderer uses:

- `r.data` for decisions, pings, claimed pings, outcome capture, labels, and implicit coverage.
- `r.variants.calibration.best_variant` for policy calibration state.
- `r.taxonomy.by_type` for decision classification counts.
- `r.examples` for recent non-green examples.
- `r.openadapt_style_gaps` for readiness gaps such as outcome capture, explicit labels, implicit labels, variant comparison, and decision volume.

### Dashboard Diet Tab

The Diet tab calls `loadDiet()` in `dashboard_ui.py`, which fetches `/information-diet/report`. The renderer uses:

- `summary.n_research_events`
- `summary.n_episodes`
- `summary.observed_research_min`
- `summary.top_domains`
- `summary.top_terms`
- `summary.workflow_patterns`
- `skill_hypotheses`
- `episodes`

The Diet tab is meant to show what the harness thinks your information diet and research workflow look like, not to drive notification policy directly yet.

## How The Data Flows

1. Fisherman captures local screen/activity context.
2. Harness daemon polls Fisherman and creates candidate events.
3. Optional vision/scene tagging enriches candidates.
4. Workflow eventization groups adjacent candidates into app/window runs, closing runs on app/title changes, stale frames, sensitive/inactive screens, or sleep/capture gaps.
5. Policy gate applies `rule_v0` hard gates, then the default `llm_icl_v0` learner decides whether to ping or skip when eligible. The learner sees the recent workflow run sequence, not just the current frame.
6. Realizer writes a candidate notification message.
7. Critic checks whether the message is safe/useful enough.
8. The Swift capsule claims pending pings and renders them.
9. User reaction is captured: click, later, dismiss, timeout, hover, approach, leave.
10. Outcomes become reward and implicit weak labels.
11. Eval reports join decisions, deliveries, outcomes, labels, traces, and policy calibration.
12. Capsule fetches those reports and visualizes the current 24-hour window.

## Workflow Events

`workflow_events.jsonl` is the harness-local event layer inspired by LifeTrace-style data collection. It is not a replacement for candidate ticks. Candidates are still the atomic decision/eval rows; workflow events are the compact trajectory summaries the policy can read.

The implementation lives in `harness/harness/workflow_events.py`:

- A run starts from the first valid candidate in an app/window.
- The run extends while the active app and window title stay stable.
- The run closes on app change, window-title change, stale frame, inactive/sensitive screen, daemon shutdown, or a capture/time gap above `[workflow_events].max_gap_sec`.
- Closed runs are appended to `~/.harness/workflow_events.jsonl` and mirrored into the SQLite `workflow_events` table.
- The daemon puts the last `[workflow_events].recent_context_sec` seconds of compact runs into `MemorySnapshot.recent_workflow_events`.
- `policies/llm_icl_v0.py` includes those compact runs in the LLM binary policy prompt.

The web dashboard Activity tab now shows recent closed workflow events. The capsule Pipeline panel stays focused on the intervention funnel; use the browser dashboard for deeper run inspection until the native capsule grows a detailed event window.

## How To Manually Inspect The Same Data

From the repo root:

```bash
cd harness
.venv/bin/harness status
.venv/bin/harness eval-report --since 24h
.venv/bin/harness info-diet --since 24h
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
- Explicit labels and implicit usable examples cover both pings and silence.
- Policy variants have enough labeled/implicit signal to compare.

Watch signs:

- Many pings but few claims: UI delivery issue.
- Many claims but few outcomes: reaction capture issue.
- Many `NOT_NOW` or `SOFT_REJECTION` examples: timing policy is too aggressive.
- Many `FALSE_INTERRUPTION` examples: gate is too permissive.
