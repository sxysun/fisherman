# Recommended file layout for `<mind-root>`

This is the practical layout for a Fisherman-derived mind wiki. `<mind-root>`
is usually `~/mind`, unless the user names a different path or an existing
hosted setup already uses `/home/ubuntu/mind`.

## Mental model
Treat `<mind-root>` as three layers:
1. raw sources
2. compiled wiki
3. system / maintenance docs

The compiled wiki should be the first place an agent searches. Raw sources
should be consulted only after the compiled layer is insufficient.

## Core files
- `<mind-root>/rolling-summary.md`
- `<mind-root>/INDEX.md`
- `<mind-root>/wiki-upgrade-plan.md` when a migration is in progress

## Rolling-memory layer
- `<mind-root>/fisherman-digests/YYYY-MM-DD_HHMM.md`
- `<mind-root>/context-hours/YYYY-MM-DD/HH.md`
- `<mind-root>/context-entities/*.md`

## Source layer
- `<mind-root>/writings/`
- `<mind-root>/sources/`
- other imported docs/assets as needed

## Intended next-version structure

### Raw sources (immutable)
- `<mind-root>/sources/writings/`
- `<mind-root>/sources/docs/`
- `<mind-root>/sources/assets/`
- `<mind-root>/sources/fisherman-exports/` (optional)

### Compiled wiki (LLM-maintained)
- `<mind-root>/wiki/mocs/`
- `<mind-root>/wiki/entities/people/`
- `<mind-root>/wiki/entities/projects/`
- `<mind-root>/wiki/entities/companies/`
- `<mind-root>/wiki/entities/motifs/`
- `<mind-root>/wiki/areas/`
- `<mind-root>/wiki/timelines/hours/YYYY-MM-DD/HH.md`
- `<mind-root>/wiki/timelines/digests/YYYY-MM-DD_HHMM.md`
- `<mind-root>/wiki/syntheses/`

### System layer
- `<mind-root>/system/INDEX.md`
- `<mind-root>/system/file-structure.md`
- `<mind-root>/system/page-types.md`
- `<mind-root>/system/retrieval-order.md`
- `<mind-root>/system/search-playbook.md`

## Recommended page types
- MOC page
- entity page
- area/workstream page
- timeline hour page
- digest page
- synthesis page
- source note
- system/maintenance page

## Rationale by page type

### rolling-summary.md
Compact high-signal synthesis. Should answer: what is going on overall right now?

### INDEX.md
Root navigation layer. Should tell a future reader where to look next, not try to contain the whole wiki itself.

### fisherman-digests/
One file per review pass. Should answer: what did the agent conclude in this pass, with what confidence, and what changed on disk?

### context-hours/
One file per UTC hour with meaningful signal. Should answer: what detailed context was present in this hour, in searchable form?

### context-entities/
Durable pages for recurring people, projects, products, companies, chats, or motifs. Should answer: why does this thing matter, what is the current best read, and where is the evidence?

### MOCs
Map-of-content pages should become the main Obsidian-native navigation primitive over time. They should gather related links, not just summarize.

## Retrieval pattern
When trying to recover context later:
1. read relevant MOC pages first (if they exist)
2. read `rolling-summary.md` for top-level state
3. scan `INDEX.md` for navigation
4. read relevant entity pages
5. read relevant digest(s)
6. search `context-hours/` for names, products, chats, or phrases
7. only then go back to raw sources

## Search discipline for agents
When updating or searching the wiki:
- prefer the compiled layer before raw files
- preserve searchable names/terms exactly
- add wikilinks when a page is clearly a recurring concept
- preserve direct evidence vs inference vs uncertainty
- update navigation pages when new durable pages are created

## Migration guidance
Do not break the current live cron workflow just to get prettier structure.
Prefer staged migration:
1. improve templates/frontmatter/wikilinks first
2. add MOCs and navigation pages
3. only then consider moving folders into `sources/`, `wiki/`, and `system/`
