---
name: mind-rolling-summary
description: Build and maintain /home/ubuntu/mind as an Obsidian-native compiled wiki from Fisherman captures — with rolling summary, digests, context-hours, entity pages, MOCs, area pages, and system docs for retrieval/search.
version: 0.1.0
author: Hermes Agent
license: MIT
---

# Mind Rolling Summary

Use this skill when you need to turn Fisherman screen-capture context into durable, searchable notes under `/home/ubuntu/mind`.

This skill is meant to work together with the `fisherman-deputy-remote-access` skill:
- `fisherman-deputy-remote-access` = how to inspect the user's live captured activity reliably through deputy access
- `mind-rolling-summary` = how to transform that evidence into a layered memory system

A newer recurring use case is **tacit-knowledge / cognition distillation** from screen data. In that mode, the goal is not only to summarize what happened, but to synthesize how the user thinks, revisits ideas, iterates, selects proof surfaces, and generates transferable tacit knowledge for onboarding interns or future agents.

## Goal

Maintain both:
1. a compact high-signal running brief at `/home/ubuntu/mind/rolling-summary.md`
2. richer timestamped markdown logs that preserve more detailed context for later search and reconstruction
3. when the task is historical synthesis / intern onboarding / cognition distillation, a durable cross-cutting layer under `/home/ubuntu/mind/distillation/`

The design principle is **layered memory**:
- `rolling-summary.md` = current worldview / stable themes / most important recency signal
- `fisherman-digests/*.md` = timestamped narrative passes (one per analysis pass)
- `context-hours/YYYY-MM-DD/HH.md` = denser searchable hour-bucket notes for reconstruction and retrieval
- `context-entities/*.md` = flexible entity/topic pages for recurring people, companies, projects, chats, or motifs
- `distillation/*.md` = higher-order tacit-knowledge layer distilled from Fisherman evidence: cognitive patterns, work behavior patterns, return patterns, proof surfaces, mode transitions, failure/correction patterns, and intern-onboarding-grade heuristics
- `mocs/*.md` = map-of-content pages that gather and route related pages
- `areas/*.md` = durable workstream/theme pages that sit between entity pages and the top-level rolling summary
- `system/*.md` = maintenance docs that define retrieval order, search behavior, and wiki operating rules
- `INDEX.md` = top-level map of the entire `/home/ubuntu/mind` folder, including uploaded writings, source docs, syntheses, Fisherman-derived memory, and distillation artifacts
 top-level map of the entire `/home/ubuntu/mind` folder, including uploaded writings, source docs, syntheses, Fisherman-derived memory, and distillation artifacts

This should increasingly be treated as an **Obsidian-native LLM wiki**, not just a logging system:
- the wiki is a compiled layer between raw sources and future reasoning
- Hermes should search the compiled layer before going back to raw source files
- future structure should move toward clearer separation of **raw sources**, **compiled wiki**, and **system/maintenance docs**
- use wikilinks, frontmatter, MOCs (maps of content), and stable page types wherever useful
- when deciding whether to add a new page or update an old one, prefer maintaining the wiki as a coherent artifact over appending disconnected summaries
- in tacit-knowledge mode, do not stop at timeline recaps; synthesize the user's cognitive patterns, return patterns, decision style, proof surfaces, failure/correction patterns, collaborator usage, and intern-transferable heuristics

## Canonical file layout

A useful newer extension is a dedicated distillation layer under `/home/ubuntu/mind/distillation/`, with pages such as:
- `README.md`
- `core-theses.md`
- `how-sxysun-generates-knowledge.md`
- `cognitive-patterns.md`
- `work-behavior-patterns.md`
- `decision-style.md`
- `project-evolution-map.md`
- `collaborator-usage-patterns.md`
- `recurring-questions.md`
- `intern-onboarding-guide.md`
- `methodology-and-confidence.md`
- optionally more explicit tacit-knowledge pages like `signature-cognitive-loops.md`, `return-patterns.md`, `proof-surfaces.md`, and `failure-patterns-and-corrections.md`

These should be grounded in actual Fisherman evidence, not generic self-help abstractions. Use concrete Fisherman/context-hour/digest examples whenever possible.

See also:
- `references/file-layout.md`
- `references/obsidian-native-llm-wiki.md`

Under `/home/ubuntu/mind`:

- `rolling-summary.md`
  - current high-signal synthesis
  - should be readable in a few minutes
- `INDEX.md`
  - top-level map of the whole `mind/` folder
  - should index source writings, anchor documents, syntheses, rolling observational memory, and any distillation layer
- `distillation/`
  - cross-cutting cognition / onboarding / tacit-knowledge layer compiled from many Fisherman hours and digests
  - use for intern-onboarding-grade pages that answer how the user thinks, how projects translate, and what recurring heuristics matter
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

### Optional automation / scheduled maintenance
If the user wants ongoing passive maintenance rather than one-off manual reviews, the agent should offer to create or verify a recurring scheduled job.

Recommended generic behavior:
- schedule the job at a reasonable cadence (hourly is a good default for active use)
- ensure the recurring job loads both the Fisherman evidence-gathering instructions and this rolling-summary skill
- if the user also has `fisherman-rokid`, and wants the rolling summary to reflect embodied activity rather than only desktop traces, also load the `fisherman-rokid-inspection` and `fisherman-rokid-server-ops` skills
- in that combined mode, explicitly inspect fisherman-rokid directly (runtime health/logs, recent captured images, recent audio/transcript outputs, and server-side artifacts under `transcripts/`, `audio_wav/`, and `audio_processed/` when present) instead of relying only on whatever desktop traces happen to appear in Fisherman
- combine both evidence families into one reconstruction of what the user did, and label which parts came from desktop Fisherman evidence versus direct fisherman-rokid artifacts when that distinction matters
- ensure the scheduled prompt is self-contained and updates the chosen knowledge base autonomously
- make the job write durable files, not just emit ephemeral chat summaries
- preserve uncertainty, correction passes, and screenshot/OCR mismatch notes in the durable memory layer

Because scheduler infrastructure differs by agent/runtime, do not assume a specific cron implementation. Use whatever recurring-job mechanism the current agent platform supports.

### Cron verification / stale-job troubleshooting
A scheduled job can look healthy while the actual mind layer is stale.

Use this verification pattern whenever the user asks whether the rolling summary has really been running, or when the latest Fisherman activity is newer than the latest digest/summary timestamps:
1. Inspect the scheduler metadata itself (enabled state, `last_run_at`, `last_status`, `next_run_at`).
   - In the current Hermes local setup, a concrete place to check is `~/.hermes/cron/jobs.json` (for example `/home/ubuntu/.hermes/cron/jobs.json`).
   - Compare the relevant job's `last_run_at` / `last_status` / `next_run_at` against the durable mind files instead of trusting `ok` at face value.
2. Inspect the durable outputs on disk, not just job status:
   - latest files under the scheduler output directory
   - latest files under `/home/ubuntu/mind/fisherman-digests/`
   - `Last updated:` in `/home/ubuntu/mind/rolling-summary.md`
   - importantly, read the latest scheduler output files themselves, not just their timestamps
3. Compare those timestamps to the newest available Fisherman frame timestamp.
4. If the job is enabled but the mind files lag behind the newest frames, treat that as a real stale-memory condition even if the last scheduler status says `ok`.
5. A concrete failure mode to watch for: the cron output files can repeatedly contain only **`[SILENT]`** while newer Fisherman frames already exist and the durable mind layer has not advanced. Treat that as a stale-memory bug / prompt-quality problem, not as proof that nothing happened.
5. Before assuming the cron is malfunctioning, verify whether Fisherman itself is actually stale:
   - inspect the newest frame timestamp directly (for example with a direct DB query or `query -j --limit 1`)
   - compare that newest frame timestamp against **today / last 24h** counts, not just the cron timestamps
   - if there are genuinely **zero new frames** since the last durable mind update, repeated cron outputs of `[SILENT]` are expected behavior rather than a summary-layer bug
   - in other words: `[SILENT] + no newer frames` = healthy quiet period; `[SILENT] + newer frames exist` = stale-memory bug
6. Manually run a catch-up pass immediately when newer frames do exist:
   - write the missing digest/hour/entity/summary/index updates to `/home/ubuntu/mind`
   - then verify that the new files actually exist on disk
7. After the manual catch-up, tighten the recurring job prompt so it explicitly:
   - performs catch-up work when durable notes are behind the newest frames
   - only emits `[SILENT]` when there is genuinely nothing new and no files were changed
   - reports which durable files were updated when it does make changes
7. Re-check the next scheduled run after the update.

Important practical lesson: a cron job can be `enabled` and `ok` yet still fail to advance the durable memory layer if the prompt is too willing to return `[SILENT]` or does not explicitly compare Fisherman recency against the on-disk mind files.

### When cron looks stuck even after auth is repaired
Another real failure mode: the cron job can remain effectively stuck even after provider auth has been repaired.

Concrete symptoms:
- `cronjob(action='run', job_id=...)` is accepted
- `jobs.json` still shows the old `last_run_at` / `last_status`
- no new file appears under `~/.hermes/cron/output/<job_id>/`
- a direct due-job check still shows the job as due
- a local test call to the scheduler tick returns `0`
- trying to lock `~/.hermes/cron/.tick.lock` reports it is already held

Best interpretation:
- the scheduler tick lock is being held by an old/stuck gateway cron thread or process, so due jobs are not actually executing even though the gateway appears alive

Recommended recovery sequence:
1. Verify the job is still due and that no fresh output file was written.
2. Check whether `~/.hermes/cron/.tick.lock` is currently locked.
3. If it is locked while no job output is advancing, suspect a stuck gateway scheduler path.
4. Restart the Hermes gateway cleanly (`hermes gateway stop`, then normal gateway startup).
5. Re-check `jobs.json` for new `last_run_at`, `last_status`, and `next_run_at`.
6. Only after the gateway/tick path is healthy again should you trust the cron status.

This matters because a cron can fail in at least three distinct ways:
- stale prompt logic (`[SILENT]` too eagerly)
- provider auth failure (for example Codex 401 refresh errors)
- scheduler/tick-lock failure where accepted runs never actually execute

### If the user asks for a "daily digest" of observed work
- a manual `cronjob run` can be accepted and `next_run_at` can move forward, yet `last_run_at` / output files do not change
- a direct scheduler tick can report `executed=0` even though the job is visibly due
- this can happen when the gateway/scheduler process is still holding `~/.hermes/cron/.tick.lock`

Recovery pattern:
1. Check that the job is actually due in code/runtime terms, not just by eyeballing `jobs.json`.
2. If a manual tick still executes `0`, test whether `~/.hermes/cron/.tick.lock` is currently blocked.
3. If the lock is blocked, assume the running gateway owns the scheduler lock.
4. Do **not** start a second gateway blindly; Telegram can fail with a local poller/token conflict if another gateway instance is still alive or draining.
5. Prefer a clean restart sequence:
   - `hermes gateway stop`
   - verify the old gateway is really gone / no longer draining
   - then start one gateway again (`hermes gateway run` or the normal service path)
