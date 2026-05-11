---
name: fisherman-deputy-agent
description: Use this on an agent host after the user grants Fisherman Agent Access with a fishdep setup token. Registers the scoped deputy token, queries user context through Cloud, Self-hosted, or laptop relay, and respects granted scopes.
version: 1.0.0
license: MIT
---

# Fisherman Deputy Agent

Use this skill when a user gives you a `fishdep:` setup token from Fisherman
Settings -> Agent Access or from `fisherman deputy new`.

This is the scoped remote-agent path. It is not the same as the root
`/SKILL.md`, which is for trusted owner/operator work such as backend setup,
self-hosted server maintenance, and memory-wiki jobs.

## Security Rules

- Treat the `fishdep:` token as a secret. Do not commit it, quote it in public
  logs, or send it to any service other than the local Fisherman CLI.
- Stay inside the granted scopes. If a command is denied, ask the user for a new
  Agent Access token with the right scope.
- Prefer concise summaries over dumping raw OCR or transcripts.
- Remote screenshot export is not currently implemented for deputy tokens.

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

Use Cloud/Self-hosted directly when the user's laptop may be offline:

```bash
fisherman query --source secondary --since 30m --limit 20 --text
```

Use laptop relay when the user specifically wants laptop-local context:

```bash
fisherman query --source primary --since 30m --limit 20 --text
```

Cloud/Self-hosted direct routing currently supports `status`, `query`, and
`transcripts`. Commands that need laptop-local state, such as `friend status`,
`publish-status`, `pause`, and `resume`, use the laptop relay path. If `primary`
fails, the user's laptop daemon is probably offline or not connected to the
relay. If `secondary` fails, the Cloud/Self-hosted backend may not be configured,
approved, reachable, or may not support that command.

## Scope Map

- `read:status` allows `fisherman status`.
- `read:captures` allows `fisherman query`.
- `read:transcripts` allows `fisherman transcripts`.
- `read:friends` allows `fisherman friend list` and `fisherman friend status`.
- `publish:status` allows `fisherman publish-status`.
- `control:pause` allows `fisherman pause` and `fisherman resume`.

Ask the user to revoke the token when the job is complete:

```bash
fisherman deputy revoke <name-or-pubkey>
```
