# Context Migration

Fisherman never copies history automatically when you switch context
homes. New context goes to the selected home; old context stays where it
was until you export, import, or delete it.

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
