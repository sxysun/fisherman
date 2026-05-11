---
name: fisherman-owner-operator
description: Trusted owner/operator fast track for Fisherman self-hosted setup, backend mode selection, context queries, data migration, diagnostics, and processor operations. Use fisherman-deputy-agent instead when the user gives you a fishdep token.
version: 2.0.0
license: MIT
---

# Fisherman Owner/Operator

Use this skill only when the user trusts you to operate Fisherman on their
behalf. If the user gives you a `fishdep:` Agent Access token, use
[`../fisherman-deputy-agent/SKILL.md`](../fisherman-deputy-agent/SKILL.md)
instead.

## First Checks

```bash
fisherman version
fisherman doctor
fisherman backend status
```

Fisherman has three context homes:

- Local Only: raw context stays on the Mac.
- Fisherman Cloud: managed Cloud backend, gated by TDX attestation.
- Self-hosted: backend infrastructure the user operates.

The packaged `fisherman` CLI is the canonical interface. Use `server/cli.py`
only for low-level backend-host debugging.

## Self-Hosted Fast Track

1. On the user's Mac, get the signing public key:

```bash
fisherman friend code --text
```

Use the `signing:` value. Never ask for or copy the Mac private key.

2. On the server:

```bash
git clone https://github.com/sxysun/fisherman.git
cd fisherman/server
bash bootstrap-agent.sh --start \
  --public-url wss://your-host/ingest \
  --client-pubkey <mac-signing-public-key>
```

3. For persistent Linux operation:

```bash
bash install-service.sh
curl -fsS http://127.0.0.1:9998/health
sudo systemctl status fisherman-ingest --no-pager
```

4. On the Mac:

```bash
fisherman backend configure self-hosted --url wss://your-host/ingest
open /Applications/Fisherman.app
fisherman doctor
```

Report the backend URL, allowlisted public key, storage backend, process status,
log path, and exact Mac configure command.

## Backend Modes

```bash
fisherman backend configure local
fisherman cloud audit https://fisherman.teleport.computer
fisherman backend configure cloud
fisherman backend configure self-hosted --url wss://your-host/ingest
```

Cloud strict mode must pass attestation before raw upload. Self-hosted mode
means the user trusts the server operator.

## Query Context

```bash
fisherman status --text
fisherman query --since 30m --limit 20 --text
fisherman query --since 4h --search "keyword" --limit 20 --text
fisherman transcripts --since 2h --limit 50 --text
fisherman screenshot --output /tmp/fisherman-latest.jpg
```

Use `--source auto` by default. Use `--source primary` for laptop relay/control
diagnostics and `--source secondary` for Cloud/self-hosted backend reads.

## Migration

Switching backend mode only changes future uploads. Move history explicitly:

```bash
fisherman context export --home active --output fisherman-history.json --since 30d
fisherman context export --home active --output fisherman-history-with-images.json --since 7d --include-images
fisherman context import fisherman-history.json --home active
fisherman context delete --home active --since 30d --dry-run
fisherman context delete --home active --since 30d --confirm DELETE
```

Exports are private JSON files. Screenshot imports are chunked by the CLI; do
not upload large archives with ad hoc scripts.

## Agent Access

```bash
fisherman deputy new --name agent --scopes read:captures,read:screenshots,read:transcripts --expires 30d
fisherman deputy list --text
fisherman deputy revoke <name-or-pubkey>
```

Give scoped remote agents the printed setup block and the deputy-agent skill,
not this owner/operator skill.

## Processors

```bash
fisherman processor list --text
fisherman processor install ./processor.json
fisherman processor run status-loop --since 10m --limit 50
fisherman processor schedule add hourly-status status-loop --every 60m --since 60m
fisherman processor schedule list --text
```

Processors receive context JSON on stdin and return JSON on stdout. Treat
backend/Cloud scheduling as an operator capability unless the active deployment
explicitly supports it.

## Evidence Discipline

- Prefer repeated evidence across frames over one frame.
- Treat OCR, app labels, and window titles as fallible.
- Use screenshots when visual evidence matters.
- Separate direct evidence from inference and uncertainty.
- Refresh once before finalizing if the user is actively working.
