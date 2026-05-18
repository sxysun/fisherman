"""Smoke test: every module imports, schemas serialize, rule_v0 decides on a fixture."""

from __future__ import annotations

import asyncio
import io
import json
import time

from harness import candidate as candidate_mod
from harness import critic as critic_mod
from harness import fisherman_client as fc_mod
from harness import gate as gate_mod
from harness import image_redaction as image_redaction_mod
from harness import memory as memory_mod
from harness import privacy as privacy_mod
from harness import push as push_mod
from harness import realizer as realizer_mod
from harness import scene as scene_mod
from harness import scene_vlm as scene_vlm_mod
from harness import schemas
from harness import server as server_mod
from harness import store as store_mod


def test_imports():
    # all top-level modules import cleanly
    assert schemas
    assert store_mod
    assert fc_mod
    assert candidate_mod
    assert scene_mod
    assert memory_mod
    assert gate_mod
    assert realizer_mod
    assert critic_mod
    assert push_mod
    assert privacy_mod
    assert image_redaction_mod
    assert server_mod


def test_schema_roundtrip():
    event = schemas.CandidateEvent()
    event.screen.frontmost_app = "Cursor"
    event.screen.ocr_snippet = "def foo(): TODO: write this"
    serialized = json.dumps(event.to_dict(), default=str)
    assert "Cursor" in serialized
    assert "candidate_id" in serialized


def test_scene_tag_codes_for_todo():
    event = schemas.CandidateEvent()
    event.screen.frontmost_app = "Cursor"
    event.screen.ocr_snippet = "def foo(): TODO write this"
    tag = scene_mod.tag(event, recent_apps=["Cursor"] * 5)
    assert tag.label == "coding_with_todo_in_view"
    assert tag.source == "rule"


def test_scene_tag_codes_for_context_switch():
    event = schemas.CandidateEvent()
    event.screen.frontmost_app = "Slack"
    tag = scene_mod.tag(
        event,
        recent_apps=["Cursor", "Slack", "Chrome", "Notion", "Discord", "iTerm2", "Figma"],
    )
    assert tag.label == "rapid_context_switching"


def test_privacy_redacts_secret_text_and_marks_scene_sensitive():
    text = 'OPENROUTER_API_KEY="or-v1-testsecret1234567890"'
    scan = privacy_mod.scan_text(text)
    assert scan.sensitive
    assert "or-v1-testsecret" not in scan.redacted_text
    assert "assignment_secret" in scan.reasons
    assert "[REDACTED:" in scan.redacted_text

    event = schemas.CandidateEvent()
    event.screen.ocr_snippet = text
    tag = scene_mod.tag(event, recent_apps=["Cursor"])
    assert tag.label == "sensitive"


def test_privacy_redacts_bearer_tokens():
    text = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456"
    scan = privacy_mod.scan_text(text)
    assert scan.sensitive
    assert "abcdefghijklmnopqrstuvwxyz" not in scan.redacted_text
    assert "bearer_token" in scan.reasons


def test_realizer_redacts_ocr_and_skips_sensitive_image():
    class ShouldNotFetchFisherman:
        async def list_frames(self, count=1):
            raise AssertionError("sensitive frames should not fetch screenshots")

    event = schemas.CandidateEvent()
    event.screen.ocr_snippet = "token = ghp_abcdefghijklmnopqrstuvwxyz123456"
    event.screen.sensitive_scene = True
    mem = schemas.MemorySnapshot.build([], [], [], 0, 0)
    state = realizer_mod._serialize_state(event, mem)
    assert "ghp_" not in state
    assert "[REDACTED:" in state

    image_b64, image_bytes, flags = asyncio.run(
        realizer_mod._fetch_latest_frame_b64(
            ShouldNotFetchFisherman(),
            event,
            redact_sensitive_screenshots=False,
        )
    )
    assert image_b64 is None
    assert image_bytes == 0
    assert flags


def test_image_redaction_masks_sensitive_ocr_box():
    from PIL import Image

    img = Image.new("RGB", (120, 60), "white")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    data = buf.getvalue()

    result = image_redaction_mod.redact_jpeg_bytes(
        data,
        ocr_runner=lambda _: [
            image_redaction_mod.OcrBox(
                text="OPENROUTER_API_KEY=or-v1-testsecret1234567890",
                bbox=(10, 10, 110, 30),
            )
        ],
    )
    assert result.redacted
    assert "assignment_secret" in result.reasons
    assert result.boxes

    with Image.open(io.BytesIO(result.image_bytes)) as out:
        red, green, blue = out.getpixel((20, 20))
    assert red < 40 and green < 40 and blue < 40


