---
name: fisherman-deputy-agent
description: Use this on an agent host after the user grants Fisherman Agent Access with a fishdep setup token. Registers the scoped deputy token, queries user context through Cloud, Self-hosted, or laptop relay, respects granted scopes, and can maintain an optional durable mind/rolling-summary note layer.
version: 2.0.0
license: MIT
---

# Fisherman Deputy Agent

Use this skill when a user gives you a `fishdep:` setup token from Fisherman
Settings -> Agent Access or from `fisherman deputy new`.

This is the scoped remote-agent path. It is not the same as the root
`/SKILL.md`, which is for trusted owner/operator work such as backend setup,
self-hosted server maintenance, and backend administration.

## Security Rules

- Treat the `fishdep:` token as a secret. Do not commit it, quote it in public
  logs, or send it to any service other than the local Fisherman CLI.
- Stay inside the granted scopes. If a command is denied, ask the user for a new
  Agent Access token with the right scope.
- Prefer concise summaries over dumping raw OCR or transcripts.
- Raw screenshots are allowed only when the token grants `read:screenshots`.
- Durable notes written by this agent must stay on the agent host or another
  user-approved destination. Do not upload them to third-party services unless
  the user explicitly asks.

## Register

The user should paste a setup block that includes a command like this:

```bash
fisherman deputy register 'fishdep:...'
```

Run it once on the agent host. Fisherman saves the deputy config under
`~/.fisherman-deputy/`.

To confirm registration:

```bash
ls ~/.fisherman-deputy
fisherman status --text
```

If the agent host has multiple deputy configs, select one explicitly:

```bash
FISHERMAN_DEPUTY_NAME=hermes fisherman status --text
```

or:

```bash
FISHERMAN_DEPUTY_CONFIG="$HOME/.fisherman-deputy/hermes.json" fisherman status --text
```

## Query Context

Use the normal Fisherman CLI. In deputy mode, these commands route through the
registered deputy config automatically.

Recent status:

```bash
fisherman status --text
```

Recent captured context:

```bash
fisherman query --since 30m --limit 20 --text
```

Search:

```bash
fisherman query --since 4h --search "keyword" --limit 20 --text
```

Transcripts:

```bash
fisherman transcripts --since 2h --limit 20 --text
```

Latest raw screenshot, when the token grants `read:screenshots`:

```bash
fisherman screenshot --output /tmp/fisherman-latest.jpg
```

Screenshot for a specific Cloud/Self-hosted frame id:

```bash
fisherman screenshot --frame-id 123 --output /tmp/fisherman-frame-123.jpg --source auto
```

Friends, when the token grants `read:friends`:

```bash
fisherman friend list --text
fisherman friend status --text
fisherman friend status alice --text
```

Publish a status, when the token grants `publish:status`:

```bash
echo '{"emoji":"💻","category":"coding","status":"reviewing deploy"}' \
  | fisherman publish-status --from-stdin
```

Pause/resume capture, when the token grants `control:pause`:

```bash
fisherman pause
fisherman resume
```

## Routing

Default to `--source auto`. It uses Cloud or Self-hosted backend when the token
contains a backend URL and falls back to laptop relay when needed.

```bash
fisherman query --source auto --since 30m --limit 20 --text
```

Use Cloud/Self-hosted directly only when you are diagnosing backend reads and
the registered deputy config includes a backend URL:

```bash
fisherman query --source secondary --since 30m --limit 20 --text
```

Use laptop relay when the user specifically wants laptop-local context:

```bash
fisherman query --source primary --since 30m --limit 20 --text
```

Cloud/Self-hosted direct routing currently supports `status`, `query`,
`screenshot`, and `transcripts`. Commands that need laptop-local state, such as
`friend status`, `publish-status`, `pause`, and `resume`, use the laptop relay
path. If `primary` fails, the user's laptop daemon is probably offline or not
connected to the relay. If `secondary` says no backend URL is configured, that
is not a token failure; use `auto` or ask the user to mint a new token after
selecting Fisherman Cloud or Self-hosted.

## Scope Map

- `read:status` allows `fisherman status`.
- `read:captures` allows `fisherman query`.
- `read:screenshots` allows `fisherman screenshot`.
- `read:transcripts` allows `fisherman transcripts`.
- `read:friends` allows `fisherman friend list` and `fisherman friend status`.
- `publish:status` allows `fisherman publish-status`.
- `control:pause` allows `fisherman pause` and `fisherman resume`.