6. After recovery, verify success by checking:
   - `last_run_at` advances
   - `last_status` becomes `ok`
   - `next_run_at` is recomputed normally
   - the expected cron output file appears

Treat this as distinct from provider-auth failure: sometimes the provider problem is real, but sometimes the later symptom is a stuck scheduler lock rather than a still-broken Fisherman or GNOMY prompt.

Another concrete failure mode now matters in the Hermes local stack: the cron job can remain scheduled while the underlying model/provider auth for cron-run sessions is broken (for example `RuntimeError: Codex token refresh failed with status 401.` in `~/.hermes/cron/jobs.json` and the per-run output markdown). In that state, an interactive chat session may still work while scheduled runs keep failing.

When debugging this case:
- inspect `~/.hermes/cron/jobs.json` for `last_error`, not just `last_status`
- inspect the newest files under `~/.hermes/cron/output/<job_id>/` and read the tail to confirm whether the run ended in an auth/runtime error versus `[SILENT]`
- if you manually trigger the job, verify success by checking that **all three** moved forward:
  - `last_run_at`
  - a new output file timestamp under `~/.hermes/cron/output/<job_id>/`
  - if applicable, the durable files under `/home/ubuntu/mind`
7. Re-check the next scheduled run after the update.

Important practical lesson: a cron job can be `enabled` and `ok` yet still fail to advance the durable memory layer if the prompt is too willing to return `[SILENT]` or does not explicitly compare Fisherman recency against the on-disk mind files.

### When cron looks stuck even after auth is repaired
Another real failure mode: the cron job can remain effectively stuck even after provider auth has been repaired.

Concrete symptoms:
- `cronjob(action='run', job_id=...)` is accepted
- `jobs.json` still shows the old `last_run_at` / `last_status`
- no new file appears under `~/.hermes/cron/output/<job_id>/`
- a direct due-job check still shows the job as due
- a local test call to the scheduler tick returns `0`
- trying to lock `~/.hermes/cron/.tick.lock` reports it is already held

Best interpretation:
- the scheduler tick lock is being held by an old/stuck gateway cron thread or process, so due jobs are not actually executing even though the gateway appears alive

Recommended recovery sequence:
1. Verify the job is still due and that no fresh output file was written.
2. Check whether `~/.hermes/cron/.tick.lock` is currently locked.
3. If it is locked while no job output is advancing, suspect a stuck gateway scheduler path.
4. Restart the Hermes gateway cleanly (`hermes gateway stop`, then normal gateway startup).
5. Re-check `jobs.json` for new `last_run_at`, `last_status`, and `next_run_at`.
6. Only after the gateway/tick path is healthy again should you trust the cron status.

This matters because a cron can fail in at least three distinct ways:
- stale prompt logic (`[SILENT]` too eagerly)
- provider auth failure (for example Codex 401 refresh errors)
- scheduler/tick-lock failure where accepted runs never actually execute

### If the user asks for a "daily digest" of observed work
Default to the rolling-summary system rather than producing only an ephemeral chat summary.

Meaning:
- create a new `fisherman-digests/*.md` pass note
- update the relevant `context-hours/YYYY-MM-DD/HH.md` files
- update `rolling-summary.md` if the high-level read sharpened
- update `INDEX.md`
- optionally give a concise chat summary too, but the durable mind-layer update is the default

If you temporarily answer with a one-off digest first and the user pushes back (for example: "use the rolling summary skill"), treat that as a correction and fold the same synthesis into the full rolling-memory structure immediately.

### Update `fisherman-digests/*.md` every review pass
Create a new digest whenever you do a meaningful Fisherman review, whether the pass is:
- a fresh active window
- a continuity / clarification pass
- a correction pass caused by visual re-inspection

Important live-burst extension rule:
- if you already wrote a digest/hour/summary update and a later refresh shows **newer same-burst frames**, do **another incremental digest** instead of silently folding the newer tail into chat only.
- In practice, active Fisherman bursts can keep extending in 2-5 minute increments while you are still inspecting screenshots or writing mind files.
- Prefer **multiple short catch-up digests** over leaving the durable layer frozen at the first pass timestamp.
- A smaller but still important variant now matters too: the newer same-burst frontier may advance only by **seconds** or by a tiny tail while adding **little or no new semantic center**. If the durable layer currently stops at an older exact raw frontier, do **not** return `[SILENT]` just because the story feels unchanged.
- In that case, write a short **continuity / clarification digest** that:
  - advances the exact raw frontier on disk,
  - explicitly says the semantic center is unchanged,
  - and records any stronger evidence-discipline lesson from the tiny tail (for example: the newest export path became even more mismatch-prone, the newest frames were text-only, or the latest minute is safer at cluster level than frame level).
- Good durable phrasing in this variant is: **"exact frontier advanced slightly, semantic center unchanged"**.
- A high-value concrete variant: the later same-burst tail can **materially correct an optimistic earlier implementation read**. For example, one pass may show a feature as installed/implemented (battery optimization, new APK, live ingest), but a 2-3 minute-later monitoring tail can reveal that the **old build is still the active sender**, that **audio is still absent**, or that the system is only **partially working**. In those cases, do not just append the new timestamp — write the incremental digest as a **correction of operational state**, and also update the relevant entity/rolling-summary language so the durable layer does not overstate success.
- Another concrete same-burst terminal/deploy variant now matters too: a checked frame can first show a real **health-ok / cutover-in-progress** state, while a 2-5 minute-later refresh from the same workbench flips into a real **restart-loop / rollback-running** state. Treat that as a genuine **same-burst operational correction**, not as noise or contradiction. Preserve both checkpoints in the digest/hour note, but make the later rollback/failure state canonical for recency and entity/rolling-summary language unless an even newer frame cleanly confirms recovery.
- Another high-value variant: a late same-burst terminal/Claude tail can upgrade a state from **"design/spec next"** to **"design/spec artifact completed"** within minutes. If the newer frame visibly shows checklist/task status, a written file path, or completed-task counters (for example `1 done, 0 open`, `Write ... design doc` marked complete, or a concrete doc path like `docs/DESIGN_E2E.md`), treat that as a real semantic update — not just another timestamp bump. Update the digest/hour/entity/rolling-summary language from **planned** to **done** so the durable layer does not keep narrating an already-finished artifact as still pending.
- Another newer same-burst variant matters for **terminal-heavy implementation windows with browser-label leakage**: you may write a first digest from visually checked frames, then a 2-5 minute-later tail lands mostly as **OCR-only terminal continuity** while browser metadata briefly claims a leisure or unrelated endpoint (for example **Chrome / YouTube / Futurama**) or a loose repo-URL tail. If a checked representative export still collapses back into the same technical workbench, keep the semantic center on the **implementation workbench**, not the browser label.
- A newer mixed-browser/research variant matters too: a burst can first land on a stronger checked semantic anchor such as a real **Claude/ChatGPT research or planning surface**, then the exact newest minute can fall back into lower-weight **Bilibili/YouTube/homepage/recommendation-feed** browsing. In that case, do **not** let the newest browser/homepage timestamp replace the better earlier semantic anchor just because it is later. Good durable phrasing is: **exact frontier advanced slightly, semantic center unchanged**.
- A stronger concrete caution in this variant: if the newest checked endpoint is a generic **homepage / recommendation / discovery** state rather than a focused document, chat, terminal, or watch page, store it as **low-weight browser carryover** and keep the earlier stronger frame as canonical for the hour's semantic center.
- In those catch-up passes, even without new exported screenshots, the later OCR-only terminal tail can still add **material operational truth** such as: a rerun of a setup check stayed green, the allowed model/provider set is narrower than expected, an API requires an extra mandatory field (for example a `systemPrompt` / `Instructions are required` constraint), or the user provided a fresh credential/prompt artifact that clearly unblocks the next step.
- Preserve those as a **same-burst micro-digest** if they materially sharpen the forward path, but redact any secret/token values and label the new facts as **OCR-level catch-up evidence** when they were not visually re-confirmed by a fresh export.
- A newer concrete same-burst auth-debug variant now matters too: you may first land a strong **terminal diagnosis** (for example proving one auth attempt really succeeded end-to-end while another really failed via timeout/expiry, and identifying a UI/state-reconciliation gap), then only a few minutes later the desktop can visibly swing back into a real **frontend rerun** of that exact flow.
- Practical shape: a later checked **Chrome / `Connect TikTok`** or similar auth page can show a fresh QR flow with states like **`pending` -> `ready · processing`**, a live QR code, and a new **copy-all diagnostics** affordance, while a nearby chat app or WeChat frame is only low-signal continuity.
- In that situation, do **not** stop the durable layer at the terminal postmortem. Write a short **same-burst micro-digest** that preserves the stronger chronology: **backend truth established -> frontend/diagnostic fix explained -> fresh live frontend rerun launched**.
- Treat that rerun as semantically meaningful even if it is only 2-3 minutes newer than the prior digest, because it distinguishes **abstract diagnosis** from **active operational retry**.

### Update `context-hours/YYYY-MM-DD/HH.md` when the pass contains usable detail
If the pass yields meaningful app/chat/browser detail, merge the evidence into the relevant UTC hour file(s).

### Continuity/correction pass when the evidence frontier is unchanged but the durable layer is behind
A practical maintenance case now matters: a later cron run may find **no newer Fisherman frames** and **no newer direct Rokid artifacts**, yet the wiki can still be stale because only the latest digest/hour was updated previously while relevant **entity pages**, **distillation pages**, or `INDEX.md` still describe an older frontier.

In that case:
1. Treat the run as a real **continuity / correction pass**, not `[SILENT]`.
2. Verify explicitly that the evidence frontier is unchanged:
   - newest desktop frame timestamp
   - newest direct Rokid artifact frontier
3. Compare that unchanged frontier against:
   - the tops of relevant `context-entities/*.md`
   - relevant `distillation/*.md`
   - `INDEX.md`
4. If those layers still lag behind the latest already-grounded digest/hour, update them even without new raw evidence.
5. Write a new digest that says clearly:
   - there was **no newer evidence**,
   - the pass value was **durable-layer catch-up / consistency repair**,
   - and which files were advanced to match the existing frontier.
6. A newer high-value variant: if the stale layer is specifically the **distillation/cognition layer**, do **direct reinspection of a small representative frame set from the latest already-grounded fresh window** before writing the catch-up.
   - Good pattern: re-open 3-5 checked frames from the latest meaningful burst, visually confirm the actual foreground again, and ask whether the durable layer has fully captured the **cognitive/proof lesson** of that burst.
   - This is especially useful when the timeline layer already has the raw facts, but the distillation layer still understates **how** the user made the idea real (for example: doctrine immediately converted into hardening, green validation, or an honest push/review gate).
   - In that variant, keep the pass labeled as **no newer evidence / distillation-layer catch-up**, but preserve the stronger lesson as newly distilled because it is grounded in the rechecked images rather than guessed from old OCR alone.
