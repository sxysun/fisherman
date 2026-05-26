from __future__ import annotations

import re
from typing import Any


MENU_WORDS = {
    "file",
    "edit",
    "view",
    "window",
    "help",
    "shell",
    "history",
    "bookmarks",
    "profiles",
    "tab",
    "selection",
    "go",
    "run",
    "terminal",
}

APP_ALIASES = {
    "arc": "Arc",
    "calendar": "Calendar",
    "chatgpt": "ChatGPT",
    "chrome": "Chrome",
    "claude": "Claude",
    "cursor": "Cursor",
    "discord": "Discord",
    "element": "Element",
    "finder": "Finder",
    "google chrome": "Chrome",
    "linear": "Linear",
    "mail": "Mail",
    "notion": "Notion",
    "obsidian": "Obsidian",
    "preview": "Preview",
    "safari": "Safari",
    "slack": "Slack",
    "terminal": "Terminal",
    "visual studio code": "VS Code",
    "vs code": "VS Code",
    "wechat": "WeChat",
    "xcode": "Xcode",
    "zoom": "Zoom",
}


def analyze_event(event: Any) -> dict[str, Any]:
    screen = getattr(event, "screen", None)
    return analyze_screen(
        frontmost_app=getattr(screen, "frontmost_app", None),
        bundle_id=getattr(screen, "bundle_id", None),
        window_title=getattr(screen, "window_title", None),
        ocr_snippet=getattr(screen, "ocr_snippet", None),
    )


def analyze_candidate_dict(candidate: dict[str, Any]) -> dict[str, Any]:
    screen = candidate.get("screen") or candidate
    if not isinstance(screen, dict):
        screen = {}
    return analyze_screen(
        frontmost_app=screen.get("frontmost_app"),
        bundle_id=screen.get("bundle_id"),
        window_title=screen.get("window_title"),
        ocr_snippet=screen.get("ocr_snippet"),
    )


def effective_app(event: Any) -> str:
    return str(analyze_event(event).get("effective_app") or "unknown")


def effective_app_from_candidate_dict(candidate: dict[str, Any]) -> str:
    return str(analyze_candidate_dict(candidate).get("effective_app") or "unknown")


def analyze_screen(
    *,
    frontmost_app: Any = None,
    bundle_id: Any = None,
    window_title: Any = None,
    ocr_snippet: Any = None,
) -> dict[str, Any]:
    raw_app = _clean(frontmost_app)
    inferred = infer_visible_app_from_ocr(str(ocr_snippet or ""))
    inferred_app = inferred.get("app")
    source = "frontmost_app"
    confidence = 0.55 if raw_app else 0.0
    effective = raw_app or ""
    flags: list[str] = []

    if inferred_app:
        mismatch = bool(raw_app and _norm(raw_app) != _norm(inferred_app))
        if mismatch:
            flags.append("app_metadata_mismatch")
        if not raw_app or mismatch:
            effective = str(inferred_app)
            source = str(inferred.get("source") or "ocr")
            confidence = float(inferred.get("confidence") or 0.8)
        else:
            confidence = max(confidence, float(inferred.get("confidence") or 0.8))

    if not effective:
        effective = _bundle_hint(str(bundle_id or "")) or "unknown"
        source = "bundle_id" if effective != "unknown" else "unknown"
        confidence = 0.4 if source == "bundle_id" else 0.0

    return {
        "raw_frontmost_app": raw_app or None,
        "bundle_id": _clean(bundle_id) or None,
        "window_title_present": bool(_clean(window_title)),
        "inferred_visible_app": inferred_app,
        "effective_app": effective,
        "source": source,
        "confidence": round(confidence, 3),
        "flags": sorted(set(flags)),
        "evidence": inferred.get("evidence") if inferred else None,
    }


def infer_visible_app_from_ocr(ocr: str) -> dict[str, Any]:
    tokens = _tokens(ocr)[:32]
    if not tokens:
        return {}
    joined = " ".join(tokens[:16])
    candidates: list[tuple[int, str, str]] = []

    for alias, canonical in APP_ALIASES.items():
        alias_tokens = alias.split()
        width = len(alias_tokens)
        for i in range(0, max(0, min(len(tokens) - width + 1, 18))):
            if tokens[i:i + width] == alias_tokens:
                candidates.append((i, canonical, alias))
                break

    for idx, canonical, alias in sorted(candidates, key=lambda row: row[0]):
        window = tokens[idx + len(alias.split()):idx + len(alias.split()) + 9]
        menu_hits = [tok for tok in window if tok in MENU_WORDS]
        if len(set(menu_hits)) >= 2:
            return {
                "app": canonical,
                "source": "ocr_menu_bar",
                "confidence": 0.88,
                "evidence": " ".join(tokens[idx:idx + len(alias.split()) + min(6, len(window))]),
            }

    # Some OCR crops lose the app token but keep distinctive terminal menus.
    if tokens[:5] and {"shell", "edit", "view"}.issubset(set(tokens[:8])):
        return {
            "app": "Terminal",
            "source": "ocr_menu_bar",
            "confidence": 0.82,
            "evidence": joined[:120],
        }

    return {}


def _tokens(value: str) -> list[str]:
    return re.findall(r"[a-z][a-z0-9.+#-]*", value.lower())


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _norm(value: str) -> str:
    value = value.lower().strip()
    value = value.removeprefix("google ")
    return re.sub(r"\s+", " ", value)


def _bundle_hint(bundle_id: str) -> str | None:
    bundle = bundle_id.lower()
    if "chrome" in bundle:
        return "Chrome"
    if "terminal" in bundle:
        return "Terminal"
    if "cursor" in bundle:
        return "Cursor"
    if "safari" in bundle:
        return "Safari"
    if "wechat" in bundle:
        return "WeChat"
    return None
