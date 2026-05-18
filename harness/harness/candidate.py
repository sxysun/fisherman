from __future__ import annotations

import time
from typing import Optional

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

    frames = await fc.list_frames(count=1)
    if not frames:
        screen = ScreenContext(active=False)
    else:
        f = frames[0]
        ocr = (f.get("ocr_text") or "")[:OCR_SNIPPET_MAX]
        ts = f.get("ts") or 0.0
        screen = ScreenContext(
            active=True,
            frontmost_app=f.get("app"),
            bundle_id=f.get("bundle"),
            window_title=f.get("window"),
            ocr_snippet=ocr,
            frame_age_sec=max(0.0, _now_unix() - float(ts)),
            sensitive_scene=False,
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
