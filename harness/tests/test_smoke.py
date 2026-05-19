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
from harness import label_ui as label_ui_mod
from harness import memory as memory_mod
from harness import metrics as metrics_mod
from harness import model_audit as model_audit_mod
from harness import privacy as privacy_mod
from harness import push as push_mod
from harness import realizer as realizer_mod
from harness import scene as scene_mod
from harness import scene_vlm as scene_vlm_mod
from harness import schemas
from harness import server as server_mod
from harness import sql_store as sql_store_mod
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
    assert label_ui_mod
    assert model_audit_mod
    assert metrics_mod
    assert server_mod
    assert sql_store_mod


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


def test_rule_v0_soft_reject_hover_backoff_is_live_signal():
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
    soft_reject = [{
        "user_action": "timed_out",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "interaction_summary": {"intent_signal": "rejection_considered"},
    }]

    blocked = rule_v0.decide(event, mem, soft_reject, cfg)
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


def test_sql_store_mirrors_jsonl_rows_and_trace_updates(tmp_path):
    old_dir = store_mod.HARNESS_DIR
    store_mod.HARNESS_DIR = tmp_path
    try:
        candidate = {
            "candidate_id": "cand_sql",
            "ts": "2026-05-19T12:00:00Z",
            "screen": {"frontmost_app": "Chrome", "sensitive_scene": False},
            "scene": {"label": "reading_browser", "source": "rule"},
        }
        decision = {
            "decision_id": "pd_sql",
            "candidate_id": "cand_sql",
            "ts": "2026-05-19T12:00:01Z",
            "policy_version": "rule_v0",
            "action": "notch_ping",
            "intent": "goal_aware",
            "reason_codes": ["reading_browser"],
            "confidence": 0.7,
        }
        trace = {
            "trace_id": "tr_sql",
            "ts": "2026-05-19T12:00:02Z",
            "state": {"candidate": candidate},
            "action": decision,
            "outcome": None,
            "reward": None,
        }
        outcome = {
            "decision_id": "pd_sql",
            "user_action": "dismissed",
            "latency_from_display_ms": 1200,
            "interaction_summary": {"intent_signal": "rejection_considered"},
            "ts": "2026-05-19T12:00:03Z",
            "reward": {"version": "v2", "value": -0.8},
        }
        model_call = {
            "model_call_id": "mc_sql",
            "ts": "2026-05-19T12:00:04Z",
            "purpose": "realizer",
            "model": "demo",
            "status": "ok",
            "latency_ms": 123,
            "tokens_in": 10,
            "tokens_out": 3,
            "vision_used": True,
            "image_bytes": 42,
        }
        label = {
            "candidate_id": "cand_sql",
            "decision_id": "pd_sql",
            "label": "bad",
            "confidence": 0.9,
            "ts": "2026-05-19T12:00:05Z",
        }

        store_mod.append_jsonl("candidates.jsonl", candidate)
        store_mod.append_jsonl("decisions.jsonl", decision)
        store_mod.append_jsonl("traces.jsonl", trace)
        store_mod.append_jsonl("outcomes.jsonl", outcome)
        store_mod.append_jsonl("model_calls.jsonl", model_call)
        store_mod.append_jsonl("retro_labels.jsonl", label)

        assert sql_store_mod.db_path().exists()
        assert sql_store_mod.count_rows("event_log") == 6
        assert sql_store_mod.count_rows("candidates") == 1
        assert sql_store_mod.count_rows("decisions") == 1
        assert sql_store_mod.count_rows("traces") == 1
        assert sql_store_mod.count_rows("outcomes") == 1
        assert sql_store_mod.count_rows("model_calls") == 1
        assert sql_store_mod.count_rows("retro_labels") == 1

        reward = {"version": "v2", "value": -0.8}
        assert store_mod.attach_outcome_to_trace("pd_sql", outcome, reward)
        trace_rows = sql_store_mod.recent_rows("traces", limit=1)
        assert trace_rows[0]["outcome_action"] == "dismissed"
        assert trace_rows[0]["reward_value"] == -0.8
        assert '"user_action":"dismissed"' in trace_rows[0]["payload_json"]
    finally:
        store_mod.HARNESS_DIR = old_dir


def test_label_queue_uses_frozen_cursor_and_session_skip(tmp_path):
    from aiohttp.test_utils import make_mocked_request

    old_dir = store_mod.HARNESS_DIR
    store_mod.HARNESS_DIR = tmp_path
    try:
        decisions = [
            {
                "decision_id": "pd_old",
                "candidate_id": "cand_old",
                "ts": "2026-05-19T12:00:00Z",
                "action": "no_ping",
            },
            {
                "decision_id": "pd_mid",
                "candidate_id": "cand_mid",
                "ts": "2026-05-19T12:05:00Z",
                "action": "notch_ping",
            },
            {
                "decision_id": "pd_new",
                "candidate_id": "cand_new",
                "ts": "2026-05-19T12:10:00Z",
                "action": "no_ping",
            },
            {
                "decision_id": "pd_live",
                "candidate_id": "cand_live",
                "ts": "2026-05-19T12:15:00Z",
                "action": "no_ping",
            },
        ]
        for row in decisions:
            store_mod.append_jsonl("decisions.jsonl", row)
        store_mod.append_jsonl(
            "retro_labels.jsonl",
            {
                "candidate_id": "cand_old",
                "decision_id": "pd_old",
                "label": "good_no_ping",
                "ts": "2026-05-19T12:20:00Z",
            },
        )

        req = make_mocked_request(
            "GET",
            "/label/queue?before_ts=2026-05-19T12:10:00Z&order=newest&action=all",
        )
        resp = asyncio.run(label_ui_mod.get_label_queue(req))
        body = json.loads(resp.text)
        assert body["decision_id"] == "pd_new"
        assert body["progress"]["remaining"] == 2

        req = make_mocked_request(
            "GET",
            "/label/queue?before_ts=2026-05-19T12:10:00Z&order=newest&action=all&exclude=pd_new",
        )
        resp = asyncio.run(label_ui_mod.get_label_queue(req))
        body = json.loads(resp.text)
        assert body["decision_id"] == "pd_mid"

        req = make_mocked_request(
            "GET",
            "/label/queue?before_ts=2026-05-19T12:10:00Z&order=newest&action=no_ping&exclude=pd_new",
        )
        resp = asyncio.run(label_ui_mod.get_label_queue(req))
        assert resp.text == "null"
    finally:
        store_mod.HARNESS_DIR = old_dir


