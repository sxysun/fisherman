---
name: fisherman
description: Trusted owner/operator fast track for Fisherman. Use for backend mode setup, self-hosted server installation, context queries, migration, diagnostics, and processor operations. Use fisherman-deputy-agent instead when the user gives you a fishdep token.
version: 2.0.0
license: MIT
---

# Fisherman Owner/Operator Fast Track

Use this skill only when the user trusts you as an owner/operator. If the
user gives you a `fishdep:` Agent Access token, stop here and use
[`skills/fisherman-deputy-agent/SKILL.md`](skills/fisherman-deputy-agent/SKILL.md)
instead.

The canonical copy of this skill also lives at
[`skills/fisherman-owner-operator/SKILL.md`](skills/fisherman-owner-operator/SKILL.md)
for agents that install skills from the `skills/` directory.

## Mental Model

Fisherman has three user-facing context homes:

- `local`: raw context stays on the Mac. Friend status can still use the
  encrypted relay.
- `cloud`: Fisherman Cloud, managed by this repo's CI/CD and gated by TDX
  attestation before raw upload.
- `self_hosted`: the same backend capability on infrastructure the user runs.

The packaged `fisherman` CLI is the only normal interface. Do not use
`server/cli.py` unless you are logged into a backend host and doing low-level
database/blob debugging.

## Self-Hosted Backend Fast Track

Goal: stand up `server/`, allowlist the user's Mac signing public key, keep the
Mac private key on the Mac, and return the exact client configure command.

1. On the Mac, get the signing public key:

```bash
fisherman friend code --text
```

Use the `signing:` value. Never ask for `FISH_PRIVATE_KEY`.

2. On the server, clone or update this repo:

```bash
git clone https://github.com/sxysun/fisherman.git
cd fisherman/server
```

3. Bootstrap the backend:

```bash
bash bootstrap-agent.sh --start \
  --public-url wss://your-host/ingest \
  --client-pubkey <mac-signing-public-key>
```

For persistent Linux/systemd operation after setup:

```bash
bash install-service.sh
sudo systemctl status fisherman-ingest --no-pager
```

4. Verify:

```bash
curl -fsS http://127.0.0.1:9998/health
```

5. On the Mac, configure the destination context home:

```bash
fisherman backend configure self-hosted --url wss://your-host/ingest
open /Applications/Fisherman.app
fisherman doctor
```

Report back:

- backend URL
- allowlisted Mac signing public key
- storage backend: local disk or R2
- process status and log path
- client command: `fisherman backend configure self-hosted --url <url>`

## Backend Commands

```bash
fisherman backend status
fisherman backend configure local
fisherman cloud audit https://fisherman.teleport.computer
fisherman backend configure cloud
fisherman backend configure self-hosted --url wss://your-host/ingest
```

Cloud strict mode requires live attestation approval. If approval fails, raw
uploads stay local and the durable upload queue holds frames.

## Query Context

Use these commands for owner/operator reads:

```bash
fisherman status --text
fisherman query --since 30m --limit 20 --text
fisherman query --since 4h --search "keyword" --limit 20 --text
fisherman transcripts --since 2h --limit 50 --text
fisherman screenshot --output /tmp/fisherman-latest.jpg
```

Route explicitly only while diagnosing:

```bash
fisherman query --source auto --since 30m --limit 20 --text
fisherman query --source primary --since 30m --limit 20 --text
fisherman query --source secondary --since 30m --limit 20 --text
```

Use `primary` for the laptop relay/control path. Use `secondary` for Cloud or
self-hosted backend reads. Default to `auto`.

Evidence rules:

- Treat screenshots as stronger evidence than OCR when they disagree.
- Treat app/window labels as useful but fallible.
- Keep direct evidence separate from inference.
- Refresh once before finalizing if the user is actively working.

## Context Migration

Switching context homes affects new uploads only. Move history explicitly:

```bash
fisherman context export --home active --output fisherman-history.json --since 30d
fisherman context export --home active --output fisherman-history-with-images.json --since 7d --include-images
fisherman context import fisherman-history.json --home active
fisherman context delete --home active --since 30d --dry-run
fisherman context delete --home active --since 30d --confirm DELETE
```

Exports are private JSON files. Screenshot exports can be large; backend
imports are chunked by the CLI, so do not hand-roll upload scripts.

## Agent Access

Owners create scoped tokens:

```bash
fisherman deputy new --name agent --scopes read:captures,read:screenshots,read:transcripts --expires 30d
fisherman deputy list --text
fisherman deputy revoke <name-or-pubkey>
```

Give the remote agent the printed setup block plus
[`skills/fisherman-deputy-agent/SKILL.md`](skills/fisherman-deputy-agent/SKILL.md).

## Processors

Processors are the supported automation surface for custom distillation:

```bash
fisherman processor list --text
fisherman processor install ./processor.json
fisherman processor run status-loop --since 10m --limit 50
fisherman processor schedule add hourly-status status-loop --every 60m --since 60m
fisherman processor schedule list --text
```

Custom processors receive recent context JSON on stdin and return JSON on
stdout. They can run locally today; backend/cloud scheduling should be treated
as an operator capability unless the active deployment explicitly supports it.

## Privacy Rules

- Local Only: raw context stays on the Mac.
- Fisherman Cloud: raw upload requires approved TDX attestation unless the user
  explicitly enables the dangerous bypass.
- Self-hosted: the user trusts the server/operator.
- Friend relay: stores signed ciphertext only.
- Deputy tokens: scoped, revocable, and rate-limited; raw screenshots require
  `read:screenshots`.

## Useful Paths

| What | Path |
|---|---|
| Mac config | `~/.fisherman/.env` |
| Mac logs | `~/.fisherman/fisherman.log` |
| Mac local frames | `~/.fisherman/frames/` |
| Server code | `server/` |
| Server env | `server/.env` |
| Server logs | `server/ingest.log` or `journalctl -u fisherman-ingest -f` |
| Cloud deploy docs | `docs/tee-deployment.md`, `docs/cloud-operations.md` |
