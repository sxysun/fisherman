---
name: fisherman-cli
description: Query and decrypt Fisherman captured user-activity context from the local server repo. Use this for reliable evidence gathering — recent frames, OCR/window search, app summaries, and screenshot export — especially when feeding the compiled /home/ubuntu/mind wiki.
version: 1.0.0
author: Hermes Agent
license: MIT
---

# Fisherman CLI

Use this skill when you need context from the local Fisherman capture system.

When the task is durable memory/wiki maintenance, pair this skill with `mind-rolling-summary` and treat `/home/ubuntu/mind` as a compiled wiki layer rather than a raw log dump. Evidence gathering happens here; synthesis, navigation, cross-linking, and persistent page maintenance happen in the rolling-summary layer.

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
   - Important frame-cap lesson: both `summary --since ...` and `query -j --since ... --limit N` can make a **dense latest burst hide earlier activity in the same time window**. If the user asks about the last `4h` / `6h` / same-day span and the first result looks suspiciously like only the newest 20-30 minutes, do **not** assume the rest of the window was empty.
   - In those cases, immediately run a second check that is **not dominated by the latest burst**, for example:
     - a direct DB-style count by hour / app if available in the environment
     - or narrower per-app summaries / pulls across the same full window
     - or a widened continuity pass plus timestamp comparison against the latest and earliest returned frames
   - A practical sign of this failure mode: the command returns exactly the maximum/default number of frames (for example 200) and they all cluster tightly in the latest minutes. Treat that as possible **window truncation by recency density**, not proof that the broader requested period was quiet.
