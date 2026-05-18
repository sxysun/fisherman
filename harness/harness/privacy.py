from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class PrivacyScan:
    sensitive: bool
    reasons: list[str] = field(default_factory=list)
    redacted_text: str = ""


_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "private_key_block",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
            re.IGNORECASE,
        ),
    ),
    (
        "assignment_secret",
        re.compile(
            r"\b[A-Za-z0-9_.-]*(?:api[_ -]?key|secret|token|password|passwd|authorization)"
            r"[A-Za-z0-9_.-]*\s*[:=]\s*['\"]?[A-Za-z0-9_\-./+=:]{8,}",
            re.IGNORECASE,
        ),
    ),
    (
        "bearer_token",
        re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}\b", re.IGNORECASE),
    ),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{20,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "credit_card_hint",
        re.compile(
            r"\b(?:credit card|cvv|cvc|card number|ssn|social security)\b",
            re.IGNORECASE,
        ),
    ),
]


def scan_text(text: str | None) -> PrivacyScan:
    raw = text or ""
    redacted = raw
    reasons: list[str] = []
    for name, pattern in _PATTERNS:
        if not pattern.search(redacted):
            continue
        reasons.append(name)
        redacted = pattern.sub(f"[REDACTED:{name}]", redacted)
    return PrivacyScan(
        sensitive=bool(reasons),
        reasons=list(dict.fromkeys(reasons)),
        redacted_text=redacted,
    )


def redact_text(text: str | None) -> str:
    return scan_text(text).redacted_text


def sensitive_reasons(text: str | None) -> list[str]:
    return scan_text(text).reasons
