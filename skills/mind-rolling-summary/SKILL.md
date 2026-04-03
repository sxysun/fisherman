---
name: mind-rolling-summary
description: Build and maintain a searchable rolling memory system in /home/ubuntu/mind from Fisherman captures, with a compact rolling summary plus detailed timestamped markdown context logs.
version: 0.1.0
author: Hermes Agent
license: MIT
---

# Mind Rolling Summary

Use this skill when you need to turn Fisherman screen-capture context into durable, searchable notes under `/home/ubuntu/mind`.

This skill is meant to work together with the `fisherman-cli` skill:
- `fisherman-cli` = how to inspect the captured activity reliably
- `mind-rolling-summary` = how to transform that evidence into a layered memory system

## Goal

Maintain both:
1. a compact high-signal running brief at `/home/ubuntu/mind/rolling-summary.md`
2. richer timestamped markdown logs that preserve more detailed context for later search and reconstruction

The design principle is **layered memory**:
- `rolling-summary.md` = current worldview / stable themes / most important recency signal
- `fisherman-digests/*.md` = timestamped narrative passes (one per analysis pass)
- `context-hours/YYYY-MM-DD/HH.md` = denser searchable hour-bucket notes for reconstruction and retrieval
- `context-entities/*.md` = flexible entity/topic pages for recurring people, companies, projects, chats, or motifs
- `INDEX.md` = top-level map of the entire `/home/ubuntu/mind` folder, including uploaded writings, source docs, syntheses, and Fisherman-derived memory

## Canonical file layout

Under `/home/ubuntu/mind`:

- `rolling-summary.md`
  - current high-signal synthesis
  - should be readable in a few minutes
- `INDEX.md`
  - top-level map of the whole `mind/` folder
  - should index source writings, anchor documents, syntheses, and rolling observational memory
- `fisherman-digests/YYYY-MM-DD_HHMM.md`
  - one file per Fisherman review pass
  - records what window was inspected, freshness status, strongest signals, and uncertainty/corrections
- `context-hours/YYYY-MM-DD/HH.md`
  - one file per UTC hour bucket
  - accumulates detailed evidence for that hour from multiple passes
  - optimized for future search / recall rather than elegance

Active/optional directories depending on how useful they become:
- `context-entities/` for recurring people / companies / projects / chats / motifs
- `context-screens/` for exported screenshots worth preserving

## When to update what

### Update `fisherman-digests/*.md` every review pass
Create a new digest whenever you do a meaningful Fisherman review, whether the pass is:
- a fresh active window
- a continuity / clarification pass
- a correction pass caused by visual re-inspection

### Update `context-hours/YYYY-MM-DD/HH.md` when the pass contains usable detail
If the pass yields meaningful app/chat/browser detail, merge the evidence into the relevant UTC hour file(s).

Examples:
- a pass at `08:23 UTC` reviewing `07:16–07:56 UTC` should update `context-hours/2026-04-03/07.md`
- if evidence spans two hours, update both hour files

Also valid: create a `context-hours/...` note for a mostly empty later hour when doing so preserves continuity or epistemic state, for example:
- confirming there was no newer substantive activity after an important burst
- recording a correction caused by visual reinspection
- marking a boundary between two meaningful bursts
- preventing false recency when a later pass is only continuity/clarification rather than fresh movement

### Update `rolling-summary.md` only when the high-level picture changes or sharpens
Do not rewrite it for every small OCR fragment.
Update it when:
- a new theme emerges
- confidence in an existing theme materially changes
- a collaborator / project / wedge becomes recurring enough to matter
- a correction changes the best current read
- a fresh activity window meaningfully shifts the recency section

### Update `INDEX.md` whenever a new digest, hour file, or entity page is created
Keep it useful as a navigation layer for the whole `mind/` folder, not a full database dump.

## Operating procedure

1. Load and follow `fisherman-cli` first for evidence gathering.
2. Determine the pass type:
   - `fresh active window`
   - `continuity / clarification pass`
   - `correction pass`
3. Separate:
   - direct evidence
   - conservative inference
   - uncertainty / contradictions
4. Write a new digest in `fisherman-digests/`.
5. Merge detailed evidence into the relevant `context-hours/YYYY-MM-DD/HH.md` file(s).
6. If a person/project/chat/topic is clearly recurring, create or update a `context-entities/*.md` page.
7. Update `rolling-summary.md` if the high-signal picture changed.
8. Update `INDEX.md` so the new files are discoverable.

