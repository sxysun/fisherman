---
name: fisherman-cli
description: Query and decrypt Fisherman captured user-activity context from the local server repo. Use this to inspect recent frames, search OCR/window text, summarize activity by app, and export screenshots.
version: 1.0.0
author: Hermes Agent
license: MIT
---

# Fisherman CLI

Use this skill when you need context from the local Fisherman capture system.

Repo/layout assumptions:
- Repo root: `/home/ubuntu/fisherman`
- Query CLI lives in: `/home/ubuntu/fisherman/server`
- Server `.env` should already contain `DATABASE_URL` and `ENCRYPTION_KEY`

Important current quirk:
- `uv run fisherman ...` does not currently work in `server/` because entrypoints are skipped (`project.scripts` not installed for this un-packaged project).
- Reliable invocation is: `uv run python cli.py ...`

## Core commands

Run from `/home/ubuntu/fisherman/server`.

### Recent activity as JSON
```bash
uv run python cli.py query -j --limit 20
```

### Search OCR / window titles / scene text
```bash
uv run python cli.py query -j --search "keyword" --limit 20
```

### Filter by app and time range
```bash
uv run python cli.py query -j --app "Telegram" --since "2h ago" --limit 20
uv run python cli.py query -j --app "Chrome" --since "2026-04-01T09:00:00"
```

### High-level summary by app
```bash
uv run python cli.py summary
uv run python cli.py summary --since "2h ago"
uv run python cli.py summary --app "Telegram"
```

### Full detail for a frame
```bash
uv run python cli.py show 123
uv run python cli.py show 123 -o /tmp/frame_123.jpg
```

### Download/decrypt an image directly from image_key
```bash
uv run python cli.py image "frames/2026-04-01/12345.jpg.enc" -o /tmp/frame.jpg
```

## What the commands return
- `query -j`: structured JSON, best for agent reasoning
- `summary`: grouped activity by app, window titles, and URLs
- `show`: one fully decrypted frame record, optionally saving its screenshot
- `image`: decrypted JPEG written to disk

## Recommended usage pattern for context gathering
1. Start with a broad summary:
   - `uv run python cli.py summary --since "2h ago"`
2. Then get recent JSON frames:
   - `uv run python cli.py query -j --limit 20`
   - If the strict `--since "2h ago"` window is empty but there are clearly recent same-day frames just outside that boundary, widen to a nearby continuity window (for example `6h ago`) and explicitly label the result as a **continuity / clarification pass**, not a fresh directional update.
   - Watch for **boundary slippage** on these continuity pulls: a strict `6h ago` window can come back empty even when the latest same-session burst is only a few minutes older than that boundary. If that happens, widen slightly again (for example `7h ago`) and report that this recovered the same prior burst rather than any newer activity.
   - In practice, if the same-day burst is materially older than that and you still need continuity, it is acceptable to widen further (for example `8h ago` or `10h ago`) as long as you explicitly say you are recovering the **same already-known burst** rather than claiming fresh activity.
3. If the broad JSON is noisy or malformed, narrow by app or keyword immediately:
   - `uv run python cli.py query -j --app "Telegram" --since "2h ago"`
   - `uv run python cli.py query -j --app "WeChat" --since "2h ago"`
   - `uv run python cli.py query -j --app "Google Chrome" --since "2h ago"`
   - `uv run python cli.py query -j --search "meeting"`
4. If a frame matters visually, export it:
   - `uv run python cli.py show <id> -o /tmp/frame.jpg`
5. For dense recent windows, do **targeted follow-up pulls** by app after the broad query:
   - `uv run python cli.py query -j --app "Telegram" --since "2h ago" --limit 50`
   - `uv run python cli.py query -j --app "Google Chrome" --since "2h ago" --limit 80`
   - `uv run python cli.py query -j --app "Lark" --since "2h ago" --limit 20`
   This is especially useful when you want high-signal collaboration / browser / chat context without flooding yourself with every frame.