7. If you do this kind of same-window reinspection, record it explicitly in the audit trail:
   - which already-known frame IDs were re-opened
   - how many images were visually rechecked
   - what new durable lesson was extracted from the same evidence family
   - and that the semantic gain came from **re-grounding the existing window**, not from fresher activity.

Examples:
- the latest digest/hour exists, but `context-entities/Feedling.md` or `context-entities/Hivemind.md` still stops at an older checkpoint
- a fresh GTM/compliance or proof-surface lesson belongs in `distillation/proof-surfaces.md`, but only the digest/hour layer was updated earlier
- `INDEX.md` still describes older entity/frontier blurbs even though the newest digest already changed the durable read
- `rolling-summary.md` itself still stops at an older `last_updated` / top `## Recency notes` block even though later digests already established that the evidence frontier did **not** move

A newer practical maintenance case now matters too: the **latest digest can already be correct while the top-level summary/index recency hygiene is stale**.
- In practice this can happen when a same-day continuity/correction digest already recorded **no newer evidence**, but `rolling-summary.md` still makes an older fresh window look like the latest review pass, or `INDEX.md` still points at the older digest list.
- A closely related variant now matters for **historical archive-deepening** work: a newer historical digest can already exist on disk and be indexed under `fisherman-digests/` / `INDEX.md`, while `rolling-summary.md` still claims an older archive-deepening pass is the latest one.
- In that case, do a narrow catch-up pass that:
  1. rechecks the current Fisherman and Rokid frontiers,
  2. visually reopens 1-2 representative latest frames from the already-grounded cluster if needed,
  3. writes a short new digest explaining that the frontier is unchanged,
  4. updates `rolling-summary.md` so it cleanly distinguishes **latest review pass**, **latest fresh active window**, and when relevant **latest historical archive-deepening pass**,
  5. updates `INDEX.md` so the newest digest lists and summary timestamp agree.
- This is worth doing even when no entity page or distillation page changes, because otherwise the compiled wiki can misstate freshness at the very top layer.

Do **not** narrate this as fresh activity. Store it as **evidence frontier unchanged, durable interpretation layer corrected/caught up**.

Examples:
- a pass at `08:23 UTC` reviewing `07:16–07:56 UTC` should update `context-hours/2026-04-03/07.md`

Examples:
- a pass at `08:23 UTC` reviewing `07:16–07:56 UTC` should update `context-hours/2026-04-03/07.md`
- if evidence spans two hours, update both hour files
- if the current durable frontier already stops mid-hour (for example inside `22.md`) and a later pass extends the rest of that hour before opening a new `23.md` cluster, do both in one maintenance run:
  - extend the older hour note through the real end of the earlier hour, and
  - create/update the new hour note for the later hour
  This matters when the later hour cools into browser/video continuity: the older hour may still contain the stronger late technical continuation, while the newer hour carries the exact frontier and handoff.

Also valid: create a `context-hours/...` note for a mostly empty later hour when doing so preserves continuity or epistemic state, for example:
- confirming there was no newer substantive activity after an important burst
- recording a correction caused by visual reinspection
- marking a boundary between two meaningful bursts
- preventing false recency when a later pass is only continuity/clarification rather than fresh movement
- capturing a **cross-midnight boundary tail** where the raw desktop frontier advances into the next UTC day but the semantic center still belongs to the denser pre-midnight cluster

A newer practical boundary-crossing rule:
- if the desktop frontier crosses into a new UTC day/hour (for example **23:59 -> 00:01 UTC**) with only a short low-weight browser/admin tail, still create the new `context-hours/YYYY-MM-DD/00.md` note so the frontier is represented on disk
- but do **not** let that tiny next-day tail overwrite the stronger semantic center from the previous hour
- good durable phrasing is: the new `00.md` note records **browser/admin or cooldown continuity**, while the prior `23.md` keeps the real center of gravity (for example the denser Feedling/Hivemind/GTM workbench)
- this avoids a retrieval failure where the newest timestamp exists on disk but later reasoning mistakenly treats the cross-midnight tail as the main fresh work window
- a newer cross-hour continuation variant matters too: the new UTC hour can open with only a brief low-weight **browser/leisure carryover** (for example **YouTube / Amazon / X**) from the previous burst, while a few minutes later the foreground clearly returns to the prior **technical workbench** (for example a resumed Terminal / Claude / repo cleanup-verification lane). In that case, still create the new `context-hours/YYYY-MM-DD/HH.md` note for the hour boundary, but do **not** anchor the semantic center of the new hour on the initial cooldown page if later checked frames in the same hour restore the implementation foreground. Good durable phrasing is: **the hour opened with browser carryover, then resumed the technical center by <later time>**, so retrieval does not misread the new hour as primarily leisure/admin drift.
- another newer cross-hour variant matters too: the raw frontier can cross into a new UTC hour while the **technical center is still the same prior-burst terminal/workbench**, but the clean latest visible endpoint becomes a real **human coordination / collaborator-planning** chat (for example a Telegram DM about travel, scheduling, or next-step planning). In that case, still create the new `context-hours/YYYY-MM-DD/HH.md` note for the boundary hour, preserve the prior technical burst as the **semantic center**, and write the chat as a **human-facing cap / planning endpoint** rather than a brand-new project handoff. Good durable phrasing is: **the same technical burst carried forward, then the clean latest foreground shifted into collaborator-planning continuity by <later time>**.

### Update `rolling-summary.md` only when the high-level picture changes or sharpens
Do not rewrite it for every small OCR fragment.
Update it when:
- a new theme emerges
- confidence in an existing theme materially changes
- a collaborator / project / wedge becomes recurring enough to matter
- a correction changes the best current read
- a fresh activity window meaningfully shifts the recency section

Important recency discipline:
- If the newest pass is a **continuity / clarification / correction pass** with **no newer frames than the last fresh burst**, do **not** accidentally imply that the latest *fresh active window* moved forward.
- In that case, `rolling-summary.md` should explicitly preserve both facts:
  - the timestamp of the **latest review pass**, and
  - the timestamp of the **latest fresh active window**.
- This avoids a subtle retrieval failure where a correction pass looks like a newer burst and later reasoning overstates freshness.
- A newer cross-source variant matters when combining **desktop Fisherman** with **direct fisherman-rokid** inspection: you can have **newer Rokid runtime timestamps** (fresh frames/audio, live `/health`, active service) while still having **no newer meaningful desktop activity**. In that case, do **not** upgrade the latest fresh active window just because Rokid has newer files.
- A newer adjacent-stack variant matters too: desktop Fisherman can show a visually clear browser/terminal workbench for an adjacent embodied-agent branch (for example a live debug panel, stream viewer, OTA/log page, or repo-local terminal for a Rokid-adjacent project) while the **direct `fisherman-rokid` artifact frontier itself is still stale**. Treat those browser/terminal surfaces as **desktop evidence about adjacent implementation/debug work**, not as proof that direct Rokid frames/audio/transcripts advanced.
- Preserve three separate facts when needed:
  - the timestamp of the **latest review pass**
  - the timestamp of the **latest fresh active desktop/work window**
  - the timestamp of the **latest direct Rokid runtime evidence**
- In active `fisherman-rokid` continuity windows, also preserve the **sub-layer timestamps** when they materially diverge:
  - latest **raw audio** timestamp
  - latest **direct frame** timestamp
  - latest **processed/transcript artifact** timestamp
- This matters because the Rokid path can stay live while those layers drift apart sharply: fresh audio may keep landing, the newest frame may lag behind, and processed/transcript outputs may stall much earlier. In those cases, summarize the state as **runtime continuity + processing lag** rather than pretending the newest processed artifact reflects the newest minute.
- If the newer Rokid evidence is only **dark / underexposed / near-silent ambient continuity**, write it as newer **runtime continuity** rather than as a fresh activity burst.
- When you add a **newer fresh active window** to the top of `## Recency notes`, also demote the previously topmost entry so it no longer says things like **"is now the latest review pass"** or **"is now the latest fresh active window"**. Rewrite it as a **previous / earlier fresh window** entry instead of leaving contradictory `latest` claims stacked in the same section.
- Practical rule: after inserting a new recency block, scan the next one or two older blocks and normalize any stale `latest` wording immediately.

### Update `INDEX.md` whenever a new digest, hour file, or entity page is created
Keep it useful as a navigation layer for the whole `mind/` folder, not a full database dump.

## Operating procedure

1. Load and follow `fisherman-deputy-remote-access` first for evidence gathering from the live deputy path.
2. Determine the pass type:
   - `fresh active window`
   - `continuity / clarification pass`
   - `correction pass`
   - `historical distillation / tacit-knowledge pass`
3. Separate:
   - direct evidence
   - conservative inference
   - uncertainty / contradictions
4. Write a new digest in `fisherman-digests/`.
5. Merge detailed evidence into the relevant `context-hours/YYYY-MM-DD/HH.md` file(s).
6. If a person/project/chat/topic is clearly recurring, create or update a `context-entities/*.md` page.
   - Do not treat entity updates as optional only for first creation. If a fresh pass materially sharpens an already-known entity (for example a project now has a clearer role, an infra surface becomes operationally real, or a collaborator/project changes from background continuity to foreground evidence), update that existing entity page in the same maintenance run.
   - Typical examples: a project page like `teleport.md` when branding/copy becomes more central or changes form; an infra/project page like `openclaw.md` when it shifts from abstract reference point to real operational host; or a system page like `hermes.md` when failures, gateway/runtime behavior, or memory/cron responsibilities become newly visible.
   - Also create/update entity pages for **recurring synthesized systems**, not just apps or people. If something like **GNOMY** stops being a passive bundle and becomes a live operational question (for example: daily diff usefulness, interview-question generation, richer personal-context ingestion, comparison against adjacent repo/archive tooling), give it its own entity page so later retrieval does not bury it inside only digests.
7. If the pass is a tacit-knowledge / cognition-distillation pass, also create or update relevant `distillation/*.md` pages.
   - Prioritize pages that explain how the user thinks and works, not just what happened.
   - Ground them in actual Fisherman/context-hour/digest evidence.
   - Particularly valuable page types include: core theses, cognitive patterns, work-behavior patterns, decision style, project evolution, collaborator usage, recurring questions, proof surfaces, failure/correction patterns, signature cognitive loops, return patterns, and an intern-onboarding guide.
8. Update `rolling-summary.md` if the high-signal picture changed.
9. Update `INDEX.md` so the new files are discoverable.
   - If you create a new major synthesis bundle under `/home/ubuntu/mind` (for example a new GNOMY revision folder, distillation layer, or other top-level interpretation package), also add it to the relevant section of `INDEX.md` so it does not become an orphaned artifact.

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

