# Event Context Packet Architecture

Date: 2026-05-26

## Executive Summary

The harness now separates raw telemetry from the model-facing decision example.
`CandidateEvent` remains a thin atomic screen tick. `WorkflowEvent` groups ticks
into app/window task runs. `EventContextPacket` is the frozen policy input: the
exact current observation, recent 5-minute workflow trajectory, short memory,
examples, optional KG-style priors, and provenance used for one binary `ping`/`no_ping`
decision.

This follows the useful part of ProAgentBench: evaluate proactive assistance at
the event/context level rather than pretending isolated screenshots are enough.
It also leaves the Karpathy-style long-term wiki in the right place: Hermes
already maintains `/home/ubuntu/mind` through `skills/mind-rolling-summary`, so
the harness records long-term memory hooks without creating a second wiki.

## Current Layers

```text
Fisherman frame / OCR / app metadata
  -> CandidateEvent
  -> WorkflowEvent
  -> MemorySnapshot
  -> EventContextPacket
  -> ProactiveDecision
  -> Realizer / Critic / Delivery / Outcome
  -> Labels / Curation / Frozen Eval
```

## Data Objects

`CandidateEvent`

- Atomic tick synthesized from Fisherman.
- Contains current app, bundle id, window title, redacted OCR snippet, capture
  gap, frame age, scene tag, and `workflow_event_id`.
- Preserves Fisherman's raw `frontmost_app`; downstream packet/workflow code
  also computes `effective_app` from conservative OCR menu-bar evidence and
  marks `app_metadata_mismatch` when the raw app looks stale.
- Does not contain the full 5-minute context or long-term memory.

`WorkflowEvent`

- Deterministic app/window continuity run.
- Contains start/end, duration, app, title samples, first/last/merged OCR
  previews, candidate ids, and quality flags.
- This is the object closest to the ProAgentBench event unit.

`MemorySnapshot`

- Short in-process memory for recent apps/scenes/outcomes/workflow runs.
- Breaks continuity across capture gaps so laptop sleep does not look like
  active time.

`EventContextPacket`

- Frozen policy input for one decision.
- Stored in `~/.harness/context_packets.jsonl` and mirrored to SQLite
  `context_packets`.
- Contains:
  - current observation
  - raw/effective app identity and metadata quality flags
  - current workflow event
  - recent 5-minute workflow events
  - short memory
  - daily goal
  - recent attention outcomes
  - retrieved wiki memory blocks, if injected
  - similar event blocks, if injected
  - KG-style priors, only when `[policy_learner].use_kg_priors = true`
  - few-shot examples
  - rule baseline
  - privacy state
  - provenance

## Policy Contract

The live `llm_icl_v0` policy now builds and persists an `EventContextPacket`
before calling the model. The model prompt receives `policy_context_packet`,
not an ad hoc reconstruction of candidate + memory + priors.

KG-style priors are deliberately optional. The builder remains available for
frozen ablations and manual inspection, but the live default keeps
`[policy_learner].use_kg_priors = false` until labels/outcomes prove that the
prior table improves ping precision/recall instead of reinforcing stale habits.

The policy still outputs only:

```json
{
  "action": "notch_ping|no_ping",
  "confidence": 0.0,
  "intent_category": "knowledge_qa|code|research|focus|writing|coordination|other|null",
  "reason_codes": ["short_machine_reason"],
  "why_now": "one short phrase, only if action is notch_ping",
  "evidence": {
    "screen_fields_used": [],
    "memory_priors_used": []
  }
}
```

Every returned decision includes `evidence.context_packet_id`, so a later audit
can inspect what the policy saw.

## Long-Term Memory Boundary

The repo already includes the Karpathy-style memory wiki machinery:

- `skills/mind-rolling-summary/SKILL.md`
- `skills/fisherman-deputy-agent/SKILL.md`

Those skills maintain `/home/ubuntu/mind` as a compiled Obsidian-native wiki
from Fisherman captures: rolling summary, digests, context-hours, entity pages,
distillation pages, and index pages.

The harness now has a conservative bridge point for this, but it is disabled by
default. Static `policy_blocks` can be injected for tests/manual experiments.
If `[long_term_memory].policy_retrieval_enabled = true`, the harness can make a
text-only, allowlist-checked provider-chat retrieval call and write the returned
blocks into `EventContextPacket.retrieved_wiki_memory`. This is useful for
dogfood experiments because Hermes may already use its server-side memory, but
the packet still records what the policy was shown.

The bridge is not yet the final audited mind API. A serious version should query
a dedicated Hermes/mind retrieval endpoint that returns source paths, timestamps,
privacy state, and retrieval scores without relying on a generative chat answer.

## Inspection

CLI:

```bash
.venv/bin/harness context-packets --limit 5
.venv/bin/harness context-packets --limit 1 --json-out
```

HTTP:

```bash
curl 'http://127.0.0.1:7893/context-packets?window=24h&limit=10'
```

Dashboard:

- `http://127.0.0.1:7893/dashboard`
- Activity tab shows recent policy context packets.

The local API serving these inspection routes runs as a supervised child
process (`harness.api_server`), separate from the daemon's policy loop. Heavy
dashboard/eval reads therefore should not block `/pending`, `/status`, or the
screen-polling loop.

## Daily Goal Boundary

The daily goal is still the top-level steering variable for the binary policy.
It can be written directly through Settings or generated through the collapsed
Draft goal helper:

- UI: floating capsule → Settings → Today → Draft goal
- HTTP: `POST /goal/interview`
- Storage: `~/.harness/goal_interviews.jsonl`

The endpoint runs locally by default so Settings stays responsive. If
`[goal_interview].use_model = true`, it uses the configured OpenAI-compatible
endpoint and model-host trust checks. It only writes the actual goal when the
user saves/applies through the existing `/goal` state path. This keeps goal
synthesis separate from policy decisions and makes it auditable.

## Remaining Memory Gap

The next step is not another local packet refactor. It is direct long-term
memory retrieval and ablation:

1. Expose a dedicated audited Hermes/mind query API.
2. Return compact blocks with `source`, `title`, `summary`, `uri`, timestamp,
   privacy state, retrieval score, and reason for inclusion.
3. Keep injecting those blocks into `EventContextPacket.retrieved_wiki_memory`.
4. Run frozen eval ablations:
   - no long-term memory
   - KG priors only
   - wiki memory only
   - KG + wiki memory

Until that exists, the harness has paper-grade short-horizon packetization but
not fully paper-grade long-term memory ablations.
