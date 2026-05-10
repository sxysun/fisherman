# Contributing

Fisherman is privacy-sensitive software. Treat every change as if it could
affect raw screen context, transcripts, friend-status sharing, or backend
tenant isolation.

## Development Setup

```bash
git clone https://github.com/sxysun/fisherman.git
cd fisherman
uv sync
uv run fisherman --help
```

The macOS menu bar app builds from `menubar/`:

```bash
cd menubar
swift build -c release
```

## Expected Checks

Before opening a PR, run the smallest relevant set:

```bash
uv run pytest
cd menubar && swift build -c release
```

For server or Cloud changes, also run the targeted tests:

```bash
uv run pytest tests/test_cloud_tenancy.py tests/test_cloud_trust.py tests/test_ingest_startup.py
```

## Privacy Review Checklist

- Does this change move raw screen context, OCR text, transcripts, screenshots, prompts, API keys, or friend status?
- Is the data scoped to the correct context home: Local Only, Fisherman Cloud, or Self-hosted?
- If Cloud is involved, is raw ingest still blocked until attestation and release approval pass?
- If a backend is involved, does every read/write enforce tenant identity or owner/deputy authorization?
- Are audit logs metadata-only?
- Does the UI say where data is going and whether history is copied automatically?

## Pull Requests

Keep PRs narrow and describe:

- user-facing behavior
- privacy/security impact
- migration or compatibility notes
- tests run

Do not include real context archives, screenshots, `.env` files, private keys,
OpenRouter/OpenAI keys, Phala tokens, or server credentials in issues or PRs.
