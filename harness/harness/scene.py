from __future__ import annotations

import re

from . import privacy
from .schemas import CandidateEvent, SceneTag


CODE_APPS = {"Cursor", "Code", "Visual Studio Code", "Xcode", "Zed", "PyCharm", "IntelliJ IDEA"}
TERMINAL_APPS = {"Ghostty", "iTerm2", "Terminal", "Alacritty", "kitty", "WezTerm"}
COMMS_APPS = {"Slack", "Discord", "Messages", "WhatsApp", "Telegram", "WeChat", "Signal"}
BROWSER_APPS = {"Safari", "Google Chrome", "Arc", "Firefox", "Brave Browser", "Chromium"}
DOC_APPS = {"Notion", "Obsidian", "Bear", "Notes", "Pages", "Microsoft Word"}
DESIGN_APPS = {"Figma", "Sketch", "Linear"}
MEDIA_APPS = {"Spotify", "Music", "QuickTime Player", "VLC", "IINA"}

SENSITIVE_KEYWORDS = [
    "password",
    "passwd",
    "secret",
    "api_key",
    "apikey",
    "private key",
    "credit card",
    "ssn",
    "social security",
    "bank account",
]

HESITATION_PATTERNS = [
    r"\b(um|uh|hmm|wait|hold on|let me think)\b",
    r"\?\?\?+",
]

TODO_PATTERNS = [r"\bTODO\b", r"\bFIXME\b", r"\bXXX\b", r"\bHACK\b"]


def _has_keyword(text: str, words: list[str]) -> bool:
    t = text.lower()
    return any(w in t for w in words)


def _has_pattern(text: str, patterns: list[str]) -> bool:
    return any(re.search(p, text, flags=re.IGNORECASE) for p in patterns)


def tag(event: CandidateEvent, recent_apps: list[str]) -> SceneTag:
    """Rule-based scene tagger. Returns SceneTag with source='rule' if confident, else 'unknown'."""
    app = event.screen.frontmost_app or ""
    ocr = event.screen.ocr_snippet or ""

    if (
        event.screen.sensitive_scene
        or _has_keyword(ocr, SENSITIVE_KEYWORDS)
        or privacy.scan_text(ocr).sensitive
    ):
        return SceneTag(label="sensitive", strength="strong", source="rule", confidence=0.95)

    distinct_recent = len(set(recent_apps[-10:])) if recent_apps else 0
    if distinct_recent >= 5:
        return SceneTag(label="rapid_context_switching", strength="strong", source="rule", confidence=0.85)

    if app in CODE_APPS:
        if _has_pattern(ocr, TODO_PATTERNS):
            return SceneTag(label="coding_with_todo_in_view", strength="strong", source="rule", confidence=0.8)
        return SceneTag(label="coding_focused", strength="medium", source="rule", confidence=0.7)

    if app in TERMINAL_APPS:
        return SceneTag(label="terminal_work", strength="medium", source="rule", confidence=0.7)

    if app in COMMS_APPS:
        if _has_pattern(ocr, HESITATION_PATTERNS):
            return SceneTag(label="chat_hesitation", strength="strong", source="rule", confidence=0.8)
        return SceneTag(label="chat_active", strength="medium", source="rule", confidence=0.65)

    if app in BROWSER_APPS:
        return SceneTag(label="reading_browser", strength="medium", source="rule", confidence=0.6)

    if app in DOC_APPS:
        return SceneTag(label="writing_doc", strength="medium", source="rule", confidence=0.65)

    if app in DESIGN_APPS:
        return SceneTag(label="design_work", strength="medium", source="rule", confidence=0.65)

    if app in MEDIA_APPS:
        return SceneTag(label="media_consumption", strength="weak", source="rule", confidence=0.7)

    return SceneTag(label="unknown", strength="unknown", source="unknown", confidence=0.0)


async def tag_with_llm_fallback(
    event: CandidateEvent,
    recent_apps: list[str],
    *,
    llm_fallback_enabled: bool = False,
    llm_fallback_fn=None,
) -> SceneTag:
    """Try rules first; only call LLM if rules return unknown AND fallback is enabled."""
    rule_tag = tag(event, recent_apps)
    if rule_tag.label != "unknown":
        return rule_tag
    if llm_fallback_enabled and llm_fallback_fn is not None:
        try:
            return await llm_fallback_fn(event)
        except Exception:
            pass
    return rule_tag