def test_realizer_redacts_sensitive_screenshot_when_boxes_match():
    from PIL import Image

    class OneFrameFisherman:
        async def list_frames(self, count=1):
            return [{"ts": 1.0, "has_image": True}]

        async def get_frame_image(self, ts_ms):
            img = Image.new("RGB", (120, 60), "white")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=95)
            return buf.getvalue()

    old_runner = image_redaction_mod.ocr_boxes_from_vision
    image_redaction_mod.ocr_boxes_from_vision = lambda _: [
        image_redaction_mod.OcrBox(
            text="token = ghp_abcdefghijklmnopqrstuvwxyz123456",
            bbox=(10, 10, 110, 30),
        )
    ]
    try:
        event = schemas.CandidateEvent()
        event.screen.ocr_snippet = "token = [REDACTED:github_token]"
        event.screen.sensitive_scene = True
        image_b64, image_bytes, flags = asyncio.run(
            realizer_mod._fetch_latest_frame_b64(OneFrameFisherman(), event)
        )
    finally:
        image_redaction_mod.ocr_boxes_from_vision = old_runner

    assert image_b64 is not None
    assert image_bytes > 0
    assert "sensitive_scene" in flags
    assert "image_redacted:1" in flags


def test_realizer_frame_fetch_returns_privacy_tuple_when_no_frame():
    class EmptyFisherman:
        async def list_frames(self, count=1):
            return []

    event = schemas.CandidateEvent()
    image_b64, image_bytes, flags = asyncio.run(
        realizer_mod._fetch_latest_frame_b64(EmptyFisherman(), event)
    )
    assert image_b64 is None
    assert image_bytes == 0
    assert flags == []


def test_scene_vlm_skips_sensitive_ocr_before_network():
    event = schemas.CandidateEvent()
    event.screen.frame_age_sec = 1
    event.screen.ocr_snippet = "api_key = sk-abcdefghijklmnopqrstuvwxyz"
    assert scene_vlm_mod._should_skip(event, min_interval_sec=0) == "sensitive_ocr"


def test_rule_v0_no_ping_when_in_call():
    from policies import rule_v0

    event = schemas.CandidateEvent()
    event.context.in_call = True
    event.scene = schemas.SceneTag(label="coding_focused", strength="medium", source="rule")
    mem = schemas.MemorySnapshot.build([], [], [], 0, 0.0)
    cfg = {
        "cooldown_min": 5,
        "quiet_hours_start": 3,  # narrow window so we're outside
        "quiet_hours_end": 4,
        "allowed_intents": ["focus_nudge"],
    }
    d = rule_v0.decide(event, mem, [], cfg)
    assert d.action == "no_ping"
    assert "in_call" in d.reason_codes


def test_rule_v0_pings_on_high_switch():
    from policies import rule_v0

    event = schemas.CandidateEvent()
    event.screen.frame_age_sec = 5.0
    event.scene = schemas.SceneTag(
        label="rapid_context_switching", strength="strong", source="rule"
    )
    mem = schemas.MemorySnapshot.build(
        recent_apps=["A", "B", "C", "D", "E"],
        recent_scenes=[],
        recent_outcomes=[],
        app_switches_last_15m=7,
        minutes_on_current_app=1.0,
    )
    cfg = {
        "cooldown_min": 5,
        "quiet_hours_start": 3,
        "quiet_hours_end": 4,
        "sensitivity": "balanced",
        "allowed_intents": [],
    }
    d = rule_v0.decide(event, mem, [], cfg)
    assert d.action == "notch_ping"
    # Goal-aware: intent is "goal_aware", reasons carry the why_now content
    assert d.intent == "goal_aware"
    assert "rapid_context_switching" in d.reason_codes


def test_rule_v0_negative_feedback_backoff_is_time_bounded():
    from policies import rule_v0

    event = schemas.CandidateEvent()
    event.screen.frame_age_sec = 5.0
    event.screen.ocr_snippet = "ship harness"
    event.scene = schemas.SceneTag(label="reading_browser", strength="medium", source="rule")
    mem = schemas.MemorySnapshot.build(
        recent_apps=["Chrome"],
        recent_scenes=["reading_browser"],
        recent_outcomes=[],
        app_switches_last_15m=0,
        minutes_on_current_app=95.0,
    )
    cfg = {
        "cooldown_min": 5,
        "negative_feedback_backoff_min": 15,
        "quiet_hours_start": 3,
        "quiet_hours_end": 4,
        "sensitivity": "responsive",
        "daily_goal": "ship harness",
        "allowed_intents": [],
    }

    stale = [{"user_action": "dismissed", "ts": "2000-01-01T00:00:00Z"}]
    recent = [{
        "user_action": "dismissed",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }]

    assert rule_v0.decide(event, mem, stale, cfg).action == "notch_ping"
    blocked = rule_v0.decide(event, mem, recent, cfg)
    assert blocked.action == "no_ping"
    assert "recent_negative_feedback" in blocked.reason_codes