6. If OCR/chat snippets are important but noisy, inspect a representative frame visually:
   - `uv run python cli.py show <id> -o /tmp/frame.jpg`
   Then use vision on the exported image to recover chat names, timestamps, and message snippets more reliably than OCR alone.
   - **Verify the exported image actually matches the frame metadata/window you expected before relying on it.** In practice, OCR/window metadata can occasionally disagree with the exported screenshot, so do a quick sanity check (app/window/title/theme) first.
   - If the screenshot appears to be from a different app/window than the frame metadata suggested, treat that single visual export as unreliable and fall back to repeated textual evidence across nearby frames (`summary`, targeted `query -j --app ...`, and keyword searches) rather than overfitting to one mismatched image.
   - Also be aware that the **vision model itself can misclassify low-height, partial, or cross-app screenshots** (for example calling a Telegram export a browser page, or calling a mislabeled Lark export “ChatGPT”). So do a **two-step sanity check**: first read the raw `show <id>` metadata/OCR dump, then inspect the exported image, and only trust conclusions that survive both checks or are reinforced by nearby frames.
   - In mixed-language chat apps like **WeChat**, OCR may look nearly unusable while the actual screenshot still contains high-signal content. When that happens, trust the **visually recovered conversation structure/content** more than the OCR blob, but mark any uncertain lines explicitly.
   - For **Telegram**, visual inspection is also useful even when OCR is already good: it can distinguish a real self-authored longform draft / bot conversation from a noisy OCR fragment and confirm nearby collaborator/chat-list context.
  - When a Telegram frame shows longform text, explicitly determine **who is speaking** before you summarize it as the user's latest self-account. In practice, the latest visible frame may be an assistant/bot reply that is paraphrasing the user rather than the user's own fresh writing. Compare at least one or two nearby Telegram frames so you can separate **earlier self-account** from **later assistant reflection/synthesis**.
  - For macOS **Preview / PDF-reading** windows, do not stop at one bad OCR blob. Export and inspect **multiple adjacent frames** from the same reading burst because nearby pages can be much more legible than the representative OCR field, and a small run of adjacent screenshots can often recover the real reading context (document identity, chapter/page range, topic, and representative paragraphs) even when the stored OCR looks unusable.
  - In those Preview/PDF cases, explicitly distinguish three confidence levels: **(a) frame/app confidence** (`Preview` + PDF filename), **(b) screenshot-level topical confidence** (what the nearby visible pages are about), and **(c) exact-text confidence** (whether the page is readable enough to quote precisely). Report high confidence on (a), moderate/high on (b) only if nearby screenshots agree, and low on (c) unless the exported page text is actually legible.
  - If a new visual inspection contradicts an earlier continuity-pass read (for example, the latest WeChat frame turns out to be an AI/model discussion group rather than the previously inferred collaborator thread, **or swings back the other way on re-inspection**), **treat that as a correction**, lower confidence in the earlier inference, and update the log accordingly instead of silently carrying the old interpretation forward.
  - For noisy WeChat windows, inspect **more than one nearby frame** before locking the narrative. In practice, adjacent frames can alternate between misleading OCR, blank window metadata, compressed chat-list views, deletion modals, and much clearer screenshots of the same moment. A second or third nearby export can materially change whether the best read is **collaborator/product chat**, **personal-social context**, **chat cleanup**, or **AI/model discussion**.
  - In WeChat desktop screenshots, carefully separate **(a) the active conversation title at the top**, **(b) other chats merely visible in the left chat list**, and **(c) any background app/window bleeding through behind WeChat**. Vision models can easily over-index on a visible chat-list name like `SUN` even when the actual open conversation is another thread (for example `七楼`), especially when Chrome/YouTube is still visible behind the WeChat window.
  - The same trap applies to **Lark/Feishu**: do not summarize a frame by a sidebar-visible workspace/thread name if the actual open conversation in the main pane is different. In practice, a frame can show `Feedling Design & Dev` in the left list while the true foreground chat is something else like **`Design >>`** or **`Sam Gu`**.
  - A useful recovery pattern: if the latest WeChat frame looks like generic chat-list noise, inspect one or two slightly earlier exports from the same burst. Those clearer nearby frames can restore the real foreground narrative — e.g. explicit discussion of **cofounder/core-product gaps**, **CEO insight**, or **personal-data-onchain / interop** — and prevent over-downgrading the thread into mere social chatter.
  - In **Chrome/browser** frames, the exported screenshot can also disagree with the frame's window/title metadata or your intended target tab. This can happen even when the app is correct but the visible page is a different browser state than the metadata implied. If that happens, **do not overfit to a single export**; prefer repeated OCR/window evidence across nearby frames and anchor on visually stable pages/documents that recur across multiple frames.
  - If one browser export claims to show a specific page (for example Claude, X, or a company profile) but the image visually shows another page/state, downgrade confidence in that single-frame claim and explicitly report the mismatch.
  - A recurring high-value trap: **Apple-signing / notarization setup** can appear as a mixed Chrome+Lark workflow. OCR/metadata may say `Manage your Apple Account` or show `account.apple.com`, while the exported image actually shows a **Lark chat with Apple-signing env vars / app-specific-password instructions** in the foreground (or vice versa). In those cases, keep the higher-level inference (**Mac signing/notarization work is real**) but avoid claiming the exact foreground app/page for that frame unless repeated nearby evidence confirms it.
  - Be alert for **cross-app mismatch**, not just wrong-tab mismatch. In practice, a frame labeled as one app can occasionally export a screenshot that visibly belongs to another app entirely (for example: Telegram metadata exporting a GitHub/Chrome screenshot, Chrome metadata exporting a Lark screenshot, WeChat metadata exporting a Claude/Chrome screenshot, or Discord metadata exporting a different Discord/Meet/VS Code state than the OCR/window text claimed). When this happens, do **not** force the image back onto the metadata label. Instead, treat the image as evidence of desktop attention at that timestamp, rely more heavily on repeated textual/app-level evidence across nearby frames, and lower confidence in any app-specific claim drawn from that single export.
  - Also watch for **mixed-surface / overlay captures**: the exported screenshot can contain a browser or desktop background plus a foreground chat window from another app, so the visible image is genuinely composite rather than simply “wrong app.” In those cases, record both surfaces explicitly (for example: `Chrome/Google background + WeChat foreground`, or `browser page with chat overlay`) and anchor conclusions on the **foreground readable content** plus repeated nearby evidence.
  - A specific recurring trap: frames labeled as **Telegram** can export as a broader **browser / terminal / whiteboard workbench desktop** instead of any Telegram UI at all (for example a Chrome product site, terminal logs, or an Excalidraw/Docs surface). When that happens, do **not** summarize it as Telegram browsing or chat review; treat it as evidence of surrounding desktop attention and keep the app attribution low-confidence unless nearby frames visually confirm real Telegram UI.
  - A related trap now shows up for **Terminal** too: a Terminal-labeled frame can export as a **mixed terminal + browser/editor desktop** (for example terminal in front of Excalidraw / Google Docs / repo UI). In those cases, extract the concrete terminal content if legible, but explicitly record the composite desktop rather than pretending the whole frame is a pure terminal view.
  - A newer concrete variant: frames labeled as **Lark**, **Chrome / Excalidraw**, or similar planning/work-doc surfaces can export as a **Terminal + browser-chat desktop** instead (for example Terminal in front of a DeepSeek chat page). Treat that as a real mixed desktop correction, not as proof that the Lark/Excalidraw foreground survived the export.
  - Another newer concrete variant: frames labeled as **Alibaba / 1688 factory or product pages** can sometimes export as a generic **Chrome new-tab / address-bar** state. When that happens, keep confidence on the repeated OCR / URL cluster (e.g. many `1688` detail URLs across nearby frames), but downgrade any single exported page claim.
  - Another recurring late-window trap: a frame labeled as **Telegram** may export as a **desktop notification/banner over some other real foreground** (for example a Telegram notification floating above X / Preview / browser work) rather than an actual open Telegram chat. Treat those as **notification evidence + surrounding desktop attention**, not as proof that Telegram itself was the active work surface.
  - Likewise, a frame labeled as **Chrome/X/browser** can visually resolve as a **foreground Lark/WeChat/chat window with browser material only behind it**, or vice versa. If the image contains both a browser and a chat app, anchor your summary on the **readable foreground pane / active typing area / selection state**, not whichever app name the metadata or vision model fixates on.
  - A newer concrete late-window variant: even when a **refresh pull** shows a genuinely newer **Chrome/X-home OCR cluster** (for example Anthropic / Glasswing / OpenClaw-cost discourse), the representative exported screenshot can still collapse back into the **same older mixed desktop** you were already inspecting — e.g. **AWS terminal + WeChat/voice-call overlay**. In that case, preserve the newer X/browser material only as **OCR-level latest continuity**, keep the older visually confirmed mixed desktop as the highest-confidence foreground, and do **not** overstate the refresh as a clean visual app switch unless at least one newer export actually matches it.
  - If OCR or metadata implies one live page/state (for example X, ChatGPT, DeepSeek, Claude, or a research tab bundle) but the visual export shows an intermediate or alternate browser state like **Cloudflare / security verification**, a **YouTube watch page**, or another still-open tab, trust the visual export for that frame and treat the OCR/tab bundle as surrounding browser context rather than proof of the currently visible page.
  - A newer same-app browser variant now matters too: a frame can be labeled as a specific **Chrome/Claude** work tab (for example `Teleport website messaging and positioning - Claude`) while the exported screenshot visually resolves as a different **Chrome** tab such as a **YouTube watch page**. Treat that as a **same-app browser-state mismatch**, not proof that the OCR/window text was fake; keep the work-tab claim at **OCR-level nearby continuity** unless another export cleanly confirms it.
  - Another same-app Chrome variant now matters too: OCR can imply a harmless **Chrome profile / account-management** surface (for example `Manage Your Google Account`, multiple Chrome profiles, autofill/password menu text) while the exported screenshot visually resolves as an ordinary **browser content page** such as **Bilibili/home/feed/video**. Treat that as the same category of **same-app browser-state mismatch**: preserve the higher-level fact that Chrome remained active, but do **not** summarize it as a real account-settings / profile-management action unless another export cleanly confirms that menu.
  - A newer late-window Chrome trap: once a dense work session cools down, Fisherman can keep leaking **parked work-tab titles and OCR-heavy background docs** even after the real foreground has already shifted into **YouTube / leisure browsing**. In practice a frame labeled **Telegram** may export as plain **Chrome / YouTube home**, or a frame labeled **Chrome / Notion / Flashbots** may export as **YouTube Shorts** with only a background doc hinting at the earlier work. In those cases, anchor the pass on the **visually stable foreground leisure surface**, preserve the parked tabs / OCR-only work material as **background continuity**, and do **not** accidentally summarize the latest minutes as an active Telegram/Notion/work burst.
  - Do not over-prioritize only chat apps. In practice, **Google Docs / browser docs** and **Gmail / calendar-like logistics surfaces** can be among the highest-signal windows: a visually confirmed doc can reveal the user's actual live product copy / framing much more reliably than noisy OCR summaries, and a visually confirmed Gmail inbox can surface concrete event timing, travel, package, and ops coordination that matters for near-term context.
  - For those non-chat browser surfaces, prefer extracting the **visible structure** (headings, bullets, roadmap items, event times, locations, cancellations, delivery notices) rather than trying to summarize the entire page.
  - A recurring late-window trap in **WeChat**: adjacent frames can mix **work links / vendor references / product docs** with completely ordinary **location-sharing / meetup-delay / rain-traffic** logistics. If one nearby export shows Mira / supply-chain / vendor continuity but the latest clearer frame shows a straightforward logistics exchange with a location card, do **not** force the latest frame back into a work-thread narrative. Report it as **mixed work-context + ordinary logistics**, and lower confidence in any claim that the final foreground WeChat state was still the collaborator/product thread.
  - A newer concrete variant of that trap: OCR can strongly imply a **Ken Hsu / Wapitee / AI-glasses-supply-chain** WeChat surface while the exported screenshot actually shows a different open conversation such as **`Ai.` logistics + Shenzhen location card**. Treat that as a correction, not just noise: preserve the broader **cluster-level continuity** (vendor / AI-glasses context may still be real nearby), but downgrade confidence that the specific frame's foreground was the vendor thread.
  - Another newer concrete mismatch pattern: a frame labeled as **WeChat** and OCR'd like the latest `Ai.` chat can still export as a pure **Chrome / Google Docs `Branding Website`** surface with no WeChat visible. When this happens, explicitly separate **foreground desktop surface** from **app/OCR attribution**, and avoid claiming a stable latest WeChat state unless at least one nearby export truly shows WeChat UI.
  - A newer high-value trap: during a docs-heavy live burst, **multiple different app-labeled exports can all visually collapse to the same Google Docs / browser surface** (for example **Telegram → Branding Website**, **X → Branding Website**, **WeChat → Branding Website** within the same few minutes). Treat this as either a real dominant foreground that Fisherman keeps re-attributing in metadata, or a screenshot/export-path reliability issue — but in either case, **do not infer app switching just because OCR/app labels changed**. Anchor the pass on the repeated visually stable surface, and treat the chat/app-specific content as **OCR-level nearby continuity** unless at least one export truly shows the alternate app UI.
  - Another concrete inbox/browser inversion now matters too: a frame labeled as **Chrome / Gmail / inbox / account-notification** can visually export as a real **foreground WeChat** desktop instead. In those cases, keep the operational inbox items (for example GitHub-key notifications, calendar invites, billing/order emails, meeting summaries) only as **OCR-level nearby continuity** unless another export actually shows Gmail UI in the foreground.
  - A newer hardware-sourcing variant: late frames labeled **WeChat** can actually export as **Chrome / 1688 procurement or supplier-chat surfaces**. In practice this can reveal much more concrete evidence than the WeChat OCR suggested — e.g. a real **`智能戒指` procurement detail** page with quantity/spec requirements, or a supplier chat asking whether a **recording ring** exists / whether recording can be added to an existing ring board. Treat that as a genuine **foreground correction to Chrome/1688**, preserve the readable requirement/supplier-response details, and do not summarize the latest state as WeChat unless a nearby export truly shows WeChat UI.
  - A closely related same-app browser variant now matters for **Claude / sourcing** work too: OCR/window text can imply a real **Claude** research or prompt-engineering surface (for example keyword ideation around **veterinary ultrasound / `兽用B超`**) while the exported image resolves as an ordinary **Chrome sourcing page** such as **1688 factory search/results**. Treat that as **same-browser-state mismatch** rather than proof that the sourcing cluster is fake: preserve the broader sourcing / research theme, but downgrade the exact `Claude`-foreground claim unless another export cleanly confirms it.
  - A newer cross-app inversion now matters for **Telegram** and **Signal** too: a frame can carry dense, high-signal OCR about a live mechanism thread (for example **factory intro / rebate-slash / bonded recommender** logic in Telegram, or **Flashbots refund-rule** language in Signal) while the exported image visually resolves as an entirely different nearby foreground such as a **Lark meeting + browser** desktop or an unrelated **hardware chat**. When this happens, do **not** discard the OCR if it repeats coherently across nearby frames — preserve it as **OCR-level nearby continuity** — but do **not** upgrade it to visually confirmed foreground work unless at least one export actually shows the matching Telegram/Signal UI.
  - Do not over-correct in only one direction: if a repeated OCR/window claim later gets a **clean visual confirmation** (for example a **`Run what you scroll campaign`** Lark/Feishu sheet with creator-outreach columns like followers / average views / collaboration fee / internal feedback), upgrade that surface back to **visually confirmed foreground work**, not merely OCR-level continuity.
