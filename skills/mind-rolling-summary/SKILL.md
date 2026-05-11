---
name: mind-rolling-summary
description: Build and maintain a user-owned Obsidian-style mind directory from Fisherman captures, with rolling summaries, digests, context-hour notes, entity pages, MOCs, and stale-job verification.
version: 1.0.0
license: MIT
---

# Mind Rolling Summary

Use this skill when the user wants Fisherman context turned into durable,
searchable notes instead of a one-off chat answer.

Pair this with `skills/fisherman-cli/SKILL.md`:

- `fisherman-cli` gathers evidence from the active Fisherman context home.
- `mind-rolling-summary` turns that evidence into a maintained wiki.

## Mind Root

Do not assume one hard-coded host path. Pick the mind root in this order:

1. A path the user explicitly names.
2. `$MIND_DIR` if it is set.
3. `~/mind` on the current machine.
4. `/home/ubuntu/mind` only when you are operating an existing Linux-hosted
   mind directory that already uses that path.

In this skill, `<mind-root>` means the selected root.

## Goal

Maintain both:

1. `<mind-root>/rolling-summary.md`: compact high-signal current worldview.
2. Timestamped, searchable pages that preserve evidence, uncertainty, and links.

Recommended layout:

- `rolling-summary.md`: current synthesis and where to look next.
- `INDEX.md`: top-level navigation.
- `fisherman-digests/YYYY-MM-DD_HHMM.md`: one note per review pass.
- `context-hours/YYYY-MM-DD/HH.md`: denser hour-bucket notes.
- `context-entities/*.md`: recurring people, projects, chats, companies, motifs.
- `mocs/*.md`: maps of content.
- `areas/*.md`: durable workstreams.
- `system/*.md`: maintenance docs when useful.

The wiki is a compiled layer between raw Fisherman frames and future reasoning.
Search and update it before going back to raw context unless it is stale or
insufficient.

## When To Use This

Use this for:

- daily or hourly digests
- "what have I been doing lately?"
- long-running self-context maintenance
- entity/workstream memory
- correcting a prior read after better evidence appears
- recurring cron/processor jobs that write durable files

If the user only wants a quick answer and does not ask for durable memory, use
`fisherman query` directly and keep the answer ephemeral.

## Evidence Gathering

Start from the current packaged CLI:

```bash
fisherman doctor
fisherman query --since 2h --limit 50
fisherman transcripts --since 2h --limit 50
```

Use targeted pulls when broad context is noisy:

```bash
fisherman query --since 4h --app "Chrome" --limit 50
fisherman query --since 4h --search "keyword" --limit 50
```

For Cloud/Self-hosted/deputy routing, prefer `--source auto` unless diagnosing:

```bash
fisherman query --source auto --since 2h --limit 50
```

## Pass Types

Label every meaningful review pass as one of:

- `fresh active window`: new context close to the current clock.
- `continuity pass`: recovering known or slightly older context.
- `correction pass`: updating an earlier interpretation because evidence changed.

Before calling a pass fresh, compare the latest Fisherman timestamp to the
current clock. If the newest frame is materially old, say so.

## Operating Procedure

1. Select `<mind-root>` and create missing directories.
2. Inspect existing `rolling-summary.md`, `INDEX.md`, recent digests, and
   relevant entity/workstream pages.
3. Gather Fisherman evidence with the current CLI.
4. Separate direct evidence, inference, and uncertainty.
5. Write a new digest in `fisherman-digests/`.
6. Merge details into relevant `context-hours/YYYY-MM-DD/HH.md` files.
7. Create or update entity/workstream pages when something recurs.
8. Update `rolling-summary.md` only when the high-level picture changes or
   sharpens.
9. Update `INDEX.md` when new durable pages are created.

## Scheduled Maintenance

Fisherman's built-in processor scheduler can run recurring local jobs:

```bash
fisherman processor list --text
fisherman processor schedule add hourly-status status-loop --every 60m --since 60m
fisherman processor schedule list --text
```

`status-loop` is the built-in friend-status processor. For a mind wiki, install
a custom processor manifest that writes to `<mind-root>` and receives context on
stdin:

```bash
fisherman processor install ./mind-writer.processor.json
fisherman processor schedule add hourly-mind mind-writer --every 60m --since 60m --limit 100
```

The daemon runs due schedules automatically while Fisherman is running.
`fisherman processor schedule run-due` is available for manual verification.

## Stale-job Verification

A scheduled job can look enabled while the durable wiki is stale. Verify actual
files, not just scheduler metadata.

Check:

```bash
fisherman processor schedule list --text
fisherman query --limit 5
```

Then compare:

- newest Fisherman frame timestamp
- newest file under `<mind-root>/fisherman-digests/`
- `Last updated:` in `<mind-root>/rolling-summary.md`
- processor `last=` and any `last_error`

If Fisherman has newer context but the mind files lag, do a manual catch-up pass
and tighten the scheduled processor prompt/manifest.

## Writing Rules

For `rolling-summary.md`:

- Keep it compact enough to reread quickly.
- Prefer stable themes, active workstreams, recurring people, repeated
  frictions, and where-to-look-next.
- Use wikilinks for recurring concepts when helpful.
- Do not dump raw OCR.

For `fisherman-digests/*.md`:

- Include digest timestamp, reviewed window, current clock, pass type, direct
  evidence, inferred themes, uncertainty/corrections, and best current read.

For `context-hours/YYYY-MM-DD/HH.md`:

- Preserve searchable names, app titles, project names, chat names, URLs or
  domains when useful.
- Keep direct evidence separate from inference.
- Link source digests and related entity/workstream pages.

For `context-entities/*.md`:

- Capture why the entity matters, current best read, open questions, and links
  to supporting digests/hour notes.

## Evidence Discipline

- Prefer repeated evidence across frames/apps over one frame.
- Treat OCR, app/window metadata, and screenshots as fallible.
- If a screenshot contradicts metadata, record the mismatch.
- If visual reinspection overturns an earlier read, write a correction pass.
- Distinguish what the user wrote from what an assistant/bot wrote.
- Do not launder uncertainty into certainty.
- Preserve exact searchable strings when useful, but do not flood the wiki with
  raw OCR.

## Backfill Procedure

Use this when turning older Fisherman exports/digests into the layered wiki.

1. Prioritize high-signal windows first.
2. Re-query the original time range instead of trusting old summaries.
3. Create one solid hour note with caveats rather than many brittle notes.
4. Create or update entity pages only when a person/project/motif clearly
   recurs.
5. Record what was confirmed and what remained OCR-only or mismatch-prone.

## Templates And References

Use these files as templates and deeper design references:

- `templates/digest-template.md`
- `templates/context-hour-template.md`
- `templates/entity-template.md`
- `templates/area-template.md`
- `references/file-layout.md`
- `references/obsidian-native-llm-wiki.md`
