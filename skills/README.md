# Fisherman Skills

This directory intentionally has only two supported handoff surfaces:

- [`fisherman-owner-operator/SKILL.md`](fisherman-owner-operator/SKILL.md)
  for trusted agents that can configure backends, operate a self-hosted server,
  run migrations, and query owner context directly.
- [`fisherman-deputy-agent/SKILL.md`](fisherman-deputy-agent/SKILL.md)
  for scoped remote agents that receive a `fishdep:` Agent Access token.

The old standalone CLI skill was removed because the packaged `fisherman`
command is now the canonical CLI for every context home. The durable mind
procedure was folded into the deputy skill, where it is most useful for
long-running scoped agents.

Do not hand the owner/operator skill to a token-bearing deputy unless the user
intentionally wants that agent to act as a trusted Fisherman operator.