7. Before treating a pass as a fresh directional update, compare the **latest frame timestamp** to the current clock time. If the 2h query is non-empty but the latest captured frame is materially earlier than now, report it as a **continuity / clarification pass** ("no newer post-<ts> activity") rather than overstating it as new movement.
8. If you spend several minutes inspecting/exporting frames during an **active live burst**, do one final **refresh pull** before you finalize the report (for example `uv run python cli.py query -j --limit 10` and/or targeted per-app pulls). New frames can land while you are investigating, and those late arrivals can materially change the best read — for example, what first looked like mostly meeting/chat-list noise may end with a much clearer vendor deck, collaborator thread, or browser document.
   - A concrete recurring variant: a browser-heavy sourcing burst can keep extending after your first pass and **change platform/site family** in the latest minutes — e.g. what looked like an **1688**-only diligence loop can continue into **Taobao / Tmall / Rokid item pages** on refresh. Treat that as the **same evolving sourcing burst**, but update the final read so you do not freeze it at the earlier 1688-only state.
9. When a **late micro-burst** appears inside a broader active hour, run **fresh targeted per-app pulls for that narrower recency band** before carrying forward older app narratives. If the newest burst shows new Chrome/Telegram frames but fresh `--app "Lark"` or `--app "WeChat"` pulls for the same recent window come back empty, do **not** lazily extend earlier campaign-sheet / meetup-logistics / chat-thread context into the very latest minutes. Keep the newest burst scoped to the apps with repeated fresh evidence, and treat older same-hour app context as continuity unless re-confirmed.
   - In Chrome-heavy hardware/sourcing windows, also watch for a **category sharpening** on refresh: the newest frames may move from broad supplier/factory pages into much more concrete **SKU detail pages**, accessory pages, or brand-specific comparisons. Preserve that shift because it often marks the real end-state of the burst.