Treat it as a compact synthesis page in a wiki, not a diary. Over time it should become more Obsidian-native:
- use frontmatter when practical
- add wikilinks for recurring entities/projects/workstreams
- avoid duplicating lower-level detail that belongs in entity pages or timeline pages
- link outward to the most relevant MOCs or entity pages once those exist

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

If the same UTC hour is extended by later micro-passes, keep the hour note cumulative and make sure `## Source digests` lists **all relevant same-hour digest files**, not just the first one.

Treat each hour note as a timeline page in a larger wiki:
- preserve exact searchable strings
- link recurring entities/topics with wikilinks when possible
- keep direct evidence separate from inference
- avoid raw OCR dumps unless absolutely necessary
- if an hour clearly belongs to a larger workstream, link that workstream or entity page explicitly

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
- When a Telegram/self-bot/assistant lane claims that a new artifact bundle or file set was created (for example a simulation/world-pack bundle under `/home/ubuntu/mind`), do **not** store that as a completed artifact from OCR/chat evidence alone. Verify the files on disk first with `search_files` / `read_file` and only then upgrade the claim from **OCR-level continuity** to a real durable artifact. If the files are missing, preserve the lane as **attempted / claimed / unverified** rather than silently narrating it as done.
- A newer closely related variant matters for **bot-reported automation / cron setup**. If a Telegram bot/assistant message claims that a scheduled workflow was created (for example naming a spec file, report path, cron job name, job id, or schedule), do **not** store that as fully real from chat text alone. Verify the concrete durable anchors on disk first — e.g. the named spec/report file under `/home/ubuntu/mind` and the job metadata under `~/.hermes/cron/jobs.json` — and only then upgrade the lane from **bot-claimed setup continuity** to a partially or fully confirmed durable artifact. If only some anchors verify, preserve that distinction explicitly (`spec file confirmed`, `job exists`, `future outputs not yet checked`).- For **sensitive ops surfaces** like password managers, auth flows, recovery prompts, or credential stores, preserve only the **type of activity** and a few harmless anchors (for example app name, category, non-secret item names if needed). Do **not** copy secrets, recovery data, passwords, or full credential contents into the durable mind layer.
- A newer practical follow-through now matters for **bot-reported automation / cron setup** too: **job existence is still not the same as successful recurring output**. If the latest meaningful lane depends on a scheduled workflow having actually run (for example a daily simulation, digest, or comparison report), verify at least one real execution artifact before narrating it as operationalized. Good anchors are:
  - the expected output file exists on disk (for example `/home/ubuntu/mind/daily-xyn-simulations/YYYY-MM-DD.md`)
  - `~/.hermes/cron/jobs.json` shows a fresh `last_run_at` and `last_status: ok`
  - a matching per-run output file exists under `~/.hermes/cron/output/<job_id>/`
- Durable wording should distinguish **setup confirmed** from **first run confirmed**. Example: `spec file confirmed + job exists` is weaker than `first scheduled report exists on disk; cron last_run_at/status also verify successful execution`. Only the latter justifies upgrading the lane from planned automation into a **real recurring compiled artifact**.
- A newer concrete credential-manager variant matters too: if a same-hour cluster includes a real **1Password** or similar vault surface during account setup / funding / broker / infra work, do **not** export that screenshot just to prove the obvious. Preserve only the harmless operational fact — e.g. **`credential-manager surface used while setting up or updating an IBKR/login item`** — and keep the surrounding product/broker/admin workflow as the main visible anchor.
- A newer concrete variant matters for live Fisherman maintenance too: if a frame visibly contains a freshly created **API token / bearer token / credential string** (for example a Cloudflare API token shown in Chrome or pasted into Terminal), do **not** export or store the raw secret in any digest/hour/entity/rolling-summary text. Preserve only the harmless operational fact — e.g. **`token appears to have been created/provided; implementation likely unblocked`** — and explicitly note that the credential itself was intentionally omitted.
- A closely related terminal-heavy variant now matters too: a same-burst **operator / Hivemind / infra** tail can carry live **room URIs, bearer tokens, agent IDs, request IDs, Wi-Fi passwords, hotspot credentials, or other copy-paste-ready command material** directly inside terminal OCR, even when the higher-level semantic read is valuable. In those cases, do **not** export the latest frame just to prove the exact command set, and do **not** copy the raw values into the durable layer. Preserve only the durable operational fact — e.g. **`verified sealed/full/ask command set was prepared`**, **`device flashing/provisioning decisions were being made`**, or **`chat/app debugging included request-id-level investigation`** — and explicitly label the newest specifics as **OCR-level continuity with redaction** when the semantic lane matters but the plaintext is too sensitive to preserve.
- A newer concrete browser-admin variant now matters too: a same-burst tail can land on a real **provider/admin console** such as **Moonshot/Kimi API Platform** with a visible **API-key creation modal** or adjacent **Projects / Billing / Recharge** pages tied to a real project name (for example **`hivemind-core`**). In these cases, preserve only the harmless operational fact — e.g. **`provider/admin credential setup or project-console work occurred`** — and intentionally omit the visible key/token itself. Also be alert for **same-app page-state mismatch** inside the same console family: one checked frame may really show **API keys**, while another nearby frame with similar OCR resolves as **Projects** or another subsection instead. Store that as **same-burst provider/admin continuity under page-state mismatch caution**, not as a fake clean single-page chronology.
- If a frame’s OCR/window says a specific browser work tab (for example Claude / Docs) but the export visually shows another browser tab instead, record that as a **same-app browser-state mismatch** and keep the work-tab read at **OCR-level continuity** unless reinforced by another clean export.
- A newer concrete Gmail/browser variant matters too: within one same-hour browser burst, one checked frame can really be **Gmail** while neighboring frames with similarly Gmail-heavy OCR/window labels collapse into other browser states like **Bilibili** or **X**. When this happens, do **not** flatten the whole burst into either "all Gmail" or "all mismatch." Preserve the **visually confirmed Gmail frame(s)** as true email-review anchors, keep the rest of the email/newsletter/account lane at **OCR-level continuity**, and write the durable hour/entity/summary language at the broader **mixed browser cluster** level.
- A newer high-value maintenance case: a lane that was only **OCR-level correction** in one digest can become **visually confirmed semantic center** in a later same-hour pass. When that happens, do **not** leave the durable layer narrating it only as tentative OCR continuity just because the earlier pass was mismatch-prone.
- In practice, if a later direct export cleanly confirms the correction lane (for example a real terminal showing a trust-story walk-back, a hardcoded green row, or explicit fix options after an earlier OCR-only attestation read), you should:
  - promote that lane from **OCR-level continuity** to **visually confirmed foreground work**,
  - rewrite the top recency/hour/entity language so the newly confirmed lane can become the semantic center,
  - and update relevant `distillation/*.md` pages when the pass teaches a stronger reusable cognition rule (for example: the user does not stop at better wording, but pushes to remove misleading code/comment/UI residue too).
- Do not keep both states in conflict. Preserve the earlier OCR-only read as historical context if useful, but let the newer visually confirmed pass become canonical for recency and distillation.
- Another newer terminal-heavy evidence rule matters too: a late checked **terminal recap/status** can visibly claim strong facts like **CI green**, **deploy green**, **live compose verified back to source**, or **watch-history run succeeded after redeploy** while still being only a recap line inside the workbench rather than an independently checked GitHub/API/dashboard surface.
- In those cases, upgrade the lane beyond pure OCR-only continuity if the frame is genuinely a clean terminal foreground, but do **not** overstate it as externally re-verified proof unless you separately confirm it from the real outside surface (for example GitHub Actions, a health/API endpoint, or another independent runtime check).
- Good durable wording is: **visibly claimed validation-success / deploy-success checkpoint** — stronger than OCR-only continuity, weaker than independently cross-checked runtime proof.
## Auditability requirement

When the user asks for stronger auditability, meaningful passes should leave a compact but explicit audit trail in the durable layer.

At minimum, a meaningful digest should usually make it possible to recover:
- review window inspected
- newest evidence frontier reached
- frame IDs directly inspected
- number of images visually inspected
- important frames that were text-only because `image_key: null`
- claims that remained OCR-only
- mismatch/correction notes
- files created/updated

A practical pattern is to add a short `## Audit trail` section to `fisherman-digests/*.md` with bullets for:
- `Frames inspected:`
- `Images visually checked:`
- `OCR-only evidence retained:`
- `Mismatch/corrections:`
- `Files changed:`

This should stay compact, but it should make later review falsifiable. If a pass returns `[SILENT]`, the cron output should still be grounded in an actual frontier check rather than an uninspected assumption that nothing changed.

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
- If I updated an existing entity page, did I merge into the current sections cleanly instead of accidentally duplicating headings like `## Evidence from rolling memory` or `## Source files`?

When updating existing entity pages, prefer editing the current section contents rather than inserting a second copy of the same heading. In practice, repeated automated updates can easily create duplicate section headers that make the page harder to read and retrieve from.

## Tooling pitfall when updating mind files

When reading existing markdown files with Hermes `read_file`, remember that the returned `content` includes `LINE_NUM|` prefixes for every line.

Do **not** feed that string directly back into `write_file` / `execute_code` rewrites, or you will accidentally persist the line-number prefixes into the markdown files.

Safer patterns:
- use targeted `patch` edits when possible
- if using Python/shell for a full rewrite, read the file from disk directly instead of reusing `read_file` output
- if you must reuse `read_file` output programmatically, strip the leading `^\s*\d+\|` prefix from each line before writing
- if you use `execute_code` or another batch-writing path to update several mind files at once, remember that writes may already have hit disk **before** a later step fails. After any batch-write failure, immediately verify which files were partially updated and finish with targeted `patch`/`write_file` cleanup instead of assuming the whole transaction rolled back.

This matters especially for `rolling-summary.md`, `INDEX.md`, and `context-hours/*.md`, where accidental prefix persistence pollutes the user's durable memory layer and failed batch rewrites can leave the wiki in a partially updated state.

Additional practical pitfall: when programmatically rewriting `rolling-summary.md` recency blocks, a naive string replacement can leave behind **orphaned/truncated lines from the previous recency section** if the replacement boundaries are slightly wrong, accidentally duplicate the `## Recency notes` heading itself when prepending a new block, **or demote the previous top block without carrying forward its own correct timestamps/content** (for example leaving the old `18:38 UTC` heading in place but mistakenly keeping the `17:33` frame timestamp/details under it).

Another concrete batch-writing pitfall: when creating new mind markdown files via `execute_code`/Python, do **not** accidentally write patch/diff markers like leading `+` characters into the actual file body or frontmatter. This can happen if you compose file content from patch-style strings or copied diff hunks instead of clean markdown literals. After any scripted creation of a new digest/hour/entity file, immediately re-read the top of the file and verify there are no stray diff markers persisted as content.