Ask the user to revoke the token when the job is complete:

```bash
fisherman deputy revoke <name-or-pubkey>
```

## Durable Mind / Rolling Summary

Use this optional mode when the user wants a scoped agent to maintain durable,
searchable notes from Fisherman context instead of returning a one-off answer.
This is now part of the deputy skill because the common deployment is a remote
agent with a `fishdep:` token and a local notes directory.

Do not assume a hard-coded path. Pick the mind root in this order:

1. A path the user explicitly names.
2. `$MIND_DIR` if set.
3. `~/mind` on the agent host.

Recommended layout:

```text
<mind-root>/
  rolling-summary.md
  INDEX.md
  fisherman-digests/YYYY-MM-DD_HHMM.md
  context-hours/YYYY-MM-DD/HH.md
  context-entities/*.md
  mocs/*.md
  areas/*.md
```

The mind directory is a compiled layer between raw Fisherman frames and future
reasoning. Search and update it before going back to raw context unless it is
stale or insufficient.

### When To Use

Use this for daily/hourly digests, long-running self-context maintenance,
entity or workstream memory, and correction passes after better evidence
appears. If the user only wants a quick answer, query Fisherman directly and
keep the answer ephemeral.

### Evidence Gathering

Start with scoped CLI reads:

```bash
fisherman status --text
fisherman query --source auto --since 2h --limit 50
fisherman transcripts --source auto --since 2h --limit 50
```

Use targeted pulls when broad context is noisy:

```bash
fisherman query --source auto --since 4h --app "Chrome" --limit 50
fisherman query --source auto --since 4h --search "keyword" --limit 50
```

If the newest Fisherman timestamp is materially old, say so before calling a
pass fresh.

### Pass Types

Label every meaningful review pass:

- `fresh active window`: new context close to the current clock.
- `continuity pass`: recovering known or slightly older context.
- `correction pass`: updating an earlier interpretation because evidence
  changed.

### Operating Procedure

1. Select `<mind-root>` and create missing directories.
2. Inspect existing `rolling-summary.md`, `INDEX.md`, recent digests, and
   relevant entity/workstream pages.
3. Gather Fisherman evidence through the registered deputy config.
4. Separate direct evidence, inference, and uncertainty.
5. Write a new digest in `fisherman-digests/`.
6. Merge searchable details into relevant `context-hours/YYYY-MM-DD/HH.md`
   files.
7. Create or update entity/workstream pages when something recurs.
8. Update `rolling-summary.md` only when the high-level picture changes or
   sharpens.
9. Update `INDEX.md` when new durable pages are created.

### Writing Rules

For `rolling-summary.md`:

- Keep it compact enough to reread quickly.
- Prefer stable themes, active workstreams, recurring people, repeated
  frictions, and where-to-look-next.
- Use wikilinks for recurring concepts when helpful.
- Do not dump raw OCR.

For `fisherman-digests/*.md`, include:

- digest timestamp
- reviewed window
- current clock
- pass type
- direct evidence
- inferred themes
- uncertainty/corrections
- best current read

For `context-hours/YYYY-MM-DD/HH.md`:

- Preserve searchable names, app titles, project names, chat names, domains,
  and URLs when useful.
- Keep direct evidence separate from inference.
- Link source digests and related entity/workstream pages.

For `context-entities/*.md`:

- Capture why the entity matters, current best read, open questions, and links
  to supporting digests/hour notes.

### Stale-Job Verification

If this deputy runs on a schedule, verify actual files, not just scheduler
metadata:

```bash
fisherman query --source auto --limit 5
```

Compare:

- newest Fisherman frame timestamp
- newest file under `<mind-root>/fisherman-digests/`
- `Last updated:` in `<mind-root>/rolling-summary.md`
- any scheduler or cron last-run timestamp

If Fisherman has newer context but the mind files lag, do a manual catch-up
pass and tighten the recurring prompt/job.

### Backfill Procedure

Use this when turning older Fisherman exports or old summaries into the layered
mind directory:

1. Prioritize high-signal windows first.
2. Re-query the original time range instead of trusting old summaries when the
   token still grants access.
3. Create one solid hour note with caveats rather than many brittle notes.
4. Create or update entity pages only when a person/project/motif clearly
   recurs.
5. Record what was confirmed and what remained OCR-only or mismatch-prone.
