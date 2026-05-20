from __future__ import annotations

import time
from typing import Optional

from . import privacy
from .fisherman_client import FishermanClient
from .schemas import CandidateEvent, ContextSignals, ScreenContext, SceneTag, UserPref


OCR_SNIPPET_MAX = 280


def _now_unix() -> float:
    return time.time()


async def synthesize(
    fc: FishermanClient,
    *,
    user_pref: UserPref,
    minutes_since_last_push: float,
) -> Optional[CandidateEvent]:
    """Compose a CandidateEvent from current Fisherman state. None if Fisherman is unreachable."""
    status = await fc.get_status()
    if status is None:
        return None

    frames = await fc.list_frames(count=2)
    if not frames:
        screen = ScreenContext(active=False)
    else:
        f = frames[0]
        ocr_full = f.get("ocr_text") or ""
        scan = privacy.scan_text(ocr_full)
        ocr = scan.redacted_text[:OCR_SNIPPET_MAX]
        try:
            ts = float(f.get("ts") or 0.0)
        except (TypeError, ValueError):
            ts = 0.0
        capture_gap_sec = 0.0
        if len(frames) > 1:
            try:
                prev_ts = float(frames[1].get("ts") or 0.0)
                if prev_ts > 0:
                    capture_gap_sec = max(0.0, ts - prev_ts)
            except (TypeError, ValueError):
                capture_gap_sec = 0.0
        screen = ScreenContext(
            active=True,
            frontmost_app=f.get("app"),
            bundle_id=f.get("bundle"),
            window_title=f.get("window"),
            ocr_snippet=ocr,
            capture_ts_unix=ts if ts > 0 else None,
            capture_gap_sec=capture_gap_sec,
            frame_age_sec=max(0.0, _now_unix() - ts) if ts > 0 else 0.0,
            sensitive_scene=scan.sensitive,
        )

    context = ContextSignals(
        in_call=bool(status.get("in_call")),
        on_battery=bool(status.get("on_battery")),
        minutes_since_last_push=minutes_since_last_push,
        minutes_since_last_user_action=0.0,
    )

    event = CandidateEvent(
        screen=screen,
        scene=SceneTag(label="unknown", strength="unknown", source="unknown"),
        context=context,
        user_pref=user_pref,
    )
    return event