def test_rule_v0_snooze_expires():
    from policies import rule_v0

    event = schemas.CandidateEvent()
    event.ts = "2026-05-18T12:00:00Z"
    event.screen.frame_age_sec = 5.0
    event.screen.ocr_snippet = "ship harness"
    event.scene = schemas.SceneTag(label="reading_browser", strength="medium", source="rule")
    mem = schemas.MemorySnapshot.build(
        recent_apps=["Chrome"],
        recent_scenes=["reading_browser"],
        recent_outcomes=[],
        app_switches_last_15m=0,
        minutes_on_current_app=95.0,
    )
    cfg = {
        "cooldown_min": 5,
        "negative_feedback_backoff_min": 15,
        "quiet_hours_start": 3,
        "quiet_hours_end": 4,
        "sensitivity": "responsive",
        "daily_goal": "ship harness",
        "allowed_intents": [],
    }

    event.user_pref.snoozed_until = "2026-05-18T11:59:00Z"
    assert rule_v0.decide(event, mem, [], cfg).action == "notch_ping"

    event.user_pref.snoozed_until = "2026-05-18T12:01:00Z"
    blocked = rule_v0.decide(event, mem, [], cfg)
    assert blocked.action == "no_ping"
    assert "snoozed" in blocked.reason_codes


def test_vlm_overlay_preserves_detail_signals():
    event = schemas.CandidateEvent()
    scene_vlm_mod.overlay_on_event(event, {
        "primary_activity": "reading",
        "specificity": "reading a harness plan",
        "sensitive": False,
        "intent_signals": {"could_offer_research": True, "has_open_thread": False},
        "load_bearing_text": "launch plan",
    })
    assert event.scene.source == "llm"
    assert event.scene.specificity == "reading a harness plan"
    assert event.scene.intent_signals["could_offer_research"] is True


def test_store_attaches_outcome_to_trace(tmp_path):
    old_dir = store_mod.HARNESS_DIR
    store_mod.HARNESS_DIR = tmp_path
    try:
        trace = {
            "trace_id": "tr_test",
            "action": {"decision_id": "pd_test"},
            "outcome": None,
            "reward": None,
        }
        store_mod.append_jsonl("traces.jsonl", trace)
        outcome = {"decision_id": "pd_test", "user_action": "clicked"}
        reward = {"value": 2.0, "version": "v2"}
        assert store_mod.attach_outcome_to_trace("pd_test", outcome, reward)
        rows = store_mod.tail_jsonl("traces.jsonl")
        assert rows[0]["outcome"] == outcome
        assert rows[0]["reward"] == reward
    finally:
        store_mod.HARNESS_DIR = old_dir


def test_later_outcome_sets_snooze(tmp_path):
    old_dir = store_mod.HARNESS_DIR
    store_mod.HARNESS_DIR = tmp_path
    try:
        row = {"decision_id": "pd_test", "user_action": "snoozed"}
        server_mod._apply_snooze_from_outcome(row, duration="30m")
        state = store_mod.read_policy_state()
        assert state["snoozed_until"]
        assert row["snoozed_until"] == state["snoozed_until"]
    finally:
        store_mod.HARNESS_DIR = old_dir


def test_reward_signal_based():
    from harness import reward
    # ignored
    r = reward.compute_reward({"user_action": "timed_out"})
    assert r["value"] == -1.0
    # considered
    r = reward.compute_reward({
        "user_action": "timed_out",
        "interaction_summary": {"intent_signal": "considered"},
    })
    assert r["value"] == 0.5
    # clicked
    r = reward.compute_reward({"user_action": "clicked"})
    assert r["value"] == 2.0
    # dismissed
    r = reward.compute_reward({"user_action": "dismissed"})
    assert r["value"] == -1.5


def test_critic_regex_blocks_secret():
    event = schemas.CandidateEvent()
    result = critic_mod.regex_check("Your api_key is hardcoded — fix it?")
    assert not result.pass_
    assert "privacy_leak" in result.flags


def test_critic_regex_passes_clean():
    result = critic_mod.regex_check("5 app switches in 8 min. Mute Slack for 25?")
    assert result.pass_
