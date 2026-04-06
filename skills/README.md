# Fisherman skill references

The canonical agent entry point is **[`/SKILL.md`](../SKILL.md)** at the repo root. It covers all three phases — server setup, querying captured data, and maintaining the rolling-memory wiki — and links into this directory for the deep operational reference.

This folder holds the deeper, longer-form material that `SKILL.md` links to:

- `fisherman-cli/SKILL.md` — full query playbook with every OCR/screenshot mismatch trap encountered in production and the recovery patterns for each
- `mind-rolling-summary/SKILL.md` — full memory-wiki maintenance procedure
- `mind-rolling-summary/templates/` — page templates for digests, hour notes, entity pages, and area pages
- `mind-rolling-summary/references/` — file-layout reference and the Obsidian-native LLM wiki design notes

Agents should read `/SKILL.md` first and only open these files when they need the deeper reference.