Practical validation lesson: do **not** use a naive whole-file assertion like `assert '***' not in text` or `assert '@@' not in text` as your only diff-marker check. Real markdown can legitimately contain repeated asterisks or other punctuation, and that can create false alarms after the files have already been written. Safer validation patterns are:
- check for patch-marker lines specifically at line starts (for example `*** Begin Patch`, `*** Update File:`, `@@` hunk headers, leading `+`/`-` diff lines in places they should not appear),
- re-read the top/frontmatter and the touched section ranges directly,
- and if a batch script fails after writing some files, assume partial writes already landed and verify each touched file on disk before retrying.

A newer closely related failure mode matters for other top-level sections too, not just recency: broad insertion logic can accidentally duplicate an existing heading like **`## Active workstreams`** when prepending a new bullet block. After any automated insertion into `rolling-summary.md`, immediately re-read the touched area and verify there is still exactly **one** copy of the section heading you targeted, not a doubled heading followed by the new content.
- Another concrete `## Active workstreams` trap: when you prepend a new top bullet like **`Latest ... pass`**, do **not** leave the formerly top bullet still labeled **`Latest`**. This creates a contradictory top-of-page chronology even if the new insert itself succeeds.
- After any automated `## Active workstreams` prepend, immediately re-read the first several bullets and demote the formerly top item to **`Previous ...`** (or equivalent past-tense wording), updating any `current pass` / `now shows` language inside that demoted bullet so it still describes its own timestamp rather than the newer pass. A newer failure mode also matters: a regex-based rewrite can silently do **nothing at all** if the pattern does not span newlines the way you expected (for example forgetting DOTALL / multiline behavior when replacing everything from `## Recency notes` to the next heading). Another real failure mode: a broad rewrite can accidentally preserve or duplicate an entire later section block (for example a second `## Collaborators / social surfaces repeatedly present` or `## Repeated frictions / constraints` section) even when the recency block itself looks correct. Prefer explicit slicing between sentinel headings when possible, and always verify the replacement actually landed on disk. After any automated rewrite of the recency section, immediately re-read that section and verify:
- there is exactly **one** `## Recency notes` heading
- the newest block says `latest` only once
- the previous block is clearly demoted to `previous` / `earlier`
- the demoted previous block still has the **right timestamp, frame endpoint, and summary for that exact pass** rather than stale content copied from an older block
- the newly intended top block actually appears in the file (not the old block left untouched)
- there are no stray fragments like partial bullets or dangling text from an older pass
- later major headings like `## Collaborators / social surfaces repeatedly present` and `## Repeated frictions / constraints` still appear exactly once each after the rewrite
If you find residue, duplicated headings, a missing new block, or a mismatched demoted block, do a targeted cleanup patch right away instead of assuming the batch rewrite was clean.
- Another concrete `rolling-summary.md` trap: a scripted pass can successfully prepend a new recency block yet still leave the file-level timestamps stale if you do not explicitly update **both** the frontmatter field (for example `last_updated:`) and the visible body line (`Last updated:`). After any automated rolling-summary update, immediately re-read the top of the file and verify that:
  - the frontmatter timestamp matches the newest pass you intend to preserve,
  - the visible `Last updated:` line matches it too,
  - and those timestamps agree with the top `## Recency notes` block rather than leaving a split-brain state where the recency section advanced but the page header still claims an older update time.
- A newer concrete rewrite pitfall: if you use Python `re.sub` to rewrite markdown timestamps and your replacement string uses a backreference immediately followed by a digit-starting timestamp (for example `rf'\1{stamp}'` where `stamp` begins with `2026...`), Python can interpret it as a larger backreference/octal escape and silently corrupt the line (for example turning `last_updated:` into `P26-...`). Safer patterns:
  - use the explicit group form `rf'\g<1>{stamp}'`, or
  - avoid regex replacement for these header timestamps and use exact-string replacement / targeted `patch` edits instead.
- After any regex-based timestamp rewrite, immediately re-read the touched header lines and verify you still have literal keys like `last_updated:` and `Last updated:` rather than malformed prefixes.
- Another concrete `rolling-summary.md` failure mode: some real wiki states may **not contain any `## Recency notes` heading at all** even though the file is otherwise valid. In that case, do **not** assume a recency-rewrite script can slice from `## Recency notes` to the next heading. Instead, detect the absence explicitly and insert a fresh `## Recency notes` block at a stable boundary (for example immediately before `## Collaborators / social surfaces repeatedly present` or another known later major heading), then re-read the file and verify there is now exactly one recency heading in the intended location.
- A newer concrete variant: even when `## Recency notes` **does** exist, it may be the **last major section in the file** rather than a block followed by `## Collaborators ...` or another later heading. Do **not** hard-code a required end-anchor like `\n## Collaborators / social surfaces repeatedly present` when rewriting recency. Safer pattern: detect the start of `## Recency notes`, then either slice to the **next heading if one exists** or to **EOF** if recency is the terminal section. After rewriting, re-read the tail of the file and verify the new recency block actually landed and the file did not get truncated accidentally.
- Another concrete recency-update failure mode: a prepend/insert script can successfully add the **new** top recency block while still leaving the **old previous top block** in place as if it were still current, producing a duplicated or contradictory frontier (for example a new `20:28 UTC` block followed by an unchanged old `19:50 UTC is the latest review pass` block). After any automated recency update, explicitly scan the next few bullets and remove or demote any leftover former-top block that still claims to be `latest` or re-states the already-superseded frontier. Do not stop after checking headings alone; verify the **content-level recency claims** were normalized too.
- A newer concrete variant: a naive demotion step can also accidentally demote or overwrite the **wrong older block** when several same-day continuity passes are stacked close together. Practical failure shape: you prepend a new `18:54 UTC` block, then broadly replace the next matching `Previous ...` paragraph and silently turn the former **17:44 / 16:34 / 15:26** block into the apparent immediate predecessor while the real **18:02** block is skipped or left with stale content. After any recency prepend, explicitly verify the **immediate previous block is still the true previous pass by timestamp**, and that its body still describes **its own** evidence/run outputs rather than inherited details from an older pass.
- A closely related concrete variant: if you build the new recency section as **`new_top + old_recency`** and your `old_recency` slice still starts at the original `## Recency notes` heading, you can silently create **two consecutive `## Recency notes` headings** even though the rest of the content looks fine. After any prepend-style recency rewrite, explicitly check for duplicate `## Recency notes` headings with a search/readback pass and, if found, delete the second heading immediately rather than assuming the insertion boundaries were correct.
- Another concrete recency-edit trap: even when the heading structure looks correct, a broad string/regex replacement can leave behind a **stray continuation sentence or duplicated detail line from the formerly top recency block**. In practice this can produce a malformed previous-pass entry such as one bullet containing the old timestamp line **plus** an extra copied sentence about the raw frontier / Rokid state, or a duplicated `Best current read` line immediately under the demoted block. After any recency rewrite, do not only verify headings; also re-read the next 1-2 older recency entries and confirm each prior pass now has exactly the intended bullet set, with no leftover continuation text from the pre-rewrite version.
- a closely related batch-update trap: if your multi-file script writes the new digest/hour/entity files first and then crashes because `rolling-summary.md` lacked the expected `## Recency notes` anchor, you have already created a **partial-write wiki inconsistency**. Treat that as a real repair case: keep the new digest, then finish the summary/index cleanup with targeted `patch` edits rather than rerunning the same broad script blindly.
- another practical verification pitfall: `search_files` against a large markdown file can return **summarized/truncated grep results** that are good enough to confirm a heading exists but awkward for exact edit boundaries (for example weird path+line summary formatting around `## Recency notes` or other large-section anchors). Use it for coarse discovery, but before patching a critical section like recency or digest lists, prefer a direct `read_file` of the nearby line range to verify the exact on-disk text you will edit.
- Another concrete `context-hours/*.md` trap: when auto-inserting new `## Source digests` bullets, keep the wikilink/filename format consistent with the surrounding file (for example `[[fisherman-digests/2026-04-18_0309.md]]` rather than accidentally dropping the `.md` suffix if the rest of the file uses it). After any scripted source-digest insertion, immediately re-read that section and verify the new bullet matches the existing local linking convention instead of introducing a one-off malformed link.
- Another concrete batch-write pitfall: when composing markdown bodies in `execute_code`/Python, do not leave literal template placeholders like `{stamp}`, `{raw_frontier}`, `{rokid_frame_frontier}`, or similar markers inside the written files because an inner multiline string was not actually formatted. This can silently land in `context-hours/*.md`, entity pages, or digests even when the rest of the update succeeds. After any scripted multi-file write, search the touched files for leftover `{...}` placeholders and patch them immediately before finishing the run.
- A newer closely related `context-hours/*.md` consistency trap: some hour notes now carry a **frontmatter `source_digests:` list** in addition to the body-level `## Source digests` section. If you add a same-burst digest like `2026-04-21_0950.md` or `2026-04-21_0953.md`, do **not** update only the body section and forget the frontmatter list (or vice versa). After any same-hour catch-up patch, re-read the top of the file and the `## Source digests` section and verify they reference the **same digest set in the same newest-first order**.
- Another concrete `context-hours/*.md` insertion trap: when prepending a new same-hour evidence block programmatically, do **not** accidentally reinsert the target section heading itself (for example creating two consecutive `## Direct evidence` headings). Safer pattern: slice from the existing heading, insert only the new bullets/content under that heading, and then re-read the touched range. After any scripted same-hour update, explicitly verify there is exactly **one** copy of each major section heading you touched (`## Direct evidence`, `## Inferences`, `## Open questions / ambiguity`, `## Source digests`) rather than assuming the insertion boundaries were right.
- Another concrete `context-hours/*.md` maintenance pitfall: when a later same-burst pass extends an existing hour note, do **not** only bump `last_updated`, `source_digests`, and the top raw frontier line. Also re-read the rest of `## Windows covered`, `## Apps / surfaces`, and `## Direct evidence` to make sure they reflect the new pass shape. In practice it is easy to leave behind a stale review-span line like **`grounded from a review spanning 17:19-18:19 UTC`** after the note has already been extended to **18:26** or **18:31**, or to forget to add a newly confirmed surface like **Chrome / Claude** even though the later pass made that app semantically central. After every same-hour catch-up, verify that the hour note's review-span wording, latest frontier line, app list, and source-digest lists all describe the **same newest checkpoint**.

Another concrete batch-update lesson: if a multi-file script fails **after some writes have already succeeded**, do not assume all touched mind files stayed in sync just because the later step errored. In practice this can leave a new digest file on disk while `context-hours/*.md`, `INDEX.md`, entity pages, and `rolling-summary.md` still point at an older or even impossible set of digest IDs/timestamps.

