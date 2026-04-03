# Recommended file layout for `/home/ubuntu/mind`

## Core files
- `/home/ubuntu/mind/rolling-summary.md`
- `/home/ubuntu/mind/INDEX.md`

## Detailed rolling memory
- `/home/ubuntu/mind/fisherman-digests/YYYY-MM-DD_HHMM.md`
- `/home/ubuntu/mind/context-hours/YYYY-MM-DD/HH.md`

## Rationale

### rolling-summary.md
Compact high-signal synthesis. Should answer: what is going on overall?

### fisherman-digests/
One file per review pass. Should answer: what did the agent conclude during this pass, with what confidence?

### context-hours/
One file per UTC hour with meaningful signal. Should answer: what detailed context was present in this hour, in searchable form?

## Retrieval pattern

When trying to recover context later:
1. read `rolling-summary.md` for top-level state
2. scan `INDEX.md` for relevant timestamps
3. read the relevant digest(s)
4. search `context-hours/` for names, products, chats, or phrases