def test_metrics_computes_label_quality_and_readiness(tmp_path):
    old_dir = store_mod.HARNESS_DIR
    store_mod.HARNESS_DIR = tmp_path
    try:
        store_mod.append_jsonl(
            "decisions.jsonl",
            {
                "decision_id": "pd_ping_good",
                "candidate_id": "cand_ping_good",
                "ts": "2026-05-19T12:00:00Z",
                "action": "notch_ping",
            },
        )
        store_mod.append_jsonl(
            "decisions.jsonl",
            {
                "decision_id": "pd_ping_bad",
                "candidate_id": "cand_ping_bad",
                "ts": "2026-05-19T12:01:00Z",
                "action": "notch_ping",
            },
        )
        store_mod.append_jsonl(
            "decisions.jsonl",
            {
                "decision_id": "pd_silence_bad",
                "candidate_id": "cand_silence_bad",
                "ts": "2026-05-19T12:02:00Z",
                "action": "no_ping",
            },
        )
        store_mod.append_jsonl(
            "decisions.jsonl",
            {
                "decision_id": "pd_silence_good",
                "candidate_id": "cand_silence_good",
                "ts": "2026-05-19T12:02:30Z",
                "action": "no_ping",
            },
        )
        store_mod.append_jsonl(
            "outcomes.jsonl",
            {
                "decision_id": "pd_ping_good",
                "user_action": "clicked",
                "ts": "2026-05-19T12:03:00Z",
            },
        )
        for row in [
            ("pd_ping_good", "cand_ping_good", "would_help"),
            ("pd_ping_bad", "cand_ping_bad", "would_annoy"),
            ("pd_silence_bad", "cand_silence_bad", "would_help"),
            ("pd_silence_good", "cand_silence_good", "would_annoy"),
        ]:
            store_mod.append_jsonl(
                "retro_labels.jsonl",
                {
                    "decision_id": row[0],
                    "candidate_id": row[1],
                    "label": row[2],
                    "confidence": 1.0,
                    "ts": "2026-05-19T12:04:00Z",
                },
            )

        report = metrics_mod.compute(window="365d")
        assert report["n_decisions"] == 4
        assert report["n_pings"] == 2
        assert report["outcomes"]["capture_rate_for_pings"] == 0.5
        assert report["labels"]["agreement_rate"] == 0.5
        assert report["labels"]["false_interruption_rate_labeled"] == 0.5
        assert report["labels"]["missed_help_rate_labeled"] == 0.5
        assert report["data_readiness"]["needs_labels_for_personalization"] == 16
    finally:
        store_mod.HARNESS_DIR = old_dir


def test_model_audit_sanitizes_url_and_writes_recent_rows(tmp_path):
    from harness import dashboard_ui as dashboard_mod

    old_dir = store_mod.HARNESS_DIR
    store_mod.HARNESS_DIR = tmp_path
    try:
        row = model_audit_mod.record_model_call(
            purpose="realizer",
            base_url="https://user:secret@example.com/v1?api_key=bad",
            endpoint="https://user:secret@example.com/v1/chat/completions?token=bad",
            model="demo",
            status="ok",
            candidate_id="cand_test",
            prompt_version="goal_aware_v1",
            latency_ms=123,
            tokens_in=10,
            tokens_out=3,
            vision_used=True,
            image_bytes=42,
            privacy_flags=["image_redacted:1"],
            extra={"prompt_hash": model_audit_mod.text_hash("prompt")},
        )
        assert row["base_url"] == "https://example.com/v1"
        assert row["endpoint"] == "https://example.com/v1/chat/completions"
        assert "secret" not in json.dumps(row)
        assert store_mod.tail_jsonl("model_calls.jsonl", n=1)[0]["model_call_id"].startswith("mc_")
        data = dashboard_mod._aggregate()
        assert data["recent_model_calls"][0]["purpose"] == "realizer"
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
        "interaction_summary": {"intent_signal": "positive_considered"},
    })
    assert r["value"] == 0.5
    # hovered dismiss, then timed out: soft rejection, not positive consideration
    r = reward.compute_reward({
        "user_action": "timed_out",
        "interaction_summary": {"intent_signal": "rejection_considered"},
    })
    assert r["value"] == -0.8
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


def test_interaction_summary_is_target_aware():
    summary = server_mod._summarize_interactions([
        {"t_ms": 50, "kind": "approach"},
        {"t_ms": 100, "kind": "hover_start", "target": "dismiss"},
        {"t_ms": 420, "kind": "hover_end", "target": "dismiss"},
    ])
    assert summary["intent_signal"] == "rejection_considered"
    assert summary["considered_targets"] == ["dismiss"]
    assert summary["total_hover_ms_by_target"]["dismiss"] == 320