Another practical schema-variance pitfall now matters for `context-hours/*.md`: do **not** assume every hour note has a `last_updated:` field in frontmatter. Some older hour notes only carry fields like `date:`, `hour:`, and `timezone:`. If a batch script tries to blindly replace `last_updated:` across all targeted hour files, it can fail mid-run **after** already writing a new digest or other files, leaving the wiki in a partial-update state. Safer pattern:
- inspect the actual frontmatter shape of each target hour note before scripted rewrites,
- treat `last_updated:` as optional rather than guaranteed,
- and if one target lacks the expected field, finish the run with narrower body-level patches (`## Direct evidence`, `## Inferences`, `## Source digests`) instead of rerunning the same broad script blindly.
- After this kind of failure, immediately verify whether the new digest file already landed on disk and then normalize all cross-file references by hand/patch so the durable layer is consistent again.

A related evidence-gathering pitfall now matters too: a frame whose OCR/show text looks like a clean **terminal commit/push** checkpoint can still export visually as a broader **Mission Control / desktop overview / mixed workbench** state. In those cases, preserve the operational claim only as **show/OCR-grounded mixed-workbench evidence**, not as a visually pure terminal foreground, and explicitly record the export mismatch in the audit trail rather than silently upgrading it to clean visual proof. After any partial-write failure, immediately re-read **every file you intended to touch** and normalize the cross-file links/state before finalizing. At minimum verify:
- newly written digest filenames are actually the ones referenced from the relevant `context-hours/*.md` files
- `rolling-summary.md` top recency block matches the newest digest you want to preserve
- `INDEX.md` lists the same newest digest set as the hour notes
- entity pages (for example `Rokid AI Assistant.md`) reflect the same latest runtime numbers/timestamps as the newest digest
- an earlier-hour note (for example `17.md`) does not accidentally absorb later-hour frames just because a failed script partially overwrote it before you corrected the newer `18.md`
If these cross-file references disagree, treat that as a **wiki-consistency bug** and repair it before ending the run.

Another newer operational lesson: the mind layer can also drift because of **concurrent same-burst updates**, not only because your own script failed. In practice, while you are gathering evidence and writing files, a later same-burst digest or another maintenance pass may already have advanced `rolling-summary.md`, `INDEX.md`, `context-hours/*.md`, or the newest `fisherman-digests/*.md` beyond the snapshot you first read. Before finalizing any rolling-summary maintenance run, do one last on-disk consistency check that explicitly asks:
- did a newer digest file appear while I was writing?
- did `rolling-summary.md`, `INDEX.md`, or the target `context-hours/*.md` file already advance to a later timestamp/review pass?
- if so, am I about to accidentally overwrite a newer same-burst state with an older one?
If a newer on-disk pass exists, do **not** blindly force your earlier snapshot back onto disk. Instead, merge forward: preserve the newer digest/recency block, update your hour/entity notes to the same newest endpoint, and then verify all cross-file references again. Treat this as a **concurrent-update consistency check**, especially during active live bursts where multiple micro-digests can land within minutes.

A concrete discovery-time lesson now matters too: do **not** rely only on a small/truncated directory listing when deciding what the current newest digest/hour already is. In practice, a limited `search_files` / file-list snapshot can hide a same-hour digest like `2026-04-19_1017.md` or an already-written `context-hours/.../10.md`, and then a later write incorrectly acts as if your new `10:21` pass is the first update for that hour. Before creating a new digest/hour file, cross-check the current frontier from at least one **index-style source** (`INDEX.md`, `rolling-summary.md`, or the relevant `context-hours/YYYY-MM-DD/` note) in addition to the raw directory listing. If those sources disagree, treat that as a **freshness-discovery bug** and rescan before writing.

A concrete late-burst variant now matters too: you may successfully write a new digest (for example `2026-04-18_0618.md`) and then discover that a newer same-burst digest (for example `2026-04-18_0621.md`) was already written by another pass before you finished updating `rolling-summary.md`, `INDEX.md`, or `context-hours/`. In that situation:
- do **not** keep narrating your own digest as the latest pass just because you wrote it yourself,
- immediately rescan the digest directory for the newest same-hour files,
- re-read `rolling-summary.md` recency blocks and `INDEX.md` digest lists before patching them again,
- and normalize the durable layer so it preserves **both** the richer intermediate checkpoint you wrote and the truly newest on-disk endpoint.
A common failure signature here is that your patch collides with a newer file state and leaves behind duplicated section labels or list items (for example two consecutive `Newest notable digests:` lines in `INDEX.md`, or duplicated source-digest bullets in a `context-hours/*.md` note). After any concurrent-write warning or failed replacement, immediately re-read the touched sections and clean up duplication before finalizing.

Practical repair lesson:
- during active same-burst maintenance, prefer **targeted `patch` edits** over one broad script rewrite whenever the files may be changing underneath you
- if a batch script partially succeeds (for example, writes a new digest file) and then fails on a later replacement because the target file has already changed, immediately re-read the touched files and finish with targeted `patch` cleanup
- do not assume a failed batch means nothing changed; you can easily end up with a real new digest on disk that is not yet referenced from `context-hours/`, `INDEX.md`, or the relevant entity page
- another concrete same-burst trap: even if **your own** digest write succeeds, a **newer micro-digest can appear minutes later while you are still editing** (for example you write `2026-04-15_1801.md`, but by final verification the durable layer has already advanced to `2026-04-15_1803.md` / `2026-04-15_1806.md`). In that case, do **not** keep treating your first successful digest as canonical just because you created it. Re-scan the digest directory, identify the newest on-disk same-burst digest, and normalize `rolling-summary.md`, `INDEX.md`, `context-hours/*.md`, and entity-page `## Source files` / `## Source digests` references so they point at the true newest surviving endpoint rather than leaving stale references to the older digest you happened to write first.
- a newer concrete variant: the competing newer same-burst digest may also contain a **different late-frame interpretation** rather than just a later timestamp. For example, one pass may summarize the tail as an **iter result / move to the next iteration**, while a concurrent later digest reads the same endpoint more concretely as a **specific code/tool change** (like widening `SCOPE_TOOLS` from `[verify_tool]` to `[verify_tool, simulate_tool]`). In that case, do **not** simply overwrite one with the other or keep only whichever digest you wrote yourself first. Preserve the newer digest as canonical for recency, but merge the two evidence families when they are compatible, then update `rolling-summary.md`, `INDEX.md`, `context-hours/*.md`, and entity pages so they reflect the **newest endpoint plus the richer combined interpretation**.
- Another newer concrete variant: you may write an intermediate digest like **`13:37`**, then discover that the durable layer already advanced again to **`13:39`** and **`13:41`** while you were still patching `INDEX.md` / entity blurbs / hour-note source-digest lists. In that case, keep the intermediate digest if it records real evidence, but do **not** leave the index/entity layer frozen on the older state just because your own write succeeded first. Re-read the newest on-disk digests, preserve the later recency frontier as canonical, and then make sure the **earlier intermediate digest is still referenced from the relevant `context-hours/*.md` source-digest block** so the evidence trail is complete rather than silently skipped.
- another concrete same-burst variant now matters for active mixed-workbench hours: the hour can first look like a **dual-terminal / dual-project cluster** and then, a few minutes later, cool into a cleaner **single-rail planning tail** (for example a mixed Feedling+Hivemind burst that later resolves into Hivemind-only next-run planning). In that case, do **not** overwrite the earlier mixed read as if it were wrong, and do **not** keep narrating the whole hour as if both rails stayed equally foregrounded to the end. Preserve the later micro-digest as canonical for recency, update the hour note so it explicitly says the **center of gravity shifted over time**, and rewrite the top recency/entity blurbs to describe the hour as a **same-burst workbench whose semantic center changed**, not as a fake clean app chronology.
- another newer same-hour pattern now matters for product/distillation maintenance too: a cluster can sharpen into a **recipient-legibility workbench** where three seriousness rails stay live at once — **(a) public shell / website formalization**, **(b) a nearby mechanism or economics board**, and **(c) a parallel implementation-proof rail** (for example attestation/demo/protocol work). In those cases, do **not** collapse the hour into only landing-page polish or only implementation. Preserve the durable unit as **shell + mechanism + proof in one workbench**, especially when export/app labels invert across browser, terminal, and editor surfaces.
- a newer same-burst tail pattern matters too: the newest minute can first appear to **degrade into a low-weight browser/search or mislabeled-chat tail** (for example a nominal **WeChat** frame that actually exports as ordinary **Chrome / Google image-search** continuity), and then a 2-5 minute-later refresh can return to a cleaner real **technical endpoint** in the same hour (for example a real **Terminal / `hermes-toy-esp`** foreground). Do **not** freeze the hour on the first low-weight tail just because it was the newest checked frame at that moment. If fresh frames are still landing, run one more refresh before finalizing; if a later clean technical frame appears, keep the broader semantic center on the main workbench and store the browser/search moment as **same-burst low-weight continuity / mismatch correction**, not as the canonical endpoint.
- a newer concrete same-burst clarification pattern now matters for mixed **verification + recovery** workbenches: one pass can already establish a strong shipped/verified checkpoint (for example a real Hivemind Phase-5.1 verification recap), then a 3-6 minute-later refresh can materially sharpen the same burst in **two different directions at once**:
  1. a short chat/Telegram foreground can add a concrete operational update like **`fetch history works now though`**, upgrading a lane from pure auth/queue confusion into **partial recovery / partial success**, and
  2. a later terminal tail can shift the same project from **verified current state** into a **next-phase design sketch** (for example public/private inspection mode, sealed private blobs, or another not-yet-shipped privacy/control-plane design).
- In those cases, do **not** flatten the later catch-up into either "nothing new" or "same as before." Write the micro-digest as a real semantic advance: preserve the earlier shipped/verified checkpoint as still true, then record the later same-burst additions separately as **(a) newer partial operational success** and **(b) newer next-phase design direction**.
- Practical wording pattern:
  - earlier checkpoint: **verified / shipped / production ask succeeded**
  - later catch-up: **same-burst partial recovery now visible**
  - later terminal/design tail: **same-burst next-phase design sketch, not shipped state**
- This prevents a common retrieval failure where the durable layer either freezes at the earlier verified state and misses the newer operational improvement, or wrongly upgrades the later design prose into completed implementation.
- a closely related practical failure mode: exact-string `patch` replacements against `INDEX.md` or entity blurbs can fail silently in spirit when a concurrent same-burst write has already changed the surrounding text. If your replacement lands nowhere useful, or you verify and still see old timestamps/blurbs, immediately re-read the current file and retry with a **smaller, fresher target span** instead of assuming the first patch covered it.

