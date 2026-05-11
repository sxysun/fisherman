# Fisherman Skill References

There are two different agent handoff modes:

- **Trusted owner/operator:** use **[`/SKILL.md`](../SKILL.md)** at the repo
  root. This is for agents that are allowed to configure backends, set up
  self-hosted servers, query owner context directly, and maintain memory.
- **Scoped remote deputy:** use
  **[`fisherman-deputy-agent/SKILL.md`](fisherman-deputy-agent/SKILL.md)**.
  This is the copy/paste companion for Settings -> Agent Access tokens.

This folder also holds deeper, longer-form reference material:

- `fisherman-cli/SKILL.md` — full local-query playbook with every OCR/screenshot
  mismatch trap encountered in production and the recovery patterns for each
- `mind-rolling-summary/SKILL.md` — full memory-wiki maintenance procedure
- `mind-rolling-summary/templates/` — page templates for digests, hour notes, entity pages, and area pages
- `mind-rolling-summary/references/` — file-layout reference and the Obsidian-native LLM wiki design notes

Do not hand the root `/SKILL.md` to a scoped deputy unless you intentionally
want that agent to act as a trusted Fisherman operator. For a token-bearing
remote agent, hand it the deputy setup block and the deputy-agent skill.
