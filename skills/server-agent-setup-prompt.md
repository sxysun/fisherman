# Agent-managed Fisherman server setup

Use this when you want an agent (Hermes, OpenCode, etc.) to handle the server side end-to-end.

## Fast shell alternative

If the agent prefers a shell entrypoint, it can run:

```bash
cd server
bash bootstrap-agent.sh --start
```

## Agent prompt

```text
Set up the Fisherman server from this repo. Handle server deployment end-to-end, including environment setup, dependency installation, Postgres, auth token generation or selection, encryption key setup, and starting the ingest service. Use the repo-local skills in `skills/fisherman-cli/` and `skills/mind-rolling-summary/`. When done, tell me the server WebSocket URL and the auth token the client should use. If an auth token already exists, explain whether you kept it or replaced it.
```

## Auth model

- `INGEST_AUTH_TOKEN` is just a shared bearer password between client and server.
- `setup.sh` auto-generates one for convenience.
- You can also manually set it yourself in `server/.env`.
- The client must use the same value as `FISH_AUTH_TOKEN`.

## Expected output from the agent

The agent should report back:
- server WebSocket URL (for example `ws://your-host:9999/ingest` or `wss://your-host/ingest`)
- auth token to paste into the client
- whether storage is local or R2-backed
- whether the ingest process is running now and how it is being run