10. If `query -j` output fails JSON parsing because decrypted OCR contains bad escape sequences, **do not trust a full-window parse**. Fall back to:
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
- If CLI queries appear to show **no recent frames**, do not stop at the DB read. Check `/home/ubuntu/fisherman/server/ingest.log` near the end for the sequence of:
  - `client_connected`
  - long gaps with **no** `frame_stored`
  - `client_disconnected`
  - later reconnect + resumed `frame_stored`
  This pattern means the ingest server is alive but the upstream client/capture path was idle or stalled for a while.
- A particularly important variant in the current stack: the default capture backend is `screenpipe`, and the client only sees frames that Screenpipe surfaces through its OCR/search API. So apparent Fisherman silence can really mean **Screenpipe discovery silence**, not total user inactivity.
- If a reconnect suddenly stores one or two much older frames before current ones resume, treat that as a clue that the upstream Screenpipe polling cursor / dedupe state likely reset. Do not summarize that old frame as fresh activity.
- For macOS `Preview` / PDF-reading windows, distinguish **frame capture** from **usable reading-context extraction**. Fisherman may correctly capture `app=Preview` and a PDF filename while still producing poor OCR of the actual page contents because the current path is screenshot-OCR-based, resolution-limited, and not PDF-aware. In those cases, report high confidence on `reading a PDF in Preview` but low confidence on the detailed document content unless you inspect the screenshot directly.
