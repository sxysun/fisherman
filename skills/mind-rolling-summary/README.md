# mind-rolling-summary (mirrored from server)

This skill is the production version of the rolling-summary pattern,
copied from the self-hosted hermes-agent install at
`~/.hermes/skills/productivity/mind-rolling-summary/SKILL.md` on
`3.82.134.133`.

It's the skill the four active cron jobs invoke on the server
(`fisherman-mind-digest`, `fisherman-distillation-maintenance`,
`fisherman-distillation-archive-deepening`, `daily-central-thread-steering`)
to maintain `/home/ubuntu/mind/` as a continuously-updated Obsidian-native
compiled wiki — rolling summary, digests, context-hours, entity pages,
MOCs, distillation layer.

## Companion skills on the server (not yet mirrored)
- `fisherman-deputy-remote-access` — how to read Fisherman context via
  the deputy CLI from a remote agent host
- `fisherman-cognition-distillation-cron` — sets up the dual-cron
  (backfill + maintenance) that drives this skill
- `central-thread-proof-surface` — finds the real central thread in
  multi-surface context
- 20+ specialized fisherman-* skills for edge cases (micro-window
  catchup, mid-burst detour weighting, multi-direction export
  inversion, archive-deepening proof windows, etc.)

If you want any of these mirrored locally too, pull them from the
same server path.