## Writing rules

### For `rolling-summary.md`
Optimize for compression and retrieval.
It should usually contain:
- last updated time
- anchor documents / weighting rules
- stable high-signal themes
- active workstreams
- recurring collaborators / social surfaces
- repeated frictions / constraints
- recency notes
- if useful, a short “where to look next” section

### For `fisherman-digests/*.md`
Each digest should explicitly include:
- timestamp of the digest
- reviewed time window
- current clock time during pass
- assessment: fresh / continuity / correction
- strongest direct evidence
- inferred themes
- uncertainty / corrections
- best current read

### For `context-hours/YYYY-MM-DD/HH.md`
These files are meant to be searchable and denser than the digest.
Prefer sections like:
- `# Context hour — YYYY-MM-DD HH:00 UTC`
- `## Windows covered`
- `## Apps / surfaces`
- `## People / entities`
- `## Direct evidence`
- `## Inferences`
- `## Open questions / ambiguity`
- `## Source digests`

Important: preserve concrete searchable strings where useful:
- app names
- chat titles
- company/project names
- keywords like `compounding pharmacy`, `七楼`, `Feedling`, `Andrew Miller`, `OpenClaw`

Do not flood these files with raw OCR dumps. Curate into clean searchable notes.

## Evidence discipline

- Prefer repeated evidence across frames/apps over a single screenshot.
- When screenshot exports mismatch metadata, say so explicitly.
- If visual re-inspection overturns an earlier read, record it as a correction.
- Distinguish clearly between:
  - what the user directly wrote
  - what an assistant/bot wrote
  - what was inferred from surrounding activity
- Treat older `/home/ubuntu/mind/writings/*` as historically informative, not necessarily current ground truth.
- Treat `what-problem-next-5-years.txt` as more current than old writings.

## Granularity guidance

Default recommendation:
- one digest per review pass
- one context-hour file per UTC hour that had meaningful signal

Do not create hourly files for empty/no-signal windows unless there is a strong reason.
If many consecutive passes contain no new activity, reflect that in digest continuity notes and keep `rolling-summary.md` stable.

## Maintenance heuristics

A good update should make future retrieval easier.
Ask after each pass:
- If I searched this topic next week, which file should surface it?
- Did I preserve the key names/terms someone would actually search?
- Did I keep the rolling summary short enough to reread quickly?
- Did I record important uncertainty instead of laundering it away?

## Backfill / reinspection procedure

Use this when you are turning older Fisherman digests into the new layered memory system, or when confidence in an old read is low.

1. Prioritize a few high-signal windows first rather than trying to backfill everything blindly.
   Good candidates are:
   - first clearly active window after a quiet period
   - hours where a thesis visibly sharpened
   - hours with named collaborators / products / markets
   - hours already known to contain metadata/image mismatch risk
2. Re-query the original time range with `query -j --since ... --until ...` instead of trusting the old digest alone.
3. Pick representative frame IDs from each hour and export screenshots with `show <id> -o ...`.
4. Visually inspect the exported images, because this system can strongly mismatch:
   - metadata says WeChat but image is Chrome/article
   - metadata says Lark but image is Telegram
   - metadata says live voice call but image is just the chat log with a call-duration bubble
   - metadata says a specific doc/page but export is a new-tab page or another unrelated surface
5. In the hour note, explicitly record both:
   - what was confirmed by image inspection
   - what remained OCR-only or mismatch-prone
6. Prefer writing one solid hour note with caveats over several brittle hour notes that overclaim.
7. Create/update entity pages during backfill whenever a person/project/motif clearly recurs across multiple hours.

Important rule: if visual export contradicts metadata, do not force the screenshot to match the label. Record the mismatch as part of the memory.

## Recommended future extensions

If the system grows, consider adding:
- `context-entities/` profile pages for recurring people/projects
- backlinks from `rolling-summary.md` into specific digest/hour files
- small frontmatter blocks on digest/hour notes for machine indexing

## Templates

See:
- `templates/digest-template.md`
- `templates/context-hour-template.md`
- `templates/entity-template.md`
- `references/file-layout.md`
