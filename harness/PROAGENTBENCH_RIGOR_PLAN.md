# ProAgentBench Takeaways For The Harness

Source: `/Users/sxysun/Downloads/2602.04482v2.pdf` (`ProAgentBench: A Benchmark for Proactive Service Agents`).

Date reviewed: 2026-05-22.

## Executive Take

The paper is useful because it treats proactive assistance as an evaluation problem first, not as a notification UX problem. The important move is the hierarchical decomposition:

1. **When to Assist**: binary classification over the user's recent observation sequence and user memory.
2. **How to Assist**: only after a positive trigger, generate the assistance content.

Our harness mostly has this shape now: `rule_v0`/`llm_icl_v0` decide `notch_ping` vs `no_ping`, then the realizer writes the message. The gap is rigor: the paper has explicit event-level datasets, hard-negative sampling, time-based splits, privacy/curation records, ablations, and memory comparisons. Our live harness has better implicit behavior signal than the paper's offline benchmark, but our dataset and eval protocol are not yet at the same standard.

The most important design correction: **reason codes should remain machine metadata, not realizer templates**. The paper asks the prediction model for structured outputs such as help-needed and intention category; it does not make generation obey a fixed `reason_code -> message shape` table. We should keep `reason_codes` for debugging/eval, while `why_now` should be a compact natural-language policy rationale.

## What The Paper Actually Does

### 1. It Defines A Two-Stage Proactive Agent

The paper formalizes a proactive agent as:

```text
B_t = f_when(U, O_1:t) in {0, 1}
```

where:

- `U` is user meta-information and historical behavior.
- `O_1:t` is the temporal observation sequence up to now.
- `B_t = 1` means assistance is needed.
- `B_t = 0` means no intervention.

Only if `B_t = 1` does the system perform the second task:

```text
C_t = f_how(U, O_1:t)
```

where `C_t` is the assistance content.

Harness implication: the system should be judged primarily as a **binary interruption policy**. Message quality matters, but it is downstream. A great message at the wrong time is still a failure.

### 2. It Uses Temporal Snapshot Sequences, Not Isolated Screenshots

The input is not a single frame. It is a sequence of snapshots with:

- screenshot
- timestamp
- active app
- window title
- user profile / long-term memory

The experiments use the previous **5 minutes** of interaction events as the main recent context. They also ablate windows from 30 seconds to 10 minutes and find that 5 minutes is a good efficiency/quality tradeoff.

Harness implication: the new `workflow_events.jsonl` layer is directionally right. We need to make it first-class in policy/eval, not just a dashboard convenience.

### 3. It Collects Real Longitudinal Computer-Use Data

They use LifeTrace to collect:

- 1Hz screenshots
- app usage logs
- app/window event segments
- event summaries and annotations

Events are segmented by application switching and temporal continuity. This is close to what we added with `workflow_events.py`, but their collection is more complete: each event links screenshots, app/window metadata, annotations, privacy decisions, and curation records.

Harness implication: our current candidates are atomic ticks; workflow events should become the unit for memory, curation, and some evaluation.

### 4. It Filters Hard Negatives, Not Just Random Negatives

For `When to Assist`, they select non-assistance moments that are contextually similar to true assistance moments. This removes trivial negatives like inactivity and forces the model to distinguish subtle timing differences.

Harness implication: most of our `no_ping` rows are easy negatives. A useful training/eval set needs hard negatives:

- same app/window as a helpful ping, but no help needed
- same daily goal context, but user is already progressing
- research-looking tabs that are actually irrelevant
- coding-looking screens with no visible next-step friction
- notifications the user ignored or dismissed in similar contexts

### 5. It Evaluates With Precision, Recall, F1, And Intention Accuracy

For timing:

- Accuracy
- Precision
- Recall
- F1

For content/intention:

- intention/category accuracy
- semantic similarity to expected assistance

Harness implication: our live dashboard should stop treating "number of pings" as a quality proxy. The north-star metrics should be:

- precision of pings: among pings, how many were useful?
- recall of help opportunities: among moments where help would have helped, how many did we catch?
- false interruption rate
- missed help rate
- claimed-ping outcome capture
- decision-to-trace completeness
- message usefulness conditional on a correct ping

### 6. CoT Is Not A Free Win

The paper tests zero-shot, CoT, and self-consistency. CoT often overthinks simple screens, imagines future problems, and shifts decision boundaries unpredictably. It also adds latency. Zero-shot and self-consistency are more robust for real-time proactivity.

Harness implication:

- Do not ask the live policy to produce long chain-of-thought.
- Keep live policy outputs short and structured.
- Use self-consistency only offline or for low-frequency calibration, not every 5-second tick.
- Store compact rationales for audit, but do not rely on verbose reasoning.

### 7. Text-Only Often Beats Or Matches Vision

Their modality ablation finds that raw screenshots do not consistently improve proactive prediction and can reduce accuracy/precision. Text-only OCR plus metadata is often more stable and efficient.

Harness implication:

- Default policy should be text/metadata/workflow-event based.
- Use vision as an enrichment path when OCR/app metadata is insufficient or when the realizer needs the actual UI.
- Avoid per-tick VLM unless it is strongly gated; current rate-limit failures are already showing why.

### 8. Long-Term Memory Helps, Especially Knowledge-Graph Priors

They compare RAG, KG, and clustering/persona memory. KG performs best because it gives interpretable priors:

- app -> probability help is needed
- app -> likely intention categories
- window keywords -> likely intention categories
- app transition patterns

Harness implication: before training a local neural classifier, build a small local KG-style prior table from our own logs:

```text
P(should_ping | app)
P(should_ping | app, scene)
P(should_ping | window_keyword)
P(intent | app)
P(intent | window_keyword)
transition counts: app_i -> app_j
```

Serialize these as soft hints in the policy prompt. This is cheap, interpretable, and aligned with the paper.

### 9. Privacy And Curation Are Part Of The Benchmark

Their dataset includes explicit privacy/curation artifacts: exclusion lists, annotation files, confidence, optional rationales, platform tags, and deterministic joins back to event ids. They use a three-stage privacy process:

1. automated sensitive-content screening
2. human review / user retention control
3. deterministic rule-based filtering

Harness implication: our privacy redaction is strong for model calls, but our dataset governance is still thin. We need curation records and review surfaces, not only redaction code.

## Current Harness Compared To The Paper

| Dimension | Paper | Harness Now | Gap |
| --- | --- | --- | --- |
| Core framing | Binary `When to Assist`, then `How to Assist` | `llm_icl_v0`/`rule_v0` then realizer | Good shape |
| Recent context | 5-minute temporal event context | candidate memory + workflow events | Need stronger event-first policy input |
| Event layer | LifeTrace events with screenshot links and annotations | `workflow_events.jsonl` app/window runs | Need quality filters, summaries, joins |
| Ground truth | LLM-use events + curated labels | implicit outcomes + explicit labels | Need missed-help labels and hard negatives |
| Metrics | Accuracy, precision, recall, F1, intention accuracy | capture, reward, implicit counts, taxonomy | Need true precision/recall panels |
| Hard negatives | contextually similar non-assistance moments | mostly all `no_ping` decisions | Need matched hard-negative sampler |
| Memory | RAG/KG/cluster ablations | short-term memory + few-shot examples | Need local KG priors and ablations |
| Prompting | zero-shot, CoT, SC compared | live ICL policy, no formal prompt ablation | Need offline prompt/memory comparison |
| Vision | modality ablation; text often stable | realizer vision + optional scene VLM | Need gate vision harder; text-first policy |
| Privacy | VLM screening + human review + rules + deletion | local redaction + endpoint allowlist | Need curation ledger/review workflow |
| Reproducibility | SQLite + JSON/JSONL annotations | JSONL + SQLite sidecar | Need dataset manifest/versioned splits |

## Read On The Current Screenshot

The screenshot is not alarming, but it reveals one naming bug and one data-quality issue.

Observed live numbers:

- 39 ping decisions in the last 24h
- 19 claimed/displayed pings
- 19 outcomes
- claimed-ping outcome capture = 100%
- 20 ping decisions with no trace/delivery/outcome

So the capsule showing `MISSING_OUTCOME_SIGNAL` is misleading. The claimed pings are fine: every claimed ping has an outcome. The suspicious rows are policy decisions that said `notch_ping` but never got a trace or delivery row. That is not an outcome-capture bug; it is a **decision-to-trace gap**. Likely causes:

- daemon restarted after writing the decision but before writing the trace
- realizer path hung/aborted before trace append
- process was killed during an in-flight ping
- older code wrote decision rows before guaranteed finalization

Fix direction:

- distinguish `trace_gap_before_delivery` from `missing_outcome_signal`
- add a metric for decision-to-trace completeness
- ideally append a trace immediately after decision with `stage=decision_recorded`, then update it through realization/delivery/outcome

## Reason Codes And `why_now`

The paper does not do `reason_code in why_now -> message shape`.

Better split:

- `reason_codes`: machine-readable audit fields used for metrics, dashboards, hard gates, and training features.
- `why_now`: compact natural-language rationale for the realizer and human review.
- `intent/category`: optional structured output for eval, not a prompt template.

Bad live pattern:

```text
why_now = "goal_aligned_help, long_session_on_one_app"
realizer maps each code to a fixed message shape
```

Better live pattern:

```json
{
  "action": "notch_ping",
  "confidence": 0.72,
  "reason_codes": ["goal_alignment", "visible_friction"],
  "why_now": "The current Chrome thread appears directly tied to today's harness-eval goal, but the user is cycling through references without extracting a next test."
}
```

Then the realizer grounds the final line in screen evidence and recent workflow trajectory.

## Target Architecture For Lab-Grade Harness

### Data Model

Keep atomic rows, but make event ids the join spine:

```text
candidates.jsonl
workflow_events.jsonl
decisions.jsonl
traces.jsonl
deliveries.jsonl
outcomes.jsonl
retro_labels.jsonl
implicit_labels.jsonl
curation.jsonl
splits.jsonl
model_calls.jsonl
```

Every row that can be joined should include:

- `candidate_id`
- `workflow_event_id`
- `decision_id` if applicable
- `policy_version`
- `schema_version`
- `privacy_state`
- `created_at`

### Policy Interface

Live policy output should be:

```json
{
  "action": "notch_ping|no_ping",
  "confidence": 0.0,
  "intent_category": "knowledge_qa|code|research|focus|writing|coordination|other|null",
  "reason_codes": ["machine_feature"],
  "why_now": "short natural-language rationale",
  "evidence": {
    "workflow_event_ids": [],
    "screen_fields_used": [],
    "memory_priors_used": []
  }
}
```

No chain-of-thought. No long rationale. No prompt-template coupling.

### Eval Units

Use three eval units:

1. **Candidate-level**: did the policy choose ping/no-ping correctly at this tick?
2. **Event-level**: across this app/window workflow event, was there a missed help opportunity or false interruption?
3. **Ping-level**: if shown, was the message useful, ignored, snoozed, or dismissed?

This matters because one bad event can produce many candidate ticks; treating every tick independently can inflate confidence.

## Plan To Reach Paper-Level Rigor

### Phase 1: Fix The Measurement Spine

Goal: every `notch_ping` decision must have an auditable terminal state.

Tasks:

- Add `trace_gap_before_delivery` taxonomy. Done in this pass.
- Add decision-to-trace completeness metric.
- Append trace immediately after decision, before realization.
- Update trace stages as the ping moves through `realizer_started`, `realizer_done`, `critic_done`, `dispatch_done`, `claimed`, `outcome`.
- Add dashboard funnel:
  - eligible ping decisions
  - traces created
  - realization attempted
  - pushed
  - claimed
  - outcome captured
- Make `MISSING_OUTCOME_SIGNAL` mean only claimed/displayed ping with no outcome.

Acceptance criteria:

- 99%+ ping decisions have trace rows.
- 95%+ pushed pings are either claimed or explicitly expired.
- 95%+ claimed pings have outcomes.

### Phase 2: Event-Level Dataset Quality

Goal: workflow events become real dataset objects, not just summaries.

Tasks:

- Persist active/open workflow event snapshots periodically, not only on close.
- Add event quality flags:
  - too short
  - too long
  - no valid frame
  - sensitive
  - stale capture
  - app/window unknown
- Link each candidate to a `workflow_event_id`.
- Add first/last OCR snippets and compact rolling title/app transitions.
- Add event-level summary only after privacy filtering.
- Add a dashboard/table for recent workflow events and their candidate/ping/outcome joins.

Acceptance criteria:

- Every candidate has a workflow event id unless invalid/sensitive.
- Event rows can reconstruct the last 5 minutes used for policy.
- Event-level counts match candidate-level counts within expected exclusions.

### Phase 3: Build Hard-Negative And Missed-Help Labeling

Goal: measure recall, not only precision.

Tasks:

- Hard-negative sampler:
  - match positives by app, scene, window keywords, daily goal terms, and time-of-day
  - include near-positive moments before/after helpful pings
  - exclude trivial idle/sensitive/stale frames
- Missed-help candidate miner:
  - user manually opens ChatGPT/Claude/Cursor chat after a no-ping context
  - user performs repeated search/query reformulations
  - user switches between docs/code/error pages repeatedly
  - user later asks for help about content visible in prior no-ping event