- another practical same-run consistency trap: when you update an **earlier hour note** to mention a **later same-day pass** (for example, adding a note that direct Rokid artifacts later advanced beyond the hour's original runtime cutoff), do **not** stop after changing the top `## Windows covered` line. Immediately sweep the whole hour note — especially `## Direct evidence`, `## Inferences`, and `## Open questions / ambiguity` — for stale phrases like **`no newer saved artifacts`** or other wording that now contradicts the newer same-day state.
- in those cases, preserve the original hour-local truth if it still matters (e.g. `within this hour, Rokid only served as runtime continuity`) but rewrite it so it explicitly coexists with the later same-day update (`later passes advanced raw timestamps, but processed/transcript lag remained`). This prevents the wiki from simultaneously claiming both **newer artifacts exist** and **no newer artifacts exist** in the same note.
- Another concrete `INDEX.md` pitfall: broad anchor-based insertion can land a new block under the **wrong section** if the matched text is not unique enough. In practice, a `Recent context hours` block intended for `## Rolling observational memory` can accidentally be inserted under `## Entity pages` or another earlier heading if you replace against a reused digest line or a shallow anchor.
- a related `INDEX.md` failure mode: a replace/edit targeting the digest block can accidentally duplicate the nearby section label itself (for example leaving two consecutive `Newest notable digests:` lines) when the old-string boundary does not include exactly one heading copy or the file changed underneath you. After any digest-block edit, immediately re-read the surrounding lines and verify there is exactly one `Newest notable digests:` line and that the list starts directly beneath it.
- another concrete `INDEX.md` list-edit failure mode: regex/string replacement of existing entity-page blurbs can accidentally leave **doubled markdown list/backtick prefixes** (for example lines starting with ``- `- `context-entities/...``) when the replacement text itself includes bullet/backtick markers but the matched span already preserved part of the old marker. After any scripted rewrite of entity blurbs under `## Entity pages`, immediately re-read those lines and verify each entry begins with exactly one `- ` bullet and one balanced code span around the path.
- another concrete `INDEX.md` entity-blurb failure mode: a broad scripted rewrite can update some entity summaries while silently **missing another nearby blurb whose text no longer matches the expected old string**, leaving one entity timestamp/summary stale even though the rest of the index advanced. A related micro-failure: partial-line replacement can leave a tiny stray suffix or merged word at the end of a blurb (for example a leftover `...cautionon`). After any batch update touching `## Entity pages`, immediately re-read the whole entity-blurb block and verify **every** intended entity line advanced to the new timestamp/summary and contains no trailing residue.
- another concrete Python rewrite pitfall now matters for `INDEX.md` and other markdown files: do **not** use `re.escape(...)` on the **replacement text** you want to write back to the file. In practice this can persist literal backslashes into markdown lines (for example turning a normal entity blurb into `\-\ ...`) or otherwise produce escaped-on-disk garbage while still looking superficially like a successful replacement. If you need regex matching with dynamic replacement text, prefer a `lambda` replacement or a plain exact-string `patch` edit, then immediately re-read the touched lines and verify no literal backslashes/escaped markdown markers were written.

- another concrete `INDEX.md` consistency trap: do not only bump timestamps in the short entity-page blurbs under `## Entity pages`. If `Fisherman.md`, `Rokid AI Assistant.md`, or another entity page is materially updated, rewrite the one-line `INDEX.md` description so it matches the **new actual top section** of the entity page. Otherwise the index can claim a newer timestamp while still describing the older state (for example a stale `README/docs push` summary after the entity page has already moved on to a `Seven notch-status` debugging burst).
- after any `INDEX.md` update that touches entity blurbs, re-read both the index lines and the top of the corresponding entity pages and verify that the **timestamp and summary text agree**, not just the date.
- Another concrete `INDEX.md` recency trap: a maintenance pass can successfully insert the new digest under `### Fisherman digests` while silently leaving the separate **`Newest notable digests:`** block stale. After any same-burst digest write, explicitly re-read **both** digest lists in `INDEX.md` and verify the newest digest appears at the top of both sections, not just the first one.
- A closely related implementation pitfall: when adding the same new digest line to both digest lists programmatically, do **not** guard the second insertion with a naive whole-file presence check like `if line not in file_text`, because the line will already exist after the first insertion and the second block will be skipped. Check or patch each section **independently**, then re-read `INDEX.md` and confirm the new digest appears once under `### Fisherman digests` and once under **`Newest notable digests:`**.
- Another closely related `INDEX.md` trap: if your exact-string replacement target for inserting a digest is **not unique** because the same digest line or surrounding block appears in multiple places (for example once under `### Fisherman digests` and again under **`Newest notable digests:`**), a broad replacement can hit the **wrong section first** and duplicate the new digest in one block while leaving the other block stale. Safer pattern: anchor each edit with the nearest unique section heading (`### Fisherman digests` vs `Newest notable digests:`), patch those sections separately, and immediately re-read both blocks to verify **no duplicate top digest line** was created in the first block while the second block stayed unchanged.
- another concrete `INDEX.md` context-hours pitfall: if you insert a new hour-note line near `### Context hours` and then also do a second broader replacement that re-includes the same new line (for example replacing the old top `20.md` block with `new_hour_line + old_20md_line`), you can silently create **duplicate `21.md` bullets** even though both edits individually look reasonable. After any scripted `INDEX.md` update that adds a new context-hour entry, explicitly re-read the first few bullets under `### Context hours` and verify the new hour appears **exactly once**, in the intended position, before finalizing.
- another concrete cross-file drift pattern now matters too: `rolling-summary.md` and the newest `fisherman-digests/*.md` can already be current while one or more **entity pages** (especially `Fisherman.md` and `Rokid AI Assistant.md`) are still stale from an earlier pass. This leaves the wiki in a split-brain state where recency blocks and hour notes know about the latest frontier but the entity layer still describes an older one.
- before finalizing any rolling-summary maintenance run, explicitly compare the newest digest / rolling-summary recency block against the tops of the most relevant entity pages. If the recency layer has advanced but the entity page top sections have not, patch the entity pages immediately and then update the matching `INDEX.md` blurbs so all three layers agree on the same latest frontier.
- another concrete entity-page pitfall: if you prepend a new `## Newest ...` block by replacing only the later `## Current durable state` anchor, you can accidentally leave an **older `## Newest ...` block above the truly newest one**. This makes the page read in the wrong order even though both sections are present.
- when updating rolling entity pages like `Fisherman.md` or `Rokid AI Assistant.md`, re-read the top of the file immediately after the edit and verify that the **newest timestamped `## Newest ...` section appears first**, older `## Newest ...` sections are demoted below it in descending time order, and you did not create a misleading top-of-page chronology.
- another concrete entity-page pitfall: if you prepend a new `## Current best read` / `### Newest ...` block with a scripted replace, you can accidentally leave the prior newest section **headingless** or duplicate a container heading like **`## Earlier supporting reads`**. In practice this yields one orphaned timestamp block floating under the wrong heading and then a second repeated `## Earlier supporting reads` later in the file.
- a closely related concrete variant: a prepend can also leave **two consecutive `## Current best read` headings** if the older top block is not explicitly demoted when the new one is inserted. After any prepend on `Hivemind.md`-style entity pages, verify there is exactly **one** `## Current best read` heading at the top, and that the previous top block has been moved under **`## Earlier supporting reads`** rather than left as a second current section.
- another concrete prepend bug now matters for simple entity pages too: if you replace a short title anchor like `# Fisherman\n\n` or `# Rokid AI Assistant\n\n` with a new block that already includes both the title **and** the next existing section heading, you can silently duplicate the first carried-forward `## Newest ...` heading. In practice this creates two identical consecutive section headings near the top even when the rest of the inserted text looks correct.
- a newer closely related pitfall: even when the heading structure stays valid, prepending a fresh `## Newest ...` block on simple entity pages like `Fisherman.md` or `Rokid AI Assistant.md` can leave the immediately previous top block still labeled **`## Newest ...`**, producing a misleading chronology with two successive sections both claiming to be newest. After any prepend on these pages, explicitly demote the formerly top section to **`## Previous ...`** (or another non-current label) and then re-read the top of the file to confirm only the real current block still claims `Newest`.

- another closely related real failure mode: do **not** assume the file literally **starts** with the title anchor. Many `context-entities/*.md` pages begin with YAML frontmatter, so a scripted guard like `text.startswith('# Fisherman\n')` or a prepend that only works at byte 0 can fail **after** other files were already written. Safer pattern: search for the title anchor **inside the body after frontmatter**, or prefer a stable section anchor like `## Current best read`. If that guard fails mid-batch, immediately treat the run as a **partial-write repair case** and reconcile the already-written digest/hour/summary/index files before doing anything else.
- safest pattern for title-anchor prepends: replace only the title-and-blank-lines anchor with `title + new section`, and let the older next section remain untouched exactly once below it. After any such prepend, immediately re-read the top ~30 lines and confirm you do **not** have two consecutive identical `## Newest ...` headings.
- another concrete verification lesson: do **not** only re-read the first ~60 lines after an entity-page prepend. A duplicated container heading like **`## Earlier supporting reads`** can survive much farther down the page, leaving the top of the file looking clean while the mid-page chronology is still split into two separate supporting-reads sections. After any scripted prepend on an entity page, also run a heading search for repeated container headings and normalize them immediately if more than one copy remains.
- after any scripted prepend on an entity page, immediately re-read the first ~60 lines and verify all three things: **(1)** there is exactly one container heading such as `## Current best read` / `## Earlier supporting reads` near the top, **(2)** the formerly-top section still has its own `### ...` heading rather than becoming bare bullets, and **(3)** the new top block appears above the older block without leaving duplicated section labels. Then also search the whole file for repeated container headings so you do not miss a second stale `## Earlier supporting reads` block farther down.
- another concrete entity-page maintenance pitfall: when you prepend or refresh a `context-entities/*.md` page with a new top block, do **not** stop after updating the visible narrative section. Also update the page's **`## Source files` / `## Source digests`** section so it includes the new digest and any newly created `context-hours/*.md` notes that now ground the latest read. Otherwise the entity page can look current at the top while its retrieval/audit trail still points only to older passes.
- after any entity-page update, explicitly re-read both the **top section** and the **source-files section** and verify they refer to the same newest pass rather than leaving a split state where the page body is newer than its cited evidence trail.
- another concrete entity-page maintenance pitfall: when you prepend a new top block on `context-entities/*.md`, do **not** forget to update the page-level frontmatter timestamp (for example `last_updated:`) if the page uses one. In practice it is easy to add a fresh **2026-04-22 10:50 UTC** top section while the entity frontmatter still claims **09:38 UTC**, leaving the wiki in a subtle split-brain state where the page body is newer than the page metadata.
- after any entity-page update, explicitly re-read the top frontmatter and verify the page-level timestamp matches the newest top section you just wrote. This matters for pages like `Fisherman.md`, `Hivemind.md`, and `Teleport.md`, because later retrieval/index maintenance may use the frontmatter timestamp as a quick freshness cue.
- another concrete schema-variance pitfall: **not all entity pages share the same body structure**. Some pages use `## Current best read`, but others (especially simpler/system-style pages like `context-entities/Fisherman.md` or `context-entities/Rokid AI Assistant.md`) begin directly with repeated timestamped `## Newest ...` sections under the title.
- do **not** assume a universal insertion anchor like `## Current best read` for batch updates across entity pages. Inspect the actual page shape first and choose the right insertion strategy per file:
  - if `## Current best read` exists, prepend beneath it;
  - otherwise prepend directly after the page title/frontmatter and preserve the existing descending chronology.
- if a batch script fails because one entity page lacks the assumed heading, treat that immediately as a **partial-write consistency risk**: verify whether the new digest/hour/summary/index files already landed, then finish the lagging entity-page repair with targeted patches instead of rerunning the same broad script blindly.


Use this when you are turning older Fisherman digests into the new layered memory system, when confidence in an old read is low, or when the user asks a retrospective question like "what did I do in the last 2 days?" and then wants that answer folded into the durable mind layer before another synthesis step like GNOMY.

### Retrospective multi-day catch-up pattern
When the user asks for a multi-day retrospective and the current mind layer is missing those hours, do not only write one high-level chat answer and do not only inspect the newest Fisherman burst.

Use this pattern:
1. First determine the exact retrospective window and inspect it with a direct DB-style pass, not only `summary --since ...`.
   - For 1-3 day windows, the CLI summary can be too recency-capped or too coarse.
   - Prefer a `uv run python` script from `/home/ubuntu/fisherman/server` that counts frames by app and by hour from the `frames` table, then decrypts representative `window` / `ocr_text` fields for the main clusters.
2. Identify the highest-signal missing hours inside that window.
   - Typical signs are repeated Teleport/Docs windows, real Terminal project windows, repeated Telegram clusters, or a dense mixed hour with multiple named projects.
3. Write **one retrospective digest** that summarizes the whole window at cluster level.
4. Also create the missing `context-hours/YYYY-MM-DD/HH.md` files for the important uncovered hours, not just the latest hour.
   - In practice, a good retrospective pass may need to add earlier missing hours like `11.md`, `12.md`, `13.md` for one day and `05.md`, `06.md` for the current day if those are the real uncovered anchors.
5. Then update:
   - `rolling-summary.md`
   - `INDEX.md`
6. If the user wants GNOMY or another higher-order synthesis next, only run that after the mind-layer catch-up files exist on disk.

### Historical distillation / intern-onboarding pass
When the user asks for a serious historical synthesis, cognition distillation, or something an intern could study to understand how the user thinks, do not stop at digests + rolling-summary only.

A useful archive-deepening selector from experience: do **not** only pick the chronologically oldest windows. First inspect the existing `distillation/` pages and find where a pattern is already named but still under-grounded by older frame-backed examples. Then use a direct DB/CLI scan of older days/hours to find dense candidate windows on dates that are underrepresented in the current distillation layer, export representative frames from those windows, and only write durable updates if the visual pass materially sharpens or corrects the pattern.

A newer practical selector helps for this:
- compare **distillation mention density** (which dates/hours are already repeatedly cited in `distillation/*.md`) against **raw Fisherman density** (days/hours with many frames or existing context-hours but little distillation coverage)
- good candidates are often hours that already have a decent `context-hours/YYYY-MM-DD/HH.md` note but are still weakly represented in the distillation layer
- three especially high-value historical window types now have stronger priority:
  1. **docs-heavy branding / Google Docs windows** that may actually be **founder self-model extraction** rather than mere copy-polish
  2. **chat-heavy steering windows** where a broad worldview gets forced into an explicit **proof-ranking / core-proof-vs-distraction** rubric
  3. **hardware/sourcing browser windows** that may look like ordinary marketplace browsing but are actually **supplier due diligence** or later **thesis-reframing fuel**
- another high-value historical type should also be prioritized when under-grounded in distillation:
  4. **collaborator-role differentiation windows**, especially dense Telegram/Signal/Lark bursts where different people in adjacent frames are each imposing a different proof obligation on the same thesis (for example venture-boundary pressure, seriousness/taste calibration, or physician/risk discipline)
- two newer high-value archive-deepening candidates should also be prioritized when under-grounded in distillation:
  4. **benchmark-object -> sourceability** windows, where a concrete reference object (for example a wearable/memory device benchmark page) is immediately pressure-tested against real supplier/storefront reality while the broader thesis tabs remain resident in the same browser workbench
  5. **operator-cockpit** windows, where provider auth/admin, device install-debug, eval/report review, and later summary/journal recompression all coexist in one mismatch-prone hour rather than surviving as a clean app chronology
  6. **archive-intake / source-family-normalization** windows, where imported archives are not yet ready for synthesis and the real work is figuring out provenance, overlap, and bundle boundaries before GNOMY/distillation. A concrete high-value shape is: founder self-account or sourcing/risk outreach still live in one surface, while a nearby Telegram/bot/operator lane explicitly discovers that two exports belong to the same evidence family, writes README/high-signal/source-reading/delta-note prep files, and warns that overlapping AI-chat bundles should be treated as one source family with sub-bundles rather than independent evidence.
  7. **public-language -> GTM proof-chain** windows, where a seemingly ordinary Teleport/Docs/copy hour is only the front door of a larger commercialization chain: the same burst later forces the shell through a real collaborator thread, a concrete budget tolerance, a real connect/auth QA surface, an ad-console setup state, and sometimes a local operator/tooling correction underneath the campaign surface. These are especially valuable because they teach that `better messaging` is not the proof object by itself; the real durable unit is the whole chain from shell -> budget -> flow QA -> publish/admin state -> live operator targeting.
- in docs-heavy windows, inspect multiple adjacent frames because the strongest durable gain may be a clearer read of the visible first-person answers (mission, target user, proof condition, wrapper discomfort), not just a nicer summary of the product copy
- a concrete archive-deepening lesson: a `Branding Website` Google Doc can visibly turn into a **founder-interview worksheet** (`What do you do...`, `How would you define the expedition...`, `How would you describe your current brand...`, `If you could hand off your entire operation to a clone...`) while a nearby frame in the same burst is actually a **memory-stack / rolling-summary maintenance** foreground rather than more branding work. Preserve that as **self-model extraction + self-model infrastructure**, not just copywriting.
- in hardware/sourcing windows, inspect whether the visible page is a real **company/factory search** with trust grades, response rates, years on platform, export/invoice capability, and manufacturer/distributor cues. That often means the lane is **operator verification / sourcing diligence**, not generic product shopping.
- if a later nearby frame from the same historical cluster then shows a real Docs/Claude rewrite that broadens the story beyond the sourced object, preserve the sequence as **sourcing diligence -> thesis reframing** rather than two unrelated browser moments.
- in chat-heavy steering windows, be careful: the semantic content may survive in repeated OCR and in an exported image, while the app attribution itself still collapses or mismatches. Preserve those as **content-confirmed under app-mismatch caution** rather than claiming clean app-specific foreground proof.
- another newer historical lesson: old-frame app labels can invert across collaboration tools too. A frame labeled **Signal** can export as a real **Lark** foreground, and a Telegram-labeled frame can export as a branding doc or memory-maintenance surface. In these cases, store the durable unit at the **cluster/workbench** level rather than forcing a fake clean app chronology.
- a newer collaborator-heavy warning matters too: in dense Telegram/Signal/Lark bursts, the tempting line in the sidebar preview can belong to a **different person/thread than the actual open main pane**, and adjacent frames only seconds apart can genuinely switch between different real collaborators. Do not flatten a minute into one person just because one sidebar preview is memorable.
- good historical pattern in those windows:
  1. inspect several adjacent frames, not one,
  2. determine which collaborator is actually in the **main pane** for each checked frame,
  3. distinguish **sidebar continuity** from **foreground collaborator pressure**,
  4. and preserve the durable lesson as **role differentiation** when different collaborators are imposing different kinds of pressure on the same thesis.
Add or update a dedicated `distillation/` layer, typically with pages like:
- `distillation/core-theses.md`
- `distillation/cognitive-patterns.md`
- `distillation/work-behavior-patterns.md`
- `distillation/project-evolution-map.md`
- `distillation/recurring-questions.md`
- `distillation/decision-style.md`
- `distillation/collaborator-usage-patterns.md`
- `distillation/intern-onboarding-guide.md`
- `distillation/how-sxysun-generates-knowledge.md`
- `distillation/methodology-and-confidence.md`

Guidelines for that pass:
1. Use representative screenshot export + visual inspection, not OCR alone.
2. Prefer pattern-level synthesis over exhaustive chronology, but still write at least one digest capturing the distillation pass itself.
3. Backfill at least the earliest or most origin-revealing missing `context-hours/` note if the pass uncovers a historically important hour not yet in the wiki.
4. Update `rolling-summary.md` and `INDEX.md` so future retrieval knows the distillation layer exists and how to use it.
5. Treat `distillation/` as a compiled layer above `context-hours/`, `context-entities/`, and `fisherman-digests/`, not as a replacement for them.

Important discipline:
- For these retrospective passes, reason at the **cluster/workbench level** rather than pretending every Chrome or Telegram frame is a clean foreground switch.
- Strongest action claims should still come from repeated **Terminal**, **Google Docs / Claude**, and named project windows.
- Preserve the distinction between:
  - a new latest fresh active window
  - a later retrospective review pass that merely compiled older missing hours into durable memory.

1. Prioritize a few high-signal windows first rather than trying to backfill everything blindly.
   Good candidates are:
   - first clearly active window after a quiet period
   - hours where a thesis visibly sharpened
   - hours with named collaborators / products / markets
   - hours already known to contain metadata/image mismatch risk
2. Re-query the original time range with `query -j --since ... --until ...` instead of trusting the old digest alone.
   - Practical host-specific CLI lesson: on this machine, the lighter `/home/ubuntu/fisherman/server` env may **not** expose a `fisherman` console script at all, so `uv run fisherman query ...` can fail with `Failed to spawn: fisherman` even though the server env itself is usable.
   - Reliable fallback for historical local inspection here is to run the server CLI file directly, e.g. `uv run python cli.py query -j --since '2026-04-15T16:00:00+00:00' --until '2026-04-15T16:20:00+00:00' --limit 30` from `/home/ubuntu/fisherman/server`.
   - Important parser lesson: this local `cli.py` path currently expects **ISO-8601** timestamps for `--since/--until` (for example `2026-04-15T16:00:00+00:00`), not looser strings like `2026-04-15 16:00 UTC`, which fail `datetime.fromisoformat(...)`.
3. Pick representative frame IDs from each hour and export screenshots with `show <id> -o ...`.
   - Same practical fallback: if the `fisherman` entrypoint is missing in `/home/ubuntu/fisherman/server`, use `uv run python cli.py show <id> -o /tmp/frame.jpg` instead. This worked for direct historical reinspection on this host.
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
- `templates/area-template.md`
- `references/file-layout.md`
- `references/obsidian-native-llm-wiki.md`
