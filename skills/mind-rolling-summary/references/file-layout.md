# Recommended file layout for `/home/ubuntu/mind`

This is the practical layout for the current live system, plus the intended direction for a more Obsidian-native compiled wiki.

## Mental model
Treat `/home/ubuntu/mind` as three layers:
1. raw sources
2. compiled wiki
3. system / maintenance docs

The compiled wiki should be the first place Hermes searches. Raw sources should be consulted only after the compiled layer is insufficient.

## Current live core files
- `/home/ubuntu/mind/rolling-summary.md`
- `/home/ubuntu/mind/INDEX.md`
- `/home/ubuntu/mind/wiki-upgrade-plan.md`

## Current live rolling-memory layer
- `/home/ubuntu/mind/fisherman-digests/YYYY-MM-DD_HHMM.md`
- `/home/ubuntu/mind/context-hours/YYYY-MM-DD/HH.md`
- `/home/ubuntu/mind/context-entities/*.md`

## Current live source layer
- `/home/ubuntu/mind/writings/`
- `/home/ubuntu/mind/what-problem-next-5-years.txt`
- other imported docs/assets as needed

## Intended next-version structure

### Raw sources (immutable)
- `/home/ubuntu/mind/sources/writings/`
- `/home/ubuntu/mind/sources/docs/`
- `/home/ubuntu/mind/sources/assets/`
- `/home/ubuntu/mind/sources/fisherman-exports/` (optional)

### Compiled wiki (LLM-maintained)
- `/home/ubuntu/mind/wiki/mocs/`
- `/home/ubuntu/mind/wiki/entities/people/`
- `/home/ubuntu/mind/wiki/entities/projects/`
- `/home/ubuntu/mind/wiki/entities/companies/`
- `/home/ubuntu/mind/wiki/entities/motifs/`
- `/home/ubuntu/mind/wiki/areas/`
- `/home/ubuntu/mind/wiki/timelines/hours/YYYY-MM-DD/HH.md`
- `/home/ubuntu/mind/wiki/timelines/digests/YYYY-MM-DD_HHMM.md`
- `/home/ubuntu/mind/wiki/syntheses/`

### System layer
- `/home/ubuntu/mind/system/INDEX.md`
- `/home/ubuntu/mind/system/file-structure.md`
- `/home/ubuntu/mind/system/page-types.md`
- `/home/ubuntu/mind/system/retrieval-order.md`
- `/home/ubuntu/mind/system/search-playbook.md`

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

## Search discipline for Hermes
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