6. If OCR/chat snippets are important but noisy, inspect a representative frame visually:
   - `uv run python cli.py show <id> -o /tmp/frame.jpg`
   Then use vision on the exported image to recover chat names, timestamps, and message snippets more reliably than OCR alone.
   - **Verify the exported image actually matches the frame metadata/window you expected before relying on it.** In practice, OCR/window metadata can occasionally disagree with the exported screenshot, so do a quick sanity check (app/window/title/theme) first.
   - If the screenshot appears to be from a different app/window than the frame metadata suggested, treat that single visual export as unreliable and fall back to repeated textual evidence across nearby frames (`summary`, targeted `query -j --app ...`, and keyword searches) rather than overfitting to one mismatched image.
   - In mixed-language chat apps like **WeChat**, OCR may look nearly unusable while the actual screenshot still contains high-signal content. When that happens, trust the **visually recovered conversation structure/content** more than the OCR blob, but mark any uncertain lines explicitly.
   - For **Telegram**, visual inspection is also useful even when OCR is already good: it can distinguish a real self-authored longform draft / bot conversation from a noisy OCR fragment and confirm nearby collaborator/chat-list context.
  - When a Telegram frame shows longform text, explicitly determine **who is speaking** before you summarize it as the user's latest self-account. In practice, the latest visible frame may be an assistant/bot reply that is paraphrasing the user rather than the user's own fresh writing. Compare at least one or two nearby Telegram frames so you can separate **earlier self-account** from **later assistant reflection/synthesis**.
  - If a new visual inspection contradicts an earlier continuity-pass read (for example, the latest WeChat frame turns out to be an AI/model discussion group rather than the previously inferred collaborator thread, **or swings back the other way on re-inspection**), **treat that as a correction**, lower confidence in the earlier inference, and update the log accordingly instead of silently carrying the old interpretation forward.
  - For noisy WeChat windows, inspect **more than one nearby frame** before locking the narrative. In practice, adjacent frames can alternate between misleading OCR, blank window metadata, compressed chat-list views, deletion modals, and much clearer screenshots of the same moment. A second or third nearby export can materially change whether the best read is **collaborator/product chat**, **personal-social context**, **chat cleanup**, or **AI/model discussion**.
  - In WeChat desktop screenshots, carefully separate **(a) the active conversation title at the top**, **(b) other chats merely visible in the left chat list**, and **(c) any background app/window bleeding through behind WeChat**. Vision models can easily over-index on a visible chat-list name like `SUN` even when the actual open conversation is another thread (for example `七楼`), especially when Chrome/YouTube is still visible behind the WeChat window.
  - A useful recovery pattern: if the latest WeChat frame looks like generic chat-list noise, inspect one or two slightly earlier exports from the same burst. Those clearer nearby frames can restore the real foreground narrative — e.g. explicit discussion of **cofounder/core-product gaps**, **CEO insight**, or **personal-data-onchain / interop** — and prevent over-downgrading the thread into mere social chatter.
  - In **Chrome/browser** frames, the exported screenshot can also disagree with the frame's window/title metadata or your intended target tab. This can happen even when the app is correct but the visible page is a different browser state than the metadata implied. If that happens, **do not overfit to a single export**; prefer repeated OCR/window evidence across nearby frames and anchor on visually stable pages/documents that recur across multiple frames.
  - If one browser export claims to show a specific page (for example Claude, X, or a company profile) but the image visually shows another page/state, downgrade confidence in that single-frame claim and explicitly report the mismatch.
  - Be alert for **cross-app mismatch**, not just wrong-tab mismatch. In practice, a frame labeled as one app can occasionally export a screenshot that visibly belongs to another app entirely (for example: Telegram metadata exporting a GitHub/Chrome screenshot, Chrome metadata exporting a Lark screenshot, or WeChat metadata exporting a Claude/Chrome screenshot). When this happens, do **not** force the image back onto the metadata label. Instead, treat the image as evidence of desktop attention at that timestamp, rely more heavily on repeated textual/app-level evidence across nearby frames, and lower confidence in any app-specific claim drawn from that single export.
  - Also watch for **mixed-surface / overlay captures**: the exported screenshot can contain a browser or desktop background plus a foreground chat window from another app, so the visible image is genuinely composite rather than simply “wrong app.” In those cases, record both surfaces explicitly (for example: `Chrome/Google background + WeChat foreground`, or `browser page with chat overlay`) and anchor conclusions on the **foreground readable content** plus repeated nearby evidence.
  - If OCR or metadata implies one live page/state (for example X, ChatGPT, DeepSeek, or a research tab bundle) but the visual export shows an intermediate state like **Cloudflare / security verification**, trust the visual export for that frame and treat the OCR/tab bundle as surrounding browser context rather than proof of the currently visible page.
7. Before treating a pass as a fresh directional update, compare the **latest frame timestamp** to the current clock time. If the 2h query is non-empty but the latest captured frame is materially earlier than now, report it as a **continuity / clarification pass** ("no newer post-<ts> activity") rather than overstating it as new movement.
8. If `query -j` output fails JSON parsing because decrypted OCR contains bad escape sequences, **do not trust a full-window parse**. Fall back to:
   - `summary --since ...` for the broad app/window picture
   - smaller **per-app `query -j` pulls** (Telegram / WeChat / Chrome etc.)
   - `show <id> -o /tmp/frame.jpg` + vision for the highest-signal frames
   In practice this is the most reliable way to recover usable context when a large all-app JSON dump is malformed by OCR text.

## Constraints / caveats
- OCR text is encrypted at rest, so text search happens client-side after decryption.
- `query` returns decrypted OCR/window/scene/urls.
- JSON output may contain sensitive user context; summarize carefully.
- Prefer short time windows or app filters to avoid flooding context.

## Troubleshooting
- If commands fail, check that `/home/ubuntu/fisherman/server/.env` exists.
- Required env vars: `DATABASE_URL`, `ENCRYPTION_KEY`
- If `uv run fisherman ...` fails with entrypoint errors, use `uv run python cli.py ...` instead.
