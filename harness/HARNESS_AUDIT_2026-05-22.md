# Harness Audit - 2026-05-22

## Scope

This audit covered the harness repo surface, not the main Fisherman app:

- live daemon pipeline: candidate -> decision -> trace -> realization -> dispatch -> claim -> outcome
- floating capsule/notch delivery path
- JSONL + SQLite read/write paths
- dashboard/eval/diet/settings endpoints
- decision labeler and event-review labeling path
- hard-example mining and frozen eval dataset export
- offline replay/shadow-eval policy comparison
- docs that describe the harness architecture

## Current Read

The harness is directionally right: it is now mostly a binary interruption policy system, with message generation downstream. The important lab-grade question is not "did it make a nice notification?" but:

```text
given the current screen + recent workflow trajectory + past feedback,
should it ping or stay quiet?
```

The codebase now has the right major streams:

- `candidates.jsonl`: observed screen/context moments
- `workflow_events.jsonl`: app/window workflow runs
- `decisions.jsonl`: binary policy decisions
- `traces.jsonl`: lifecycle rows for decisions
- `deliveries.jsonl`: UI claim/display rows
- `outcomes.jsonl`: click/hover/dismiss/timeout reactions
- `retro_labels.jsonl`: explicit human labels, now with candidate and workflow-event scopes
- `curation.jsonl`: retain/exclude/delete/blur decisions
- `model_calls.jsonl`: privacy-safe model call audit rows

## Findings

### 1. Ping surfacing was fragile

Root cause was in the Swift capsule lifecycle:

- pending payload expiry was based on daemon enqueue time, not actual display time
- outcome POSTs were fire-and-forget with a short timeout
- if `/outcome` timed out, the UI cleared locally but the pending file stayed on disk and could be claimed again

Status: fixed in `0c4e3c2`. The forced ping test claimed once, captured hover/click interactions, wrote an outcome, and drained `~/.harness/pending`.

### 2. Live metrics still show historical trace gaps

The 24h audit snapshot showed:

- 5748 decisions
- 35 ping decisions
- 15 traced pings
- 15 claimed pings
- 18 outcomes
- claimed-ping outcome capture = 100%
- ping trace completeness = 42.9%

Interpretation: current claimed pings are completing, but old rows from before the trace-lifecycle hardening still pollute the 24h trace-completeness metric. The metric is useful, but it should be read as a rolling data-quality indicator, not proof that current code is still dropping every trace.

### 3. Replay was missing workflow context

`eval/replay.py` converted candidate rows back into `CandidateEvent` but dropped `workflow_event_id`. `shadow_eval.py` rebuilt memory snapshots without recent workflow-event summaries. That made offline eval thinner than live policy input.

Status: fixed. Replay preserves `workflow_event_id`, and replay/shadow eval reconstruct a no-future-leak recent workflow context from candidates at or before the replay tick.

### 4. Hard-example mining was too positive-skewed

`harness hard-examples` could return only useful-ping positives because examples were sorted by confidence/rank and then truncated. That weakens the binary classifier task because hard negatives and missed-help rows are exactly what teach the boundary.

Status: fixed. The sampler now balances positives, negatives, hard negatives, and missed-help candidates before truncation.

### 5. Event-level evaluation was missing a real label loop

The repo had `workflow_events.jsonl`, but the labeler was still centered on decision ticks. That meant we could measure ping reactions but not reliably measure event-level recall: "during this whole research/debugging run, should Hermes have helped at least once?"

Status: fixed first pass.

- `/label/events` now reviews whole workflow events.
- `GET /label/events/queue` serves mined event-level examples.
- `POST /label/events/submit` appends labels with `label_scope="workflow_event"`.
- `POST /label/events/curate` can exclude workflow events from training/eval.
- dashboard Eval links to the event-review UI.
- metrics report event labels separately from candidate labels.

### 6. Frozen eval needed clearer temporal protocol

`harness freeze-eval` wrote a time split, but the manifest did not state the no-future-leakage rule strongly enough and had no event-level export or manifest replay command.

Status: fixed.

- `examples.jsonl` contains candidate-level examples.
- `event_examples.jsonl` contains workflow-event examples.
- `source_candidates.jsonl`, `source_workflow_events.jsonl`, and `source_outcomes.jsonl` make the bundle replayable without reading live state.
- manifest includes candidate/event split bounds.
- manifest states the memory rule: replay may only use candidates, outcomes, labels, priors, and workflow events with timestamps at or before the example timestamp.
- `harness eval-manifest` replays a policy chronologically over the source stream and reports candidate/event metrics, bootstrap confidence intervals, and app/scene/example-type slices.
- The final hardening pass added `split_assignments.jsonl`, split consistency checks, source/confidence-weighted metrics, fail-closed missing-artifact behavior, missing-prediction coverage accounting, and optional `--require-live-model` attestation.

### 7. Old next-step prediction files remain local state

`~/.harness/next_step_predictions.jsonl` and `~/.harness/prediction_errors.jsonl` still exist from the removed next-step/replay experiment. They are not part of the current harness code path, but they consume local disk and can confuse manual inspection.

Status: not deleted automatically. They are historical local artifacts, not repo state. If cleanup is desired, archive or delete them explicitly after confirming no personal data needs to be retained.

## What Changed In This Pass

- Added workflow context reconstruction to offline replay/shadow eval.
- Added workflow-event example mining in `dataset.py`.
- Balanced hard-example sampling so hard negatives and missed-help rows survive truncation.
- Extended frozen eval manifests with source streams, event examples, no-future-leak metadata, and manifest replay.
- Added split assignment export/validation, source weighting, live-model attestation, and missing-prediction coverage accounting.
- Added `/label/events` workflow-event review UI and submit/curate endpoints.
- Added event-label metrics separate from candidate-label metrics.
- Linked event review from the dashboard Eval tab and decision labeler.
- Added event quality fields: first/last OCR previews, title samples, and event-level flags.
- Updated README, capsule guide, and ProAgentBench rigor plan.

## Current Gaps

The remaining important gaps are now narrower:

- RAG/similar-event retrieval is not implemented, and should not be added to live policy until its temporal no-leak behavior is tested.
- Event-review UI is browser-backed, not native inside the floating capsule.
- Curation is available for workflow events from event review, but arbitrary candidate/trace deletion still needs a fuller review panel.
- Storage remains append-mostly; there is backfill coverage, but no automatic retention/compaction job yet.
- The dashboard can show low trace completeness until pre-fix historical rows age out of the selected window.
- The LLM ICL learner still depends on endpoint latency and quality; keep `rule_v0` hard gates and recent-negative-feedback backoff as non-negotiable safety rails.
- RAG/similar-event retrieval is still intentionally absent. Add it only with a temporal retrieval test proving no future examples can be retrieved.

## Verification Commands

```bash
.venv/bin/python -m compileall harness eval policies
.venv/bin/harness event-examples --since 7d --limit 20
.venv/bin/harness freeze-eval --since 7d --limit 40 --out /tmp/harness-freeze-test
.venv/bin/harness eval-manifest /tmp/harness-freeze-test/manifest.json --policy rule_v0
# Optional promotion gate when the dataset/policy run should prove live-model use:
.venv/bin/harness eval-manifest /tmp/harness-freeze-test/manifest.json --policy rule_v0 --require-live-model
.venv/bin/python -m pytest tests/test_smoke.py -q
```