- Label UI should present event snippets, not just latest tick.
- Add labels:
  - `should_ping`
  - `should_not_ping`
  - `not_now`
  - `insufficient_context`
  - optional `intent_category`

Acceptance criteria:

- At least 100 hard negatives.
- At least 50 positive help opportunities.
- Precision/recall/F1 can be computed on time-based holdout splits.

### Phase 4: Paper-Style Offline Eval Protocol

Goal: changes are judged against frozen datasets before dogfood activation.

Tasks:

- Create dataset manifests:
  - `datasets/harness/YYYY-MM-DD/manifest.json`
  - candidate/event/traces paths
  - label paths
  - privacy/curation paths
  - split definition
- Enforce time-based splits.
- No future leakage: memory retrieval may only use events before the candidate timestamp.
- Add bootstrap confidence intervals for precision, recall, F1.
- Report per-context slices:
  - app
  - scene
  - daily goal present/absent
  - notification source: rule/LLM/explore
  - vision used/not used

Acceptance criteria:

- `harness eval-report` includes precision/recall/F1 when labels support it.
- Every policy change has before/after report on same frozen split.
- Dashboard distinguishes live dogfood metrics from offline validation.

### Phase 5: Memory Ablations

Goal: match the paper's memory rigor without prematurely training a heavy model.

Tasks:

- Baselines:
  - rule only
  - LLM zero-shot current context
  - LLM + 5-minute workflow events
  - LLM + few-shot implicit/explicit labels
  - LLM + KG priors
  - LLM + RAG similar events
- Build local KG priors:
  - app -> help probability
  - scene -> help probability
  - app/scene -> positive/negative outcome rates
  - title keyword -> help probability
  - transition pattern -> help probability
- Add strict temporal retrieval.
- Evaluate latency and quality separately.

Acceptance criteria:

- KG prior retrieval under 5ms.
- Policy prompt remains compact.
- Ablation report identifies whether KG/RAG actually improves precision/recall.

### Phase 6: Prompting And Modality Ablations

Goal: avoid superstition around VLMs and reasoning prompts.

Tasks:

- Compare:
  - text-only policy
  - text + workflow events
  - text + KG priors
  - screenshot/VLM scene tagger
  - self-consistency on uncertain samples only
- Explicitly do not use live CoT.
- Track:
  - policy latency
  - realizer latency
  - model error/rate-limit rate
  - prediction quality
  - privacy suppression rate

Acceptance criteria:

- Vision is enabled only where it improves measured results.
- Scene VLM rate limiting cannot cause repeated ping decisions without trace.
- Policy latency budget is explicit.

### Phase 7: Privacy And Curation Ledger

Goal: every training/eval row has a privacy provenance.

Tasks:

- Add `curation.jsonl`:
  - row id
  - candidate/event id
  - retain/blur/delete/exclude
  - reason
  - source: auto/privacy/user/manual
  - timestamp
- Store privacy state per event:
  - OCR redacted
  - screenshot suppressed
  - screenshot redacted
  - sensitive reasons
- Add review panel for deleting/excluding events from future training.
- Add dataset export that omits raw OCR/screenshots by default.

Acceptance criteria:

- Training/eval builders respect curation exclusions.
- Sensitive events cannot enter prompt examples.
- User can inspect and delete local examples.

## Immediate Next Moves

1. Make the pipeline funnel exact: decision -> trace -> realization -> push -> claim -> outcome.
2. Convert workflow events into joinable dataset objects by adding `workflow_event_id` to candidates/decisions/traces.
3. Replace realizer reason-code templates with natural `why_now` rationale.
4. Add hard-negative sampling and event-level labeling.
5. Add KG priors as the first serious memory mechanism.
6. Add time-based frozen eval splits and precision/recall/F1.

## Implementation Status: 2026-05-26

The first paper-style rigor pass is now implemented in code:

- The daemon appends a trace immediately after each decision and patches it through lifecycle stages: `decision_recorded`, `realizer_started`, `realizer_done`/`realizer_failed`, `critic_started`, `critic_done`, `dispatch_started`, `dispatch_done`, `claimed`, `outcome`, and terminal skipped/blocked/no-ping states.
- Candidates, decisions, and traces now carry `workflow_event_id`, and workflow events include basic quality flags plus periodic open snapshots.
- `EventContextPacket` is now a first-class stored object. The live `llm_icl_v0` policy persists the frozen model-facing packet to `context_packets.jsonl`/SQLite before calling the model, and decisions include `evidence.context_packet_id` for audit/replay.
- The policy-facing path now preserves raw Fisherman `frontmost_app` metadata but also computes `effective_app` from conservative OCR menu-bar evidence. Workflow grouping, short memory, KG priors, datasets, and shadow eval use `effective_app` when raw app metadata looks stale, and packets mark `app_metadata_mismatch`.
- `metrics` and `eval-report` now expose ping trace-completeness plus explicit-label precision, recall, and F1.
- SQLite sidecar schema now includes typed `deliveries` and `curation` tables in addition to candidates/decisions/traces/outcomes/model calls/labels/workflow events.
- `curation.jsonl` exists and is respected by KG-prior and dataset builders.
- `kg_priors.py` builds local app/scene/app+scene/window-keyword priors from explicit labels and usable implicit outcomes, and `llm_icl_v0` includes matching priors in the live policy prompt.
- `dataset.py` mines useful pings, behavioral negatives, context-matched hard negatives, and missed-help candidates. It now also mines workflow-event review rows, and `harness freeze-eval` writes sanitized candidate and event eval files with time-ordered split bounds.
- `/label/events` adds a browser event-level labeling UI for whole workflow runs. Event labels are stored separately from decision labels via `label_scope="workflow_event"` and feed event-level precision/recall/F1 metrics.
- Replay/shadow evaluation now preserves `workflow_event_id` and reconstructs a no-future-leak recent workflow context for offline memory snapshots.
- `harness freeze-eval` now exports sanitized source candidates, source workflow events, source outcomes, split assignments, and candidate/event examples, so frozen datasets are replayable without reading live JSONL state.
- `harness eval-manifest` replays a policy chronologically over a frozen manifest and reports candidate/event precision, recall, F1, missed-help rate, false-interruption rate, bootstrap confidence intervals, train/validation/test splits, source/confidence-weighted metrics, and slices by app/scene/example type.
- Frozen manifest replay validates split assignment consistency, fails closed on missing required artifacts, counts missing predictions as coverage failures instead of silent no-ping wins, and supports `--require-live-model` for live-model attestation.
- Live delivery handling now requires displayed acknowledgements, expires terminal delivery states, and avoids treating a dequeued-but-never-displayed payload as a clean outcome.
- Shadow eval now uses the active store by default, uses time-ordered group holdout, and avoids ambient `~/.harness` fallback when an explicit store is provided.
- Workflow events now carry first/last OCR previews, window-title samples, and event-level quality flags such as `too_short`, `too_long`, and `no_valid_frame`.
- The per-candidate VLM scene tagger now has explicit error/rate-limit backoff, so a failing VLM endpoint cannot repeatedly spend calls on every eligible tick.
- Smoke coverage is 72 tests, including trace patching, SQL delivery/curation mirroring, KG priors, hard-example curation exclusions, workflow-event review mining, frozen manifest replay, split assignment validation, live-model attestation, source weighting, delivery ack/expiry handling, VLM backoff, and precision/recall metrics.
- `harness context-packets` and `/context-packets` expose recent frozen policy inputs for inspection; the dashboard Activity tab also surfaces recent packets.
- `long_term_memory.py` adds a disabled-by-default text-only policy retrieval bridge. Static `policy_blocks` support tests/manual ablations; provider-chat retrieval is allowlist-checked and writes exact returned snippets into `EventContextPacket.retrieved_wiki_memory`.

Still not done:

- The event-level labeling UI is browser-backed, not yet surfaced inside the native capsule.
- The frozen eval protocol now has a manifest replay command with leakage checks. The provider-chat memory bridge has smoke coverage, but policy-specific RAG retrievers still need ablation fixtures before they are allowed into offline comparison.
- KG priors are simple count priors; RAG similar-event retrieval and ablation reports are still future work.
- Hermes/mind long-term memory already exists through `skills/mind-rolling-summary`. The harness has a disabled provider-chat bridge, but a dedicated non-generative `/home/ubuntu/mind` retrieval API with source paths, timestamps, privacy state, retrieval scores, and frozen ablations is still the serious target.
- The curation ledger is CLI-backed and event-review-backed for workflow events; there is still no full capsule review panel for arbitrary candidate/trace deletion.
- Harness storage is append-mostly. There is SQLite backfill, but no automatic retention/compaction policy for long-running dogfood state yet.

## Non-Goals For Now

- Do not train a neural local classifier until the event/label protocol is clean.
- Do not make VLM mandatory for every tick.
- Do not add action agents on the laptop.
- Do not optimize message style before timing precision improves.
- Do not trust aggregate reward without separating claimed pings, unclaimed pings, trace gaps, and missed-help labels.
