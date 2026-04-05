# Obsidian-native LLM wiki pattern for `/home/ubuntu/mind`

This reference captures the intended pattern for the next version of the mind system.

## Core idea
Do not treat `/home/ubuntu/mind` as a passive RAG dump or a folder of raw notes that must be rediscovered from scratch each time.
Treat it as a persistent compiled wiki that sits between raw sources and future reasoning.

The LLM should:
- read new sources
- extract durable structure
- update existing pages
- strengthen or challenge prior synthesis
- preserve contradictions / uncertainty
- maintain cross-links, MOCs, summaries, and entity pages

The human should mainly:
- curate source material
- ask good questions
- decide what matters
- browse and validate the evolving wiki

## Three-layer architecture

### 1. Raw sources
Immutable source-of-truth material.
Examples:
- writings
- docs
- screenshots/assets
- Fisherman exports when preserved intentionally

The LLM reads these but should not rewrite them unless explicitly asked.

### 2. Compiled wiki
The real working memory layer.
This is where the LLM should accumulate knowledge over time.

The wiki should contain:
- map-of-content pages (MOCs)
- entity pages
- workstream/area pages
- timeline hour pages
- digest pages
- synthesis pages
- navigation/index pages

This compiled layer should be searched before raw sources whenever possible.

### 3. System layer
Pages that describe how the wiki works.
Examples:
- file structure
- page types
- retrieval order
- search playbook
- maintenance rules

## Why this is better than plain RAG
RAG rediscovers knowledge from raw chunks every time.
This wiki pattern compiles knowledge once and keeps it current.

The value comes from persistent maintenance:
- cross-references are already there
- contradictions are already noted
- summaries already reflect prior reading
- entity pages already aggregate scattered evidence
- future answers can start from the compiled synthesis instead of rebuilding from scratch

## Obsidian-native conventions

### Wikilinks
Use Obsidian-style internal links when naming recurring pages, entities, projects, or topics.
Examples:
- `[[Teleport]]`
- `[[OpenClaw]]`
- `[[Branding Website]]`
- `[[MOC - Hardware Wedge]]`

### Frontmatter
Prefer YAML frontmatter on durable compiled-wiki pages.
Good fields include:
- `type`
- `status`
- `aliases`
- `tags`
- `last_updated`
- `source_count`
- `time_span`
- `entities`
- `moc`

### MOCs
Do not rely only on one giant index page.
Create map-of-content pages that gather links and give shape to the wiki.
Suggested first MOCs:
- `MOC - Self Model`
- `MOC - Teleport`
- `MOC - Hardware Wedge`
- `MOC - Collaborators`
- `MOC - Active Workstreams`
- `MOC - Fisherman Timeline`
- `MOC - Sources`

### Backlink discipline
Whenever updating a page, ask:
- what should this page link to?
- what should link back here?
- which MOC should include this page?
- which entities should become wikilinks?

## Retrieval order for Hermes
Default retrieval order should be:
1. MOC pages
2. synthesis pages
3. entity pages
4. timeline pages (hours / digests)
5. raw sources only if needed

This makes the wiki the first-class memory layer.

## Search order for Hermes
When trying to find something later:
1. search MOCs and synthesis pages
2. search entity pages
3. search timeline hours and digests
4. search raw sources
5. if needed, return to Fisherman or the original source material

## What to optimize for
- compounding synthesis rather than repeated rediscovery
- legible structure
- stable page types
- explicit uncertainty
- low maintenance burden for the human
- easy browsing in Obsidian graph/search/backlinks/Dataview

## Important rule for future rolling-summary maintenance
When updating `/home/ubuntu/mind`, write as if you are maintaining a real wiki, not just appending logs.
That means:
- update summaries instead of duplicating them
- connect related pages
- preserve durable concepts in entities/MOCs
- keep raw sources separate from compiled interpretation
- make search easier for your future self
