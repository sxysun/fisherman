# Context Migration

Fisherman never copies history automatically when you switch context
homes. New context goes to the selected home; old context stays where it
was until you export, import, or delete it.

That behavior is deliberate. Local Only, Fisherman Cloud, and
Self-hosted are different trust domains, so migration is an explicit
user action rather than silent background sync.

## Switching Homes

The safe switch pattern is:

```bash
# 1. Export from the source while it is still active.
fisherman context export --home active --output fisherman-context.json --since 30d

# 2. Point Fisherman at the destination.
fisherman backend configure self-hosted --url wss://your-host/ingest
# or:
fisherman backend configure cloud
# or:
fisherman backend configure local

# 3. Import into the destination.
fisherman context import fisherman-context.json --home active

# 4. Optional cleanup after a dry run.
fisherman context delete --home active --since 30d --dry-run
fisherman context delete --home active --since 30d --confirm DELETE
```

This is copy-then-optionally-delete, not live bidirectional sync. That
keeps the failure mode simple: if an import fails, the original home
still has the data.

## Export

```bash
fisherman context export --home active --output context.json --since 30d
```

Targets:

- `--home active`: current Settings choice
- `--home local`: `~/.fisherman/frames` and `~/.fisherman/audio`
- `--home backend`: current Cloud or Self-hosted backend

Screenshots are excluded by default:

```bash
fisherman context export --home active --output context-with-images.json --since 7d --include-images
```

Treat image archives as highly private. They are chmod `0600` on export.

## Import

Switch to the destination context home first, then import:

```bash
fisherman backend configure self-hosted --url wss://your-host:9999/ingest
fisherman context import context.json --home active
```

Import adds rows to the destination. It does not delete the source.

## Delete

Dry run first:

```bash
fisherman context delete --home active --since 30d --dry-run
```

Delete requires an explicit confirmation:

```bash
fisherman context delete --home active --since 30d --confirm DELETE
```

For a complete wipe of the selected home:

```bash
fisherman context delete --home active --all --confirm DELETE
```

## Backend API

Cloud and self-hosted expose the same migration surface:

- `GET /api/context/export`
- `POST /api/context/import`
- `DELETE /api/context`

All backend calls require FishKey auth. Fisherman Cloud also requires an
approved Cloud release and the client-held tenant data key.
