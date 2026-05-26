"""Smoke test: every module imports, schemas serialize, rule_v0 decides on a fixture."""

from __future__ import annotations

import asyncio
import calendar
import io
import json
import time
from pathlib import Path

from harness import candidate as candidate_mod
from harness import config as config_mod
from harness import context_packets as context_packets_mod
from harness import critic as critic_mod
from harness import curation as curation_mod
from harness import dataset as dataset_mod
from harness import daemon as daemon_mod
from harness import eval_report as eval_report_mod
from harness import fisherman_client as fc_mod
from harness import experiments as experiments_mod
from harness import gate as gate_mod
from harness import frozen_eval as frozen_eval_mod
from harness import image_redaction as image_redaction_mod
from harness import implicit as implicit_mod
from harness import information_diet as information_diet_mod
from harness import kg_priors as kg_priors_mod
from harness import label_ui as label_ui_mod
from harness import memory as memory_mod
from harness import metrics as metrics_mod
from harness import model_audit as model_audit_mod
from harness import privacy as privacy_mod
from harness import push as push_mod
from harness import realizer as realizer_mod
from harness import scene as scene_mod
from harness import scene_vlm as scene_vlm_mod
from harness import service as service_mod
from harness import schemas
from harness import server as server_mod
from harness import shadow_eval as shadow_eval_mod
from harness import sql_store as sql_store_mod
from harness import store as store_mod
from harness import trust as trust_mod
from harness import trainer as trainer_mod
from harness import workflow_events as workflow_events_mod


def test_imports():
    # all top-level modules import cleanly
    assert schemas
    assert config_mod
    assert context_packets_mod
    assert eval_report_mod
    assert store_mod
    assert fc_mod
    assert candidate_mod
    assert curation_mod
    assert dataset_mod
    assert daemon_mod
    assert scene_mod
    assert memory_mod
    assert gate_mod
    assert frozen_eval_mod
    assert realizer_mod
    assert critic_mod
    assert push_mod
    assert privacy_mod
    assert image_redaction_mod
    assert implicit_mod
    assert information_diet_mod
    assert label_ui_mod
    assert model_audit_mod
    assert metrics_mod
    assert server_mod
    assert shadow_eval_mod
    assert sql_store_mod
    assert experiments_mod
    assert service_mod
    assert trust_mod
    assert trainer_mod
    assert workflow_events_mod
    assert kg_priors_mod


def test_schema_roundtrip():
    event = schemas.CandidateEvent()
    event.screen.frontmost_app = "Cursor"
    event.screen.ocr_snippet = "def foo(): TODO: write this"
    serialized = json.dumps(event.to_dict(), default=str)
    assert "Cursor" in serialized
    assert "candidate_id" in serialized


def test_workflow_events_group_and_close_on_window_change():
    builder = workflow_events_mod.WorkflowEventBuilder(max_gap_sec=90)

    first = schemas.CandidateEvent(candidate_id="cand_wev_1")
    first.ts = "2026-05-19T12:00:00Z"
    first.screen.frontmost_app = "Google Chrome"
    first.screen.window_title = "FreeTodo paper"
    first.screen.ocr_snippet = "ProAgentBench evaluates proactive assistance"
    first.screen.frame_age_sec = 1
    first.scene = schemas.SceneTag(label="reading_browser", strength="medium")
    assert builder.observe(first) is None
    assert first.workflow_event_id

    second = schemas.CandidateEvent(candidate_id="cand_wev_2")
    second.ts = "2026-05-19T12:00:10Z"
    second.screen.frontmost_app = "Google Chrome"
    second.screen.window_title = "FreeTodo paper"
    second.screen.ocr_snippet = "When to Assist and How to Assist"
    second.screen.frame_age_sec = 1
    second.scene = schemas.SceneTag(label="reading_browser", strength="medium")
    assert builder.observe(second) is None
    assert second.workflow_event_id == first.workflow_event_id

    third = schemas.CandidateEvent(candidate_id="cand_wev_3")
    third.ts = "2026-05-19T12:00:20Z"
    third.screen.frontmost_app = "Google Chrome"
    third.screen.window_title = "Harness dashboard"
    third.screen.frame_age_sec = 1
    third.scene = schemas.SceneTag(label="reading_browser", strength="medium")
    closed = builder.observe(third)

    assert closed is not None
    assert third.workflow_event_id != closed.workflow_event_id
    assert closed.status == "closed"
    assert closed.close_reason == "window_changed"
    assert closed.n_candidates == 2
    assert closed.duration_sec == 10.0
    assert closed.candidate_ids == ["cand_wev_1", "cand_wev_2"]
    context = builder.recent_context(now_ts=calendar.timegm(time.strptime(third.ts, "%Y-%m-%dT%H:%M:%SZ")))
    assert [row["status"] for row in context] == ["closed", "open"]
    assert context[-1]["window_title"] == "Harness dashboard"


def test_workflow_events_close_on_capture_gap_and_memory_snapshot(tmp_path):
    old_dir = store_mod.HARNESS_DIR
    store_mod.HARNESS_DIR = tmp_path
    try:
        builder = workflow_events_mod.WorkflowEventBuilder(max_gap_sec=90)
        mem = memory_mod.SessionMemory(window_min=120, idle_boundary_sec=90)
        first = schemas.CandidateEvent()
        first.ts = "2026-05-19T12:00:00Z"
        first.screen.frontmost_app = "Cursor"
        first.screen.window_title = "policy.py"
        first.screen.frame_age_sec = 1
        first.scene = schemas.SceneTag(label="coding_focused")
        mem.record(first)
        assert builder.observe(first) is None

        resumed = schemas.CandidateEvent()
        resumed.ts = "2026-05-19T12:30:00Z"
        resumed.screen.frontmost_app = "Cursor"
        resumed.screen.window_title = "policy.py"
        resumed.screen.capture_gap_sec = 1800
        resumed.screen.frame_age_sec = 1
        resumed.scene = schemas.SceneTag(label="coding_focused")
        mem.record(resumed)
        closed = builder.observe(resumed)
        assert closed is not None
        assert closed.close_reason == "capture_gap"

        snap = mem.snapshot(
            [],
            recent_workflow_events=builder.recent_context(
                now_ts=calendar.timegm(time.strptime(resumed.ts, "%Y-%m-%dT%H:%M:%SZ"))
            ),
        )
        assert snap.session_boundary == "capture_gap"
        assert snap.recent_workflow_events[-1]["status"] == "open"
        assert snap.recent_workflow_events[-1]["app"] == "Cursor"
    finally:
        store_mod.HARNESS_DIR = old_dir


def test_replay_preserves_workflow_event_id():
    from eval import replay as replay_mod

    event = replay_mod._to_event({
        "candidate_id": "cand_replay_wev",
        "ts": "2026-05-19T12:00:00Z",
        "workflow_event_id": "wev_replay",
        "screen": {"frontmost_app": "Chrome", "frame_age_sec": 1},
        "scene": {"label": "reading_browser", "source": "rule"},
    })
    assert event.workflow_event_id == "wev_replay"


def test_event_examples_mine_workflow_level_review_rows(tmp_path):
    old_dir = store_mod.HARNESS_DIR
    store_mod.HARNESS_DIR = tmp_path
    try:
        store_mod.append_jsonl("workflow_events.jsonl", {
            "workflow_event_id": "wev_negative",
            "ts": "2026-05-19T12:01:00Z",
            "start_ts": "2026-05-19T12:00:00Z",
            "last_ts": "2026-05-19T12:01:00Z",
            "status": "closed",
            "app": "Chrome",
            "window_title": "Harness notes",
            "scene_label": "reading_browser",
            "duration_sec": 60,
            "n_candidates": 2,
            "ocr_preview": "reading harness notes",
        })
        store_mod.append_jsonl("candidates.jsonl", {
            "candidate_id": "cand_negative",
            "ts": "2026-05-19T12:00:30Z",
            "workflow_event_id": "wev_negative",
            "screen": {"frontmost_app": "Chrome", "window_title": "Harness notes", "ocr_snippet": "reading harness notes"},
            "scene": {"label": "reading_browser"},
        })
        store_mod.append_jsonl("decisions.jsonl", {
            "decision_id": "pd_negative",
            "candidate_id": "cand_negative",
            "workflow_event_id": "wev_negative",
            "ts": "2026-05-19T12:00:35Z",
            "action": "notch_ping",
        })
        store_mod.append_jsonl("outcomes.jsonl", {
            "decision_id": "pd_negative",
            "user_action": "dismissed",
            "ts": "2026-05-19T12:00:40Z",
            "interaction_summary": {"intent_signal": "rejection_considered"},
        })

        store_mod.append_jsonl("workflow_events.jsonl", {
            "workflow_event_id": "wev_missed",
            "ts": "2026-05-19T12:10:00Z",
            "start_ts": "2026-05-19T12:08:00Z",
            "last_ts": "2026-05-19T12:10:00Z",
            "status": "closed",
            "app": "Chrome",
            "window_title": "Error page",
            "scene_label": "reading_browser",
            "duration_sec": 120,
            "n_candidates": 2,
            "ocr_preview": "debugging an error",
        })
        store_mod.append_jsonl("candidates.jsonl", {
            "candidate_id": "cand_missed",
            "ts": "2026-05-19T12:09:00Z",
            "workflow_event_id": "wev_missed",
            "screen": {"frontmost_app": "Chrome", "window_title": "Error page", "ocr_snippet": "debugging an error"},
            "scene": {"label": "reading_browser"},
        })
        store_mod.append_jsonl("decisions.jsonl", {
            "decision_id": "pd_missed",
            "candidate_id": "cand_missed",
            "workflow_event_id": "wev_missed",
            "ts": "2026-05-19T12:09:05Z",
            "action": "no_ping",
        })
        store_mod.append_jsonl("candidates.jsonl", {
            "candidate_id": "cand_help_seek",
            "ts": "2026-05-19T12:15:00Z",
            "workflow_event_id": "wev_help_seek",
            "screen": {"frontmost_app": "ChatGPT", "window_title": "ChatGPT", "ocr_snippet": "help me debug this error"},
            "scene": {"label": "knowledge_qa"},
        })

        report = dataset_mod.event_examples(window="365d", limit=10)
        by_type = report["summary"]["by_type"]
        assert by_type["negative_event"] == 1
        assert by_type["missed_help_event"] == 1
        targets = {row["workflow_event_id"]: row["target"] for row in report["examples"]}
        assert targets["wev_negative"] == "no_ping"
        assert targets["wev_missed"] == "notch_ping"
    finally:
        store_mod.HARNESS_DIR = old_dir


def test_freeze_eval_manifest_is_self_contained_and_evaluable(monkeypatch, tmp_path):
    old_dir = store_mod.HARNESS_DIR
    store_mod.HARNESS_DIR = tmp_path / "state"
    try:
        for idx, (cid, action, label) in enumerate([
            ("cand_eval_pos", "notch_ping", "would_help"),
            ("cand_eval_neg", "no_ping", "good_no_ping"),
        ]):
            ts = f"2026-05-19T12:0{idx}:00Z"
            wev = f"wev_eval_{idx}"
            store_mod.append_jsonl("workflow_events.jsonl", {
                "workflow_event_id": wev,
                "ts": ts,
                "start_ts": ts,
                "last_ts": ts,
                "status": "closed",
                "app": "Chrome",
                "window_title": "Harness eval",
                "scene_label": "coding_with_todo_in_view" if idx == 0 else "reading_browser",
                "duration_sec": 60,
                "n_candidates": 1,
                "ocr_preview": "TODO ship harness" if idx == 0 else "read notes",
            })
            store_mod.append_jsonl("candidates.jsonl", {
                "candidate_id": cid,
                "ts": ts,
                "workflow_event_id": wev,
                "screen": {
                    "active": True,
                    "frontmost_app": "Chrome",
                    "window_title": "Harness eval",
                    "ocr_snippet": "TODO ship harness" if idx == 0 else "read notes",
                    "frame_age_sec": 1,
                    "capture_gap_sec": 0,
                },
                "scene": {
                    "label": "coding_with_todo_in_view" if idx == 0 else "reading_browser",
                    "strength": "strong",
                    "source": "rule",
                },
                "context": {"minutes_since_last_push": 9999},
                "user_pref": {"allowed_intents": []},
            })
            store_mod.append_jsonl("decisions.jsonl", {
                "decision_id": f"pd_eval_{idx}",
                "candidate_id": cid,
                "workflow_event_id": wev,
                "ts": ts,
                "policy_version": "fixture",
                "action": action,
                "reason_codes": ["fixture"],
            })
            if action == "notch_ping":
                store_mod.append_jsonl("outcomes.jsonl", {
                    "decision_id": f"pd_eval_{idx}",
                    "user_action": "clicked",
                    "ts": ts,
                    "interaction_summary": {"intent_signal": "committed"},
                })
            store_mod.append_jsonl("retro_labels.jsonl", {
                "label_id": f"lab_eval_{idx}",
                "candidate_id": cid,
                "decision_id": f"pd_eval_{idx}",
                "label": label,
                "confidence": 1.0,
                "ts": ts,
            })

        manifest = dataset_mod.freeze_eval_dataset(
            window="365d",
            out_dir=tmp_path / "frozen",
            limit=10,
        )
        assert not Path(manifest["source_candidates_path"]).is_absolute()
        assert (tmp_path / "frozen" / manifest["source_candidates_path"]).exists()
        assert (tmp_path / "frozen" / manifest["event_examples_path"]).exists()
        assert (tmp_path / "frozen" / manifest["split_assignments_path"]).exists()
        assert manifest["temporal_protocol"]["method"].endswith("stable_hash_tiebreak")
        report = frozen_eval_mod.evaluate_manifest(
            Path(tmp_path / "frozen" / "manifest.json"),
            policy="rule_v0",
            bootstrap_samples=20,
        )
        assert report["source"]["n_candidates"] == 2
        assert report["candidate"]["overall"]["n"] >= 2
        assert report["leakage_checks"]["pass"] is True
        assert report["leakage_checks"]["split_assignments"]["pass"] is True
        assert report["policy_execution"]["measurement_kind"] == "deterministic_policy_replay"
        assert "by_app" in report["candidate"]

        from policies import llm_icl_v0

        def fail_live_read(_filename):
            raise AssertionError("frozen llm eval must not read live jsonl examples")

        def fail_live_priors(*_args, **_kwargs):
            raise AssertionError("frozen llm eval must not read live kg priors")

        def fail_model_call(*_args, **_kwargs):
            raise AssertionError("official frozen eval should not require a live model endpoint")

        monkeypatch.setattr(llm_icl_v0, "iter_jsonl", fail_live_read)
        monkeypatch.setattr(llm_icl_v0.kg_priors_mod, "priors_for_event", fail_live_priors)
        monkeypatch.setattr(llm_icl_v0, "_call_model", fail_model_call)
        llm_icl_v0._last_call_ts = 0.0
        llm_report = frozen_eval_mod.evaluate_manifest(
            Path(tmp_path / "frozen" / "manifest.json"),
            policy="llm_icl_v0",
            config_overrides={
                "quiet_hours_start": 3,
                "quiet_hours_end": 4,
                "policy_learner": {
                    "enabled": True,
                    "offline_eval": True,
                    "eval_mode": "offline_surrogate",
                    "min_interval_sec": 0,
                    "max_examples": 8,
                },
                "privacy": {"allow_local_model_hosts": True, "block_untrusted_model_hosts": True},
            },
            bootstrap_samples=0,
        )
        assert llm_report["policy_execution"]["n_fatal_fallback_predictions"] == 0
        assert llm_report["policy_execution"]["eval_mode"] == "offline_surrogate"
        assert llm_report["policy_execution"]["measurement_kind"] == "offline_llm_policy_surrogate"
        assert llm_report["policy_execution"]["exercises_live_model"] is False
        assert llm_report["policy_execution"]["execution_counts"]["n_offline_surrogate_decisions"] > 0

        def fake_model_call(*_args, **_kwargs):
            return {
                "action": "no_ping",
                "confidence": 0.9,
                "reason_codes": ["fixture_live_model"],
                "why_now": "",
            }

        monkeypatch.setattr(llm_icl_v0, "_call_model", fake_model_call)
        llm_icl_v0._last_call_ts = 0.0
        live_report = frozen_eval_mod.evaluate_manifest(
            Path(tmp_path / "frozen" / "manifest.json"),
            policy="llm_icl_v0",
            config_overrides={
                "quiet_hours_start": 3,
                "quiet_hours_end": 4,
                "policy_learner": {
                    "enabled": True,
                    "eval_mode": "live_model",
                    "base_url": "http://127.0.0.1:11434/v1",
                    "model": "fake-live",
                    "min_interval_sec": 0,
                },
                "privacy": {"allow_local_model_hosts": True, "block_untrusted_model_hosts": True},
            },
            bootstrap_samples=0,
            require_live_model=True,
        )
        assert live_report["policy_execution"]["measurement_kind"] == "live_llm_policy_eval"
        assert live_report["policy_execution"]["execution_counts"]["n_live_model_decisions"] > 0

        source_path = tmp_path / "frozen" / manifest["source_candidates_path"]
        hard_rows = []
        for line in source_path.read_text().splitlines():
            row = json.loads(line)
            row.setdefault("scene", {})["strength"] = "weak"
            row.setdefault("scene", {})["label"] = "unknown"
            hard_rows.append(row)
        source_path.write_text("\n".join(json.dumps(row) for row in hard_rows) + "\n")
        try:
            frozen_eval_mod.evaluate_manifest(
                Path(tmp_path / "frozen" / "manifest.json"),
                policy="llm_icl_v0",
                config_overrides={
                    "quiet_hours_start": 3,
                    "quiet_hours_end": 4,
                    "policy_learner": {
                        "enabled": True,
                        "eval_mode": "live_model",
                        "base_url": "http://127.0.0.1:11434/v1",
                        "model": "fake-live",
                        "min_interval_sec": 0,
                    },
                    "privacy": {"allow_local_model_hosts": True, "block_untrusted_model_hosts": True},
                },
                bootstrap_samples=0,
                require_live_model=True,
            )
            raise AssertionError("expected live-model attestation to fail without live model decisions")
        except RuntimeError as e:
            assert "no predictions carried live_model" in str(e)

        try:
            frozen_eval_mod.evaluate_manifest(
                Path(tmp_path / "frozen" / "manifest.json"),
                policy="llm_icl_v0",
                config_overrides={"policy_learner": {"enabled": False}},
                bootstrap_samples=0,
            )
            raise AssertionError("expected llm_icl_v0 frozen eval to fail closed without learner config")
        except ValueError as e:
            assert "policy_learner.enabled" in str(e)
    finally:
        store_mod.HARNESS_DIR = old_dir


def test_frozen_eval_missing_predictions_are_coverage_not_no_ping():
    rows = [
        {
            "should_ping": True,
            "did_ping": None,
            "prediction_missing": True,
            "confidence": 1.0,
        },
        {
            "should_ping": True,
            "did_ping": True,
            "prediction_missing": False,
            "confidence": 0.5,
        },
    ]
    metrics = frozen_eval_mod._metrics(rows, bootstrap_samples=0)
    assert metrics["n"] == 2
    assert metrics["scored_n"] == 1
    assert metrics["prediction_missing"] == 1
    assert metrics["prediction_coverage"] == 0.5
    assert metrics["tp"] == 1
    assert metrics["fn"] == 0
    weighted = frozen_eval_mod._weighted_metrics(rows)
    assert weighted["weighting"] == "source_weight_v1"
    assert weighted["weighted_n"] == 0.75
    assert weighted["scored_weighted_n"] == 0.25
    assert weighted["missing_weight"] == 0.5


def test_replay_recent_outcomes_are_strictly_prior():
    outcomes = [
        {"decision_id": "pd_same", "user_action": "clicked", "ts": "2026-05-19T12:00:00Z"},
        {"decision_id": "pd_prior", "user_action": "dismissed", "ts": "2026-05-19T11:59:59Z"},
    ]
    from eval import replay as replay_mod

    recent = replay_mod._recent_outcomes_for(outcomes, "2026-05-19T12:00:00Z")
    assert [row["decision_id"] for row in recent] == ["pd_prior"]


def test_time_split_keeps_workflow_groups_isolated():
    rows = [
        {"example_id": "ex_a1", "workflow_event_id": "wev_a", "ts": "2026-05-19T12:00:00Z"},
        {"example_id": "ex_a2", "workflow_event_id": "wev_a", "ts": "2026-05-19T12:10:00Z"},
        {"example_id": "ex_b1", "workflow_event_id": "wev_b", "ts": "2026-05-19T12:20:00Z"},
        {"example_id": "ex_b2", "workflow_event_id": "wev_b", "ts": "2026-05-19T12:30:00Z"},
        {"example_id": "ex_c1", "workflow_event_id": "wev_c", "ts": "2026-05-19T12:40:00Z"},
        {"example_id": "ex_c2", "workflow_event_id": "wev_c", "ts": "2026-05-19T12:50:00Z"},
    ]
    annotated = dataset_mod._annotate_split(rows)
    by_event: dict[str, set[str]] = {}
    for row in annotated:
        by_event.setdefault(row["workflow_event_id"], set()).add(row["split"])
    assert all(len(splits) == 1 for splits in by_event.values())
    split = dataset_mod._time_split(rows)
    assert split["method"].endswith("stable_hash_tiebreak")
    assert split["split_seed"] == dataset_mod.SPLIT_SEED


def test_frozen_llm_examples_are_strictly_prior_and_not_self():
    from policies import llm_icl_v0

    event = schemas.CandidateEvent(candidate_id="cand_now")
    event.ts = "2026-05-19T12:10:00Z"
    event.workflow_event_id = "wev_now"
    rows = [
        {
            "candidate_id": "cand_prior",
            "workflow_event_id": "wev_prior",
            "ts": "2026-05-19T12:00:00Z",
            "target": "notch_ping",
            "source": "explicit",
            "confidence": 1.0,
            "context": {"app": "Chrome", "scene": "coding", "ocr_snippet": "prior"},
        },
        {
            "candidate_id": "cand_now",
            "workflow_event_id": "wev_now",
            "ts": "2026-05-19T12:10:00Z",
            "target": "no_ping",
            "source": "explicit",
            "confidence": 1.0,
            "context": {"app": "Chrome", "scene": "coding", "ocr_snippet": "self"},
        },
        {
            "candidate_id": "cand_future",
            "workflow_event_id": "wev_future",
            "ts": "2026-05-19T12:20:00Z",
            "target": "no_ping",
            "source": "explicit",
            "confidence": 1.0,
            "context": {"app": "Chrome", "scene": "coding", "ocr_snippet": "future"},
        },
    ]
    examples = llm_icl_v0._few_shot_examples_from_frozen(
        rows,
        event=event,
        limit=10,
        cutoff_ts=event.ts,
    )
    assert [row["context"]["ocr_snippet"] for row in examples] == ["prior"]


def test_config_load_merges_new_defaults(tmp_path):
    from eval import replay as replay_mod

    old_path = config_mod.CONFIG_PATH
    config_mod.CONFIG_PATH = tmp_path / "config.toml"
    try:
        config_mod.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        config_mod.CONFIG_PATH.write_text(
            "[realizer]\napi_key = \"local-key\"\nbase_url = \"http://localhost:9000\"\n"
            "[policy_learner]\nbase_url = \"http://localhost:9001\"\nmodel = \"test-model\"\n"
        )
        cfg = config_mod.load()
        assert cfg["realizer"]["api_key"] == "local-key"
        assert cfg["realizer"]["base_url"] == "http://localhost:9000"
        assert cfg["trainer"]["min_explicit_labels"] == 20
        assert cfg["experiment"]["enabled"] is True
        assert cfg["privacy"]["block_untrusted_model_hosts"] is True
        replay_cfg = replay_mod._live_gate_config()
        assert replay_cfg["policy_learner"]["base_url"] == "http://localhost:9001"
        assert replay_cfg["policy_learner"]["model"] == "test-model"
        assert replay_cfg["privacy"]["block_untrusted_model_hosts"] is True
    finally:
        config_mod.CONFIG_PATH = old_path


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


def test_trust_blocks_untrusted_model_endpoint_before_network(tmp_path):
    old_dir = store_mod.HARNESS_DIR
    store_mod.HARNESS_DIR = tmp_path

    class ShouldNotFetchFisherman:
        async def list_frames(self, count=1):
            raise AssertionError("blocked endpoint should not fetch screenshots")

    try:
        check = trust_mod.check_model_endpoint(
            "https://evil.example/v1",
            {"block_untrusted_model_hosts": True, "allowed_model_hosts": ["openrouter.ai"]},
        )
        assert not check.allowed
        event = schemas.CandidateEvent()
        event.screen.ocr_snippet = "ship harness"
        mem = schemas.MemorySnapshot.build([], [], [], 0, 0)
        result = asyncio.run(
            realizer_mod.realize(
                intent="goal_aware",
                event=event,
                memory=mem,
                fisherman=ShouldNotFetchFisherman(),
                config={
                    "base_url": "https://evil.example/v1",
                    "model": "demo",
                    "include_vision": True,
                    "privacy": {
                        "block_untrusted_model_hosts": True,
                        "allowed_model_hosts": ["openrouter.ai"],
                    },
                },
            )
        )
        assert result.error and "untrusted_model_endpoint" in result.error
        assert "model_endpoint_blocked" in result.privacy_flags
        assert result.privacy_provenance["screenshot_action"] == "blocked_untrusted_endpoint"
        row = store_mod.tail_jsonl("model_calls.jsonl", n=1)[0]
        assert row["status"] == "blocked_untrusted_endpoint"
    finally:
        store_mod.HARNESS_DIR = old_dir


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


def test_scene_vlm_backoff_blocks_repeated_failures():
    old_backoff = scene_vlm_mod._backoff_until_ts
    try:
        scene_vlm_mod._note_failure({"error_backoff_sec": 60})
        event = schemas.CandidateEvent()
        event.screen.frame_age_sec = 1
        event.screen.ocr_snippet = "ordinary screen text"
        assert scene_vlm_mod._should_skip(event, min_interval_sec=0).startswith("backoff")
    finally:
        scene_vlm_mod._backoff_until_ts = old_backoff


def test_daemon_realizer_exception_records_skipped_trace(monkeypatch, tmp_path):
    old_dir = store_mod.HARNESS_DIR
    store_mod.HARNESS_DIR = tmp_path
    try:
        async def fake_synthesize(fc, user_pref, minutes_since_last_push):
            event = schemas.CandidateEvent(candidate_id="cand_realizer_fail")
            event.ts = "2026-05-19T12:00:00Z"
            event.user_pref = user_pref
            event.screen.frontmost_app = "Cursor"
            event.screen.ocr_snippet = "TODO ship harness notification"
            event.screen.frame_age_sec = 1
            event.context.minutes_since_last_push = minutes_since_last_push
            return event

        async def fail_realize(*args, **kwargs):
            raise FileNotFoundError("missing prompt")

        monkeypatch.setattr(daemon_mod, "synthesize", fake_synthesize)
        monkeypatch.setattr(daemon_mod.realizer_mod, "realize", fail_realize)
        memory = memory_mod.SessionMemory(window_min=120, idle_boundary_sec=90)
        cfg = {
            "daemon": {"http_port": 7893},
            "gate": {
                "active_policy": "rule_v0",
                "cooldown_min": 0,
                "negative_feedback_backoff_min": 0,
                "resume_suppression_sec": 90,
                "quiet_hours_start": 3,
                "quiet_hours_end": 4,
            },
            "intents": {"enabled": ["focus_nudge"]},
            "experiment": {"enabled": False},
            "policy_learner": {"enabled": False},
            "privacy": {},
            "realizer": {"base_url": "http://localhost:9000", "model": "test"},
            "critic": {},
            "push": {"channel": "notch_pill"},
        }

        asyncio.run(daemon_mod._tick(
            config=cfg,
            fc=object(),
            memory=memory,
            workflow_events=None,
            last_push_at_ref=[None],
        ))

        decisions = store_mod.tail_jsonl("decisions.jsonl")
        traces = store_mod.tail_jsonl("traces.jsonl")
        assert decisions[-1]["action"] == "notch_ping"
        assert traces[-1]["delivery"]["channel"] == "skipped"
        assert "FileNotFoundError" in traces[-1]["realization"]["error"]
        stages = [row["stage"] for row in traces[-1]["lifecycle"]]
        assert "realizer_failed" in stages
        assert "terminal_skipped" in stages
    finally:
        store_mod.HARNESS_DIR = old_dir


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
    event.ts = "2026-05-19T12:00:00Z"
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


def test_experiment_holdout_suppresses_ping_with_counterfactual():
    decision = schemas.ProactiveDecision(
        decision_id="pd_exp",
        candidate_id="cand_exp",
        policy_version="rule_v0",
        action="notch_ping",
        intent="goal_aware",
        reason_codes=["goal_aligned_help"],
    )
    event = schemas.CandidateEvent(candidate_id="cand_exp")
    assigned = experiments_mod.apply(
        decision,
        event,
        {"enabled": True, "salt": "test", "holdout_rate": 1.0, "explore_ping_rate": 0.0},
    )
    assert assigned.action == "no_ping"
    assert assigned.intent is None
    assert "experiment_holdout" in assigned.reason_codes
    assert assigned.experiment["assignment"] == "holdout"
    assert assigned.experiment["counterfactual_action"] == "notch_ping"


def test_experiment_exploration_respects_hard_gates():
    from harness.policy_contract import HARD_NO_PING_REASONS
    from policies import llm_icl_v0

    assert set(llm_icl_v0.HARD_NO_PING_REASONS) == set(HARD_NO_PING_REASONS)
    assert set(experiments_mod.HARD_NO_PING_REASONS) == set(HARD_NO_PING_REASONS)

    event = schemas.CandidateEvent(candidate_id="cand_exp")
    hard = schemas.ProactiveDecision(
        decision_id="pd_hard",
        candidate_id="cand_exp",
        policy_version="rule_v0",
        action="no_ping",
        reason_codes=["sensitive_scene"],
    )
    cfg = {"enabled": True, "salt": "test", "explore_ping_rate": 1.0}
    assert experiments_mod.apply(hard, event, cfg).action == "no_ping"
    assert experiments_mod.apply(hard, event, cfg | {"respect_hard_gates": False}).action == "no_ping"

    soft = schemas.ProactiveDecision(
        decision_id="pd_soft",
        candidate_id="cand_exp",
        policy_version="rule_v0",
        action="no_ping",
        reason_codes=["no_clear_help"],
    )
    explored = experiments_mod.apply(soft, event, cfg)
    assert explored.action == "notch_ping"
    assert explored.intent == "goal_aware"
    assert explored.experiment["assignment"] == "explore_ping"


def test_rule_v0_negative_feedback_backoff_is_time_bounded():
    from policies import rule_v0

    event = schemas.CandidateEvent()
    event.ts = "2026-05-19T12:10:00Z"
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
        "ts": "2026-05-19T12:00:00Z",
    }]

    assert rule_v0.decide(event, mem, stale, cfg).action == "notch_ping"
    blocked = rule_v0.decide(event, mem, recent, cfg)
    assert blocked.action == "no_ping"
    assert "recent_negative_feedback" in blocked.reason_codes


def test_rule_v0_soft_reject_hover_backoff_is_live_signal():
    from policies import rule_v0

    event = schemas.CandidateEvent()
    event.ts = "2026-05-19T12:10:00Z"
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
        "ts": "2026-05-19T12:00:00Z",
        "interaction_summary": {"intent_signal": "rejection_considered"},
    }]

    blocked = rule_v0.decide(event, mem, soft_reject, cfg)
    assert blocked.action == "no_ping"
    assert "recent_negative_feedback" in blocked.reason_codes


def test_rule_v0_negative_feedback_backoff_uses_event_time_for_replay():
    from policies import rule_v0

    event = schemas.CandidateEvent()
    event.ts = "2026-05-19T12:10:00Z"
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
    replay_recent = [{"user_action": "dismissed", "ts": "2026-05-19T12:00:00Z"}]
    replay_stale = [{"user_action": "dismissed", "ts": "2026-05-19T11:00:00Z"}]

    blocked = rule_v0.decide(event, mem, replay_recent, cfg)
    assert blocked.action == "no_ping"
    assert "recent_negative_feedback" in blocked.reason_codes
    assert rule_v0.decide(event, mem, replay_stale, cfg).action == "notch_ping"


def test_rule_v0_ignored_timeout_counts_as_recent_negative_feedback():
    from policies import rule_v0

    event = schemas.CandidateEvent()
    event.ts = "2026-05-19T12:10:00Z"
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
    ignored_timeout = [{
        "user_action": "timed_out",
        "ts": "2026-05-19T12:00:00Z",
        "interaction_summary": {"intent_signal": "ignored"},
    }]

    blocked = rule_v0.decide(event, mem, ignored_timeout, cfg)
    assert blocked.action == "no_ping"
    assert "recent_negative_feedback" in blocked.reason_codes


def test_llm_icl_policy_uses_model_for_binary_decision(monkeypatch, tmp_path):
    from policies import llm_icl_v0

    old_dir = store_mod.HARNESS_DIR
    store_mod.HARNESS_DIR = tmp_path
    llm_icl_v0._last_call_ts = 0.0
    try:
        event = schemas.CandidateEvent(candidate_id="cand_llm")
        event.ts = "2026-05-19T12:00:00Z"
        event.screen.frame_age_sec = 5.0
        event.screen.frontmost_app = "Chrome"
        event.screen.ocr_snippet = "drafting harness policy learner"
        event.scene = schemas.SceneTag(label="reading_browser", strength="strong", source="rule")
        mem = schemas.MemorySnapshot.build(
            recent_apps=["Chrome"],
            recent_scenes=["reading_browser"],
            recent_outcomes=[],
            app_switches_last_15m=0,
            minutes_on_current_app=12.0,
            recent_workflow_events=[{
                "workflow_event_id": "wev_llm",
                "status": "open",
                "app": "Chrome",
                "window_title": "Harness policy notes",
                "scene_label": "reading_browser",
                "duration_sec": 42.0,
                "n_candidates": 4,
                "ocr_preview": "drafting harness policy learner",
            }],
        )
        event.workflow_event_id = "wev_llm"
        cfg = {
            "cooldown_min": 5,
            "negative_feedback_backoff_min": 15,
            "quiet_hours_start": 3,
            "quiet_hours_end": 4,
            "sensitivity": "responsive",
            "daily_goal": "ship harness",
            "allowed_intents": [],
            "privacy": {"allow_local_model_hosts": True, "block_untrusted_model_hosts": True},
            "policy_learner": {
                "enabled": True,
                "base_url": "http://localhost:9000",
                "model": "test-policy",
                "min_interval_sec": 0,
                "min_confidence_to_ping": 0.55,
            },
        }

        def fake_call(cfg, base_url, model, messages):
            body = json.loads(messages[1]["content"])
            packet = body["policy_context_packet"]
            assert packet["packet_id"].startswith("pkt_")
            assert packet["current_observation"]["ocr_snippet"] == "drafting harness policy learner"
            assert packet["current_workflow_event"]["window_title"] == "Harness policy notes"
            assert packet["kg_priors"]
            return {
                "action": "notch_ping",
                "confidence": 0.82,
                "reason_codes": ["goal_aligned"],
                "why_now": "The visible work matches today's harness goal.",
            }

        monkeypatch.setattr(llm_icl_v0, "_call_model", fake_call)
        decision = llm_icl_v0.decide(event, mem, [], cfg)

        assert decision.action == "notch_ping"
        assert decision.policy_version == "llm_icl_v0"
        assert "llm_icl_policy" in decision.reason_codes
        assert decision.evidence["context_packet_id"].startswith("pkt_")
        packets = store_mod.tail_jsonl("context_packets.jsonl", n=1)
        assert packets[0]["packet_id"] == decision.evidence["context_packet_id"]
        assert packets[0]["workflow_event_id"] == "wev_llm"
        assert sql_store_mod.count_rows("context_packets", base_dir=tmp_path) == 1
        assert store_mod.tail_jsonl("model_calls.jsonl", n=1)[0]["purpose"] == "policy_learner"
    finally:
        store_mod.HARNESS_DIR = old_dir


def test_llm_icl_policy_respects_rule_hard_gates(monkeypatch, tmp_path):
    from policies import llm_icl_v0

    old_dir = store_mod.HARNESS_DIR
    store_mod.HARNESS_DIR = tmp_path
    def fail_call(*args, **kwargs):
        raise AssertionError("LLM should not be called for hard gates")

    try:
        monkeypatch.setattr(llm_icl_v0, "_call_model", fail_call)
        event = schemas.CandidateEvent()
        event.context.in_call = True
        event.screen.frame_age_sec = 5.0
        event.scene = schemas.SceneTag(label="reading_browser", strength="strong", source="rule")
        mem = schemas.MemorySnapshot.build([], [], [], 0, 0.0)
        cfg = {
            "cooldown_min": 5,
            "negative_feedback_backoff_min": 15,
            "quiet_hours_start": 3,
            "quiet_hours_end": 4,
            "policy_learner": {"enabled": True},
        }

        decision = llm_icl_v0.decide(event, mem, [], cfg)
        assert decision.action == "no_ping"
        assert "in_call" in decision.reason_codes
        assert "rule_hard_gate" in decision.reason_codes
        assert decision.evidence["context_packet_id"].startswith("pkt_")
    finally:
        store_mod.HARNESS_DIR = old_dir


def test_session_memory_breaks_continuity_across_sleep_gap(tmp_path):
    old_dir = store_mod.HARNESS_DIR
    store_mod.HARNESS_DIR = tmp_path
    try:
        mem = memory_mod.SessionMemory(window_min=120, idle_boundary_sec=90)
        first = schemas.CandidateEvent()
        first.ts = "2026-05-19T12:00:00Z"
        first.screen.frontmost_app = "Chrome"
        first.screen.frame_age_sec = 1
        first.scene = schemas.SceneTag(label="reading_browser", strength="medium", source="rule")
        mem.record(first)

        resumed = schemas.CandidateEvent()
        resumed.ts = "2026-05-19T12:30:00Z"
        resumed.screen.frontmost_app = "Chrome"
        resumed.screen.frame_age_sec = 1
        resumed.screen.capture_gap_sec = 1800
        resumed.scene = schemas.SceneTag(label="reading_browser", strength="medium", source="rule")
        mem.record(resumed)
        snap = mem.snapshot([])

        assert snap.minutes_on_current_app == 0.0
        assert snap.session_boundary == "capture_gap"
        assert snap.last_event_gap_sec == 1800.0
        assert snap.recent_apps == ["Chrome"]
    finally:
        store_mod.HARNESS_DIR = old_dir


def test_rule_v0_suppresses_resume_from_idle():
    from policies import rule_v0

    event = schemas.CandidateEvent()
    event.ts = "2026-05-19T12:00:00Z"
    event.screen.frame_age_sec = 5.0
    event.screen.capture_gap_sec = 240.0
    event.screen.ocr_snippet = "ship harness"
    event.scene = schemas.SceneTag(label="reading_browser", strength="medium", source="rule")
    mem = schemas.MemorySnapshot.build(
        recent_apps=["Chrome"],
        recent_scenes=["reading_browser"],
        recent_outcomes=[],
        app_switches_last_15m=0,
        minutes_on_current_app=0.0,
        last_event_gap_sec=240.0,
        session_boundary="capture_gap",
    )
    cfg = {
        "cooldown_min": 5,
        "negative_feedback_backoff_min": 15,
        "resume_suppression_sec": 90,
        "quiet_hours_start": 3,
        "quiet_hours_end": 4,
        "sensitivity": "responsive",
        "daily_goal": "ship harness",
        "allowed_intents": [],
    }

    blocked = rule_v0.decide(event, mem, [], cfg)
    assert blocked.action == "no_ping"
    assert "resume_from_idle" in blocked.reason_codes
    assert "resume_from_idle" in experiments_mod.HARD_NO_PING_REASONS


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
        assert rows[0]["lifecycle"][-1]["stage"] == "outcome"
        assert store_mod.patch_trace("pd_test", {"delivery": {"pushed": True}}, lifecycle_stage="dispatch_done")
        rows = store_mod.tail_jsonl("traces.jsonl")
        assert rows[0]["delivery"]["pushed"] is True
        assert rows[0]["lifecycle"][-1]["stage"] == "dispatch_done"
    finally:
        store_mod.HARNESS_DIR = old_dir


def test_pending_claim_lease_and_completion(tmp_path):
    old_dir = store_mod.HARNESS_DIR
    store_mod.HARNESS_DIR = tmp_path
    try:
        store_mod.write_pending(
            "pd_pending",
            {
                "decision_id": "pd_pending",
                "candidate_id": "cand_pending",
                "message": "hello",
            },
        )
        first = store_mod.claim_pending(lease_sec=60)
        assert first is not None
        assert first["decision_id"] == "pd_pending"
        assert first["pending_attempts"] == 1
        assert store_mod.claim_pending(lease_sec=60) is None

        p = tmp_path / "pending" / "pd_pending.json"
        with open(p) as f:
            payload = json.load(f)
        payload["pending_lease_until_unix"] = time.time() - 1
        with open(p, "w") as f:
            json.dump(payload, f)

        second = store_mod.claim_pending(lease_sec=60)
        assert second is not None
        assert second["pending_attempts"] == 2
        assert store_mod.complete_pending("pd_pending")
        assert store_mod.claim_pending(lease_sec=60) is None
    finally:
        store_mod.HARNESS_DIR = old_dir


def test_expired_pending_is_terminal_and_not_claimed(tmp_path):
    old_dir = store_mod.HARNESS_DIR
    store_mod.HARNESS_DIR = tmp_path
    try:
        store_mod.write_pending(
            "pd_expired",
            {
                "decision_id": "pd_expired",
                "candidate_id": "cand_expired",
                "message": "stale",
                "expires_at_unix": time.time() - 1,
            },
        )
        assert store_mod.claim_pending(lease_sec=60) is None
        assert not (tmp_path / "pending" / "pd_expired.json").exists()
        deliveries = store_mod.tail_jsonl("deliveries.jsonl")
        assert deliveries[-1]["decision_id"] == "pd_expired"
        assert deliveries[-1]["delivery_action"] == "never_displayed_expired"
    finally:
        store_mod.HARNESS_DIR = old_dir


def test_pending_display_ack_writes_claimed_capture_metric(tmp_path):
    from aiohttp.test_utils import make_mocked_request

    old_dir = store_mod.HARNESS_DIR
    store_mod.HARNESS_DIR = tmp_path
    try:
        decision = {
            "decision_id": "pd_claimed",
            "candidate_id": "cand_claimed",
            "ts": "2026-05-19T12:00:00Z",
            "action": "notch_ping",
        }
        store_mod.append_jsonl("decisions.jsonl", decision)
        store_mod.write_pending(
            "pd_claimed",
            {
                "decision_id": "pd_claimed",
                "candidate_id": "cand_claimed",
                "message": "hello",
            },
        )
        req = make_mocked_request("GET", "/pending")
        resp = asyncio.run(server_mod.get_pending(req))
        body = json.loads(resp.text)
        assert body["decision_id"] == "pd_claimed"
        deliveries = store_mod.tail_jsonl("deliveries.jsonl")
        assert deliveries[-1]["decision_id"] == "pd_claimed"
        assert deliveries[-1]["delivery_action"] == "dequeued"

        report = metrics_mod.compute(window="365d")
        assert report["n_claimed_pings"] == 0
        assert report["outcomes"]["capture_rate_for_pings"] == 0.0
        assert report["outcomes"]["capture_rate_for_claimed_pings"] is None

        ack_req = make_mocked_request("POST", "/delivery-ack?id=pd_claimed")
        ack_resp = asyncio.run(server_mod.post_delivery_ack(ack_req))
        assert json.loads(ack_resp.text)["ok"] is True
        deliveries = store_mod.tail_jsonl("deliveries.jsonl")
        assert deliveries[-1]["delivery_action"] == "displayed_ack"

        report = metrics_mod.compute(window="365d")
        assert report["n_claimed_pings"] == 1
        assert report["outcomes"]["capture_rate_for_claimed_pings"] == 0.0

        store_mod.append_jsonl(
            "outcomes.jsonl",
            {
                "decision_id": "pd_claimed",
                "user_action": "clicked",
                "ts": "2026-05-19T12:00:05Z",
            },
        )
        report = metrics_mod.compute(window="365d")
        assert report["outcomes"]["capture_rate_for_claimed_pings"] == 1.0
    finally:
        store_mod.HARNESS_DIR = old_dir


def test_outcome_validation_is_idempotent_and_rejects_stale(tmp_path):
    from aiohttp.test_utils import make_mocked_request

    old_dir = store_mod.HARNESS_DIR
    store_mod.HARNESS_DIR = tmp_path
    try:
        unknown = make_mocked_request("POST", "/outcome?id=pd_unknown&user_action=clicked")
        resp = asyncio.run(server_mod.post_outcome(unknown))
        assert resp.status == 404

        store_mod.append_jsonl("decisions.jsonl", {
            "decision_id": "pd_ok",
            "candidate_id": "cand_ok",
            "ts": "2026-05-19T12:00:00Z",
            "action": "notch_ping",
        })
        store_mod.write_pending("pd_ok", {
            "decision_id": "pd_ok",
            "candidate_id": "cand_ok",
            "message": "hello",
        })
        not_dequeued = make_mocked_request("POST", "/outcome?id=pd_ok&user_action=clicked")
        resp = asyncio.run(server_mod.post_outcome(not_dequeued))
        assert resp.status == 409
        assert json.loads(resp.text)["error"] == "not_dequeued"

        asyncio.run(server_mod.get_pending(make_mocked_request("GET", "/pending")))
        needs_ack = make_mocked_request("POST", "/outcome?id=pd_ok&user_action=clicked")
        resp = asyncio.run(server_mod.post_outcome(needs_ack))
        assert resp.status == 409
        assert json.loads(resp.text)["error"] == "display_ack_required"
        asyncio.run(server_mod.post_delivery_ack(make_mocked_request("POST", "/delivery-ack?id=pd_ok")))
        first = make_mocked_request("POST", "/outcome?id=pd_ok&user_action=clicked")
        resp = asyncio.run(server_mod.post_outcome(first))
        assert json.loads(resp.text)["ok"] is True
        assert len(store_mod.tail_jsonl("outcomes.jsonl")) == 1
        assert store_mod.tail_jsonl("deliveries.jsonl")[-1]["delivery_action"] == "displayed_ack"
        assert store_mod.tail_jsonl("deliveries.jsonl")[-1]["ack_source"] == "client_ack"
        duplicate = make_mocked_request("POST", "/outcome?id=pd_ok&user_action=clicked")
        resp = asyncio.run(server_mod.post_outcome(duplicate))
        body = json.loads(resp.text)
        assert body["ok"] is True
        assert body["duplicate"] is True
        assert len(store_mod.tail_jsonl("outcomes.jsonl")) == 1

        store_mod.append_jsonl("decisions.jsonl", {
            "decision_id": "pd_expired_outcome",
            "candidate_id": "cand_expired_outcome",
            "ts": "2026-05-19T12:00:00Z",
            "action": "notch_ping",
        })
        store_mod.write_pending("pd_expired_outcome", {
            "decision_id": "pd_expired_outcome",
            "candidate_id": "cand_expired_outcome",
            "message": "stale",
            "expires_at_unix": time.time() - 1,
        })
        assert store_mod.sweep_expired_pending() == 1
        stale = make_mocked_request("POST", "/outcome?id=pd_expired_outcome&user_action=clicked")
        resp = asyncio.run(server_mod.post_outcome(stale))
        assert resp.status == 409
        assert json.loads(resp.text)["error"] == "terminal_delivery_expired"
    finally:
        store_mod.HARNESS_DIR = old_dir


def test_displayed_ping_late_outcome_rejected_after_timeout(tmp_path):
    from aiohttp.test_utils import make_mocked_request

    old_dir = store_mod.HARNESS_DIR
    store_mod.HARNESS_DIR = tmp_path
    try:
        store_mod.append_jsonl("decisions.jsonl", {
            "decision_id": "pd_late",
            "candidate_id": "cand_late",
            "ts": "2026-05-19T12:00:00Z",
            "action": "notch_ping",
        })
        store_mod.write_pending("pd_late", {
            "decision_id": "pd_late",
            "candidate_id": "cand_late",
            "message": "hello",
            "expires_at_unix": time.time() + 60,
        })
        assert json.loads(asyncio.run(server_mod.get_pending(make_mocked_request("GET", "/pending"))).text)["decision_id"] == "pd_late"
        assert json.loads(asyncio.run(server_mod.post_delivery_ack(make_mocked_request("POST", "/delivery-ack?id=pd_late"))).text)["ok"] is True

        p = tmp_path / "pending" / "pd_late.json"
        payload = json.loads(p.read_text())
        payload["expires_at_unix"] = time.time() - 1
        p.write_text(json.dumps(payload))
        assert store_mod.sweep_expired_pending() == 1
        assert store_mod.tail_jsonl("deliveries.jsonl")[-1]["delivery_action"] == "displayed_timeout_no_outcome"

        resp = asyncio.run(server_mod.post_outcome(make_mocked_request("POST", "/outcome?id=pd_late&user_action=clicked")))
        assert resp.status == 409
        assert json.loads(resp.text)["error"] == "terminal_delivery_expired"
        assert store_mod.tail_jsonl("outcomes.jsonl") == []
    finally:
        store_mod.HARNESS_DIR = old_dir


def test_terminal_notifier_display_allows_callback_outcome(monkeypatch, tmp_path):
    from aiohttp.test_utils import make_mocked_request

    class FakeProc:
        returncode = 0

        async def wait(self):
            return None

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return FakeProc()

    old_dir = store_mod.HARNESS_DIR
    store_mod.HARNESS_DIR = tmp_path
    try:
        monkeypatch.setattr(push_mod.shutil, "which", lambda _name: "/usr/local/bin/terminal-notifier")
        monkeypatch.setattr(push_mod.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
        decision = schemas.ProactiveDecision(
            decision_id="pd_terminal",
            candidate_id="cand_terminal",
            policy_version="rule_v0",
            action="notch_ping",
            intent="goal_aware",
        )
        realization = schemas.Realization(
            model="fixture",
            base_url="local",
            prompt_version="fixture",
            message="Terminal notifier fallback",
        )
        store_mod.append_jsonl("decisions.jsonl", {
            **decision.to_dict(),
            "ts": "2026-05-19T12:00:00Z",
        })
        delivery = asyncio.run(push_mod.dispatch(
            decision,
            realization,
            {"channel": "terminal_notifier", "harness_port": 7893},
        ))
        assert delivery.pushed is True
        deliveries = store_mod.tail_jsonl("deliveries.jsonl")
        assert deliveries[-1]["channel"] == "terminal_notifier"
        assert deliveries[-1]["delivery_action"] == "displayed_ack"
        assert deliveries[-1]["ack_source"] == "terminal_notifier_dispatch"
        assert (tmp_path / "pending" / "pd_terminal.json").exists()

        resp = asyncio.run(server_mod.post_outcome(make_mocked_request(
            "GET",
            "/outcome?id=pd_terminal&user_action=clicked",
        )))
        assert resp.status == 200
        assert json.loads(resp.text)["ok"] is True
        assert store_mod.tail_jsonl("outcomes.jsonl")[-1]["decision_id"] == "pd_terminal"
        assert not (tmp_path / "pending" / "pd_terminal.json").exists()
    finally:
        store_mod.HARNESS_DIR = old_dir


def test_terminal_notifier_ignored_ping_expires_to_terminal_delivery(monkeypatch, tmp_path):
    class FakeProc:
        returncode = 0

        async def wait(self):
            return None

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return FakeProc()

    old_dir = store_mod.HARNESS_DIR
    store_mod.HARNESS_DIR = tmp_path
    try:
        monkeypatch.setattr(push_mod.shutil, "which", lambda _name: "/usr/local/bin/terminal-notifier")
        monkeypatch.setattr(push_mod.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
        decision = schemas.ProactiveDecision(
            decision_id="pd_terminal_ignored",
            candidate_id="cand_terminal_ignored",
            policy_version="rule_v0",
            action="notch_ping",
            intent="goal_aware",
        )
        realization = schemas.Realization(
            model="fixture",
            base_url="local",
            prompt_version="fixture",
            message="Terminal notifier fallback",
        )
        store_mod.append_jsonl("decisions.jsonl", {
            **decision.to_dict(),
            "ts": "2026-05-19T12:00:00Z",
        })
        delivery = asyncio.run(push_mod.dispatch(
            decision,
            realization,
            {"channel": "terminal_notifier", "harness_port": 7893, "auto_dismiss_sec": 60},
        ))
        assert delivery.pushed is True
        pending_path = tmp_path / "pending" / "pd_terminal_ignored.json"
        payload = json.loads(pending_path.read_text())
        assert payload["claimable_by_notch"] is False
        assert store_mod.claim_pending() is None
        payload["expires_at_unix"] = time.time() - 1
        pending_path.write_text(json.dumps(payload))
        assert store_mod.sweep_expired_pending() == 1
        deliveries = store_mod.tail_jsonl("deliveries.jsonl")
        assert deliveries[-1]["channel"] == "terminal_notifier"
        assert deliveries[-1]["delivery_action"] == "displayed_timeout_no_outcome"
    finally:
        store_mod.HARNESS_DIR = old_dir


def test_metrics_supplements_partial_sqlite_delivery_backfill(tmp_path):
    old_dir = store_mod.HARNESS_DIR
    store_mod.HARNESS_DIR = tmp_path
    try:
        store_mod.append_jsonl(
            "decisions.jsonl",
            {
                "decision_id": "pd_jsonl_only_claim",
                "candidate_id": "cand_jsonl_only_claim",
                "ts": "2026-05-19T12:00:00Z",
                "action": "notch_ping",
            },
        )
        store_mod.append_jsonl(
            "deliveries.jsonl",
            {
                "delivery_id": "del_unrelated",
                "decision_id": "pd_unrelated",
                "candidate_id": "cand_unrelated",
                "delivery_action": "claimed",
                "ts": "2026-05-19T12:00:01Z",
            },
        )

        jsonl_only_delivery = {
            "delivery_id": "del_jsonl_only_claim",
            "decision_id": "pd_jsonl_only_claim",
            "candidate_id": "cand_jsonl_only_claim",
            "delivery_action": "claimed",
            "ts": "2026-05-19T12:00:02Z",
        }
        with open(tmp_path / "deliveries.jsonl", "a") as f:
            f.write(json.dumps(jsonl_only_delivery) + "\n")

        assert sql_store_mod.count_rows("deliveries") == 1
        report = metrics_mod.compute(window="365d")
        assert report["n_claimed_pings"] == 1
        assert report["outcomes"]["claimed_pings"] == 1
    finally:
        store_mod.HARNESS_DIR = old_dir


def test_launchd_plist_points_at_harness_module(tmp_path):
    plist = service_mod.build_plist(
        python_executable="/tmp/venv/bin/python",
        repo_dir=tmp_path / "harness",
        harness_dir=tmp_path / ".harness",
    )
    assert plist["Label"] == "com.fisherman.harness"
    assert plist["KeepAlive"] is True
    assert plist["RunAtLoad"] is True
    assert plist["ProgramArguments"] == [
        "/tmp/venv/bin/python",
        "-m",
        "harness.cli",
        "start",
        "--foreground",
    ]
    assert plist["WorkingDirectory"].endswith("harness")


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
        workflow_event = {
            "workflow_event_id": "wev_sql",
            "ts": "2026-05-19T12:00:06Z",
            "start_ts": "2026-05-19T12:00:00Z",
            "last_ts": "2026-05-19T12:00:06Z",
            "end_ts": "2026-05-19T12:00:06Z",
            "status": "closed",
            "app": "Chrome",
            "window_title": "Harness eval",
            "scene_label": "reading_browser",
            "n_candidates": 2,
            "duration_sec": 6.0,
            "close_reason": "window_changed",
        }
        delivery = {
            "delivery_id": "del_sql",
            "decision_id": "pd_sql",
            "candidate_id": "cand_sql",
            "delivery_action": "claimed",
            "channel": "notch_pill",
            "pending_attempts": 1,
            "ts": "2026-05-19T12:00:07Z",
        }
        curation = {
            "curation_id": "cur_sql",
            "target_type": "candidate",
            "target_id": "cand_sql",
            "action": "retain",
            "reason": "test",
            "source": "manual",
            "ts": "2026-05-19T12:00:08Z",
        }

        store_mod.append_jsonl("candidates.jsonl", candidate)
        store_mod.append_jsonl("decisions.jsonl", decision)
        store_mod.append_jsonl("traces.jsonl", trace)
        store_mod.append_jsonl("outcomes.jsonl", outcome)
        store_mod.append_jsonl("model_calls.jsonl", model_call)
        store_mod.append_jsonl("retro_labels.jsonl", label)
        store_mod.append_jsonl("workflow_events.jsonl", workflow_event)
        store_mod.append_jsonl("deliveries.jsonl", delivery)
        store_mod.append_jsonl("curation.jsonl", curation)

        assert sql_store_mod.db_path().exists()
        assert sql_store_mod.count_rows("event_log") == 9
        assert sql_store_mod.count_rows("candidates") == 1
        assert sql_store_mod.count_rows("decisions") == 1
        assert sql_store_mod.count_rows("traces") == 1
        assert sql_store_mod.count_rows("outcomes") == 1
        assert sql_store_mod.count_rows("deliveries") == 1
        assert sql_store_mod.count_rows("model_calls") == 1
        assert sql_store_mod.count_rows("retro_labels") == 1
        assert sql_store_mod.count_rows("workflow_events") == 1
        assert sql_store_mod.count_rows("curation") == 1

        reward = {"version": "v2", "value": -0.8}
        assert store_mod.attach_outcome_to_trace("pd_sql", outcome, reward)
        trace_rows = sql_store_mod.recent_rows("traces", limit=1)
        assert trace_rows[0]["outcome_action"] == "dismissed"
        assert trace_rows[0]["reward_value"] == -0.8
        assert '"user_action":"dismissed"' in trace_rows[0]["payload_json"]
        payloads = sql_store_mod.payload_rows(
            "decisions",
            since_iso="2026-05-19T12:00:00Z",
        )
        assert payloads[0]["decision_id"] == "pd_sql"
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
        assert report["implicit"]["usable"] == 1
        assert report["implicit"]["positive"] == 1
        assert report["labels"]["agreement_rate"] == 0.5
        assert report["labels"]["precision_labeled"] == 0.5
        assert report["labels"]["recall_labeled"] == 0.5
        assert report["labels"]["f1_labeled"] == 0.5
        assert report["labels"]["false_interruption_rate_labeled"] == 0.5
        assert report["labels"]["missed_help_rate_labeled"] == 0.5
        assert report["data_readiness"]["needs_labels_for_personalization"] == 16
    finally:
        store_mod.HARNESS_DIR = old_dir


def test_metrics_uses_latest_label_when_implicit_panel_corrects(tmp_path):
    old_dir = store_mod.HARNESS_DIR
    store_mod.HARNESS_DIR = tmp_path
    try:
        store_mod.append_jsonl(
            "decisions.jsonl",
            {
                "decision_id": "pd_corrected",
                "candidate_id": "cand_corrected",
                "ts": "2026-05-19T12:00:00Z",
                "action": "notch_ping",
            },
        )
        store_mod.append_jsonl(
            "retro_labels.jsonl",
            {
                "label_id": "implicit_panel_pd_corrected",
                "decision_id": "pd_corrected",
                "candidate_id": "cand_corrected",
                "label": "would_annoy",
                "ts": "2026-05-19T12:01:00Z",
            },
        )
        store_mod.append_jsonl(
            "retro_labels.jsonl",
            {
                "label_id": "implicit_panel_pd_corrected",
                "decision_id": "pd_corrected",
                "candidate_id": "cand_corrected",
                "label": "would_help",
                "ts": "2026-05-19T12:02:00Z",
            },
        )

        report = metrics_mod.compute(window="365d")
        assert report["labels"]["n"] == 1
        assert report["labels"]["counts"] == {"would_help": 1}
    finally:
        store_mod.HARNESS_DIR = old_dir


def test_implicit_outcomes_become_confidence_weighted_weak_labels():
    decision = {
        "decision_id": "pd_implicit",
        "candidate_id": "cand_implicit",
        "action": "notch_ping",
        "reason_codes": ["goal_aligned_help"],
    }
    positive = implicit_mod.weak_label_for_outcome(
        {
            "decision_id": "pd_implicit",
            "user_action": "clicked",
            "interaction_summary": {"intent_signal": "committed"},
        },
        decision,
    )
    assert positive["label"] == "would_help"
    assert positive["direction"] == "positive"
    assert positive["confidence"] > 0.9

    weak_negative = implicit_mod.weak_label_for_outcome(
        {
            "decision_id": "pd_implicit",
            "user_action": "timed_out",
            "interaction_summary": {"intent_signal": "rejection_considered"},
        },
        decision,
    )
    assert weak_negative["label"] == "would_annoy"
    assert weak_negative["direction"] == "negative"

    ignored = implicit_mod.weak_label_for_outcome(
        {"decision_id": "pd_implicit", "user_action": "timed_out"},
        decision,
    )
    assert ignored["label"] == "would_annoy"
    assert ignored["direction"] == "weak_negative"
    assert ignored["usable_for_training"] is True

    summary = implicit_mod.summarize([positive, weak_negative, ignored])
    assert summary["usable"] == 3
    assert summary["positive"] == 1
    assert summary["negative"] == 2


def test_kg_priors_match_current_event_from_implicit_signal(tmp_path):
    old_dir = store_mod.HARNESS_DIR
    store_mod.HARNESS_DIR = tmp_path
    old_cache = kg_priors_mod._CACHE
    kg_priors_mod._CACHE = None
    try:
        store_mod.append_jsonl(
            "candidates.jsonl",
            {
                "candidate_id": "cand_kg",
                "ts": "2026-05-19T12:00:00Z",
                "screen": {"frontmost_app": "Chrome", "window_title": "Harness paper", "ocr_snippet": "policy evaluation"},
                "scene": {"label": "reading_browser"},
            },
        )
        store_mod.append_jsonl(
            "decisions.jsonl",
            {"decision_id": "pd_kg", "candidate_id": "cand_kg", "ts": "2026-05-19T12:00:01Z", "action": "notch_ping"},
        )
        store_mod.append_jsonl(
            "outcomes.jsonl",
            {"decision_id": "pd_kg", "user_action": "clicked", "ts": "2026-05-19T12:00:02Z"},
        )
        event = schemas.CandidateEvent(candidate_id="cand_now")
        event.screen.frontmost_app = "Chrome"
        event.screen.window_title = "Harness paper"
        event.screen.ocr_snippet = "policy evaluation"
        event.scene = schemas.SceneTag(label="reading_browser")

        matched = kg_priors_mod.priors_for_event(event, window="365d")
        assert matched["n_examples"] == 1
        assert any(row["feature"] == "app:chrome" for row in matched["matches"])
    finally:
        kg_priors_mod._CACHE = old_cache
        store_mod.HARNESS_DIR = old_dir


def test_hard_example_miner_respects_curation_exclusions(tmp_path):
    old_dir = store_mod.HARNESS_DIR
    store_mod.HARNESS_DIR = tmp_path
    try:
        for cid, did, action, minute in [
            ("cand_pos", "pd_pos", "notch_ping", "00"),
            ("cand_neg", "pd_neg", "no_ping", "01"),
        ]:
            store_mod.append_jsonl(
                "candidates.jsonl",
                {
                    "candidate_id": cid,
                    "ts": f"2026-05-19T12:{minute}:00Z",
                    "screen": {"frontmost_app": "Chrome", "window_title": "Harness eval", "ocr_snippet": "policy evaluation"},
                    "scene": {"label": "reading_browser"},
                },
            )
            store_mod.append_jsonl(
                "decisions.jsonl",
                {"decision_id": did, "candidate_id": cid, "ts": f"2026-05-19T12:{minute}:01Z", "action": action},
            )
        store_mod.append_jsonl(
            "outcomes.jsonl",
            {"decision_id": "pd_pos", "user_action": "clicked", "ts": "2026-05-19T12:00:02Z"},
        )

        mined = dataset_mod.hard_examples(window="365d", limit=20)
        assert any(row["candidate_id"] == "cand_neg" and row["example_type"] == "hard_negative" for row in mined["examples"])

        curation_mod.record(target_type="candidate", target_id="cand_neg", action="exclude", reason="test")
        mined = dataset_mod.hard_examples(window="365d", limit=20)
        assert not any(row["candidate_id"] == "cand_neg" for row in mined["examples"])
    finally:
        store_mod.HARNESS_DIR = old_dir


def test_implicit_endpoint_returns_joined_examples(tmp_path):
    from aiohttp.test_utils import make_mocked_request

    old_dir = store_mod.HARNESS_DIR
    store_mod.HARNESS_DIR = tmp_path
    try:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        decision = {
            "decision_id": "pd_endpoint",
            "candidate_id": "cand_endpoint",
            "ts": ts,
            "policy_version": "rule_v0",
            "action": "notch_ping",
            "intent": "goal_aware",
            "reason_codes": ["reading_browser", "goal_aligned_help"],
        }
        outcome = {
            "decision_id": "pd_endpoint",
            "user_action": "timed_out",
            "latency_from_display_ms": 8000,
            "interaction_summary": {
                "intent_signal": "rejection_considered",
                "considered_targets": ["dismiss"],
                "total_hover_ms_by_target": {"dismiss": 620},
            },
            "ts": ts,
            "reward": {"version": "v2", "value": -0.8},
        }
        trace = {
            "trace_id": "tr_endpoint",
            "ts": ts,
            "state": {
                "candidate": {
                    "screen": {
                        "frontmost_app": "Chrome",
                        "ocr_snippet": "secret token should not surface",
                    },
                    "scene": {"label": "reading_browser", "source": "rule"},
                }
            },
            "action": {
                **decision,
                "why_now": "stalled on reading",
            },
            "realization": {
                "message": "Return to the draft or close this tab.",
                "vision_used": True,
            },
        }
        store_mod.append_jsonl("decisions.jsonl", decision)
        store_mod.append_jsonl("outcomes.jsonl", outcome)
        store_mod.append_jsonl("traces.jsonl", trace)

        req = make_mocked_request(
            "GET",
            "/implicit?window=365d&limit=5&direction=negative",
        )
        resp = asyncio.run(server_mod.get_implicit(req))
        body = json.loads(resp.text)
        assert body["summary"]["usable"] == 1
        assert body["examples"][0]["label"] == "would_annoy"
        assert body["examples"][0]["outcome"]["hover_ms_by_target"]["dismiss"] == 620
        assert body["examples"][0]["context"]["message"] == "Return to the draft or close this tab."
        assert "ocr_snippet" not in json.dumps(body["examples"][0])
        assert "secret token" not in json.dumps(body["examples"][0])
    finally:
        store_mod.HARNESS_DIR = old_dir


def test_implicit_promote_endpoint_appends_retro_label(tmp_path):
    from aiohttp.test_utils import make_mocked_request

    old_dir = store_mod.HARNESS_DIR
    store_mod.HARNESS_DIR = tmp_path
    try:
        store_mod.append_jsonl(
            "decisions.jsonl",
            {
                "decision_id": "pd_promote",
                "candidate_id": "cand_promote",
                "ts": "2026-05-19T12:00:00Z",
                "action": "notch_ping",
            },
        )
        req = make_mocked_request("POST", "/implicit/promote")
        req._read_bytes = json.dumps({
            "decision_id": "pd_promote",
            "label": "would_annoy",
            "implicit_label": "would_help",
            "implicit_direction": "positive",
        }).encode()
        resp = asyncio.run(server_mod.post_implicit_promote(req))
        body = json.loads(resp.text)
        assert body["ok"] is True
        rows = store_mod.tail_jsonl("retro_labels.jsonl")
        assert rows[-1]["label_id"] == "implicit_panel_pd_promote"
        assert rows[-1]["label"] == "would_annoy"
        assert rows[-1]["source"] == "implicit_examples_panel"
    finally:
        store_mod.HARNESS_DIR = old_dir


def test_information_diet_report_synthesizes_research_hypotheses(tmp_path):
    from aiohttp.test_utils import make_mocked_request

    old_dir = store_mod.HARNESS_DIR
    store_mod.HARNESS_DIR = tmp_path
    try:
        rows = [
            ("cand_diet_1", "2026-05-19T12:00:00Z", "Google Chrome", "screenpipe github.com/screenpipe screen capture search"),
            ("cand_diet_2", "2026-05-19T12:00:30Z", "Google Chrome", "docs.screenpi.pe home local screen audio capture"),
            ("cand_diet_3", "2026-05-19T12:01:00Z", "Google Chrome", "openadapt.ai evals github.com/OpenAdaptAI/OpenAdapt"),
        ]
        for cid, ts, app, ocr in rows:
            store_mod.append_jsonl(
                "candidates.jsonl",
                {
                    "candidate_id": cid,
                    "ts": ts,
                    "screen": {
                        "frontmost_app": app,
                        "ocr_snippet": ocr,
                        "frame_age_sec": 3,
                        "sensitive_scene": False,
                    },
                    "scene": {"label": "reading_browser", "strength": "medium", "source": "rule"},
                    "context": {},
                    "user_pref": {},
                },
            )

        report = information_diet_mod.build_report(window="365d")
        assert report["summary"]["n_research_events"] == 3
        assert report["summary"]["n_episodes"] == 1
        assert "source_triage" in report["summary"]["workflow_patterns"]
        assert report["skill_hypotheses"]
        serialized = json.dumps(report)
        assert "screenpipe github.com" not in serialized

        req = make_mocked_request("GET", "/information-diet/report?window=365d")
        resp = asyncio.run(server_mod.get_information_diet(req))
        body = json.loads(resp.text)
        assert body["version"] == information_diet_mod.REPORT_VERSION
    finally:
        store_mod.HARNESS_DIR = old_dir


def test_eval_report_builds_failure_taxonomy_and_sanitized_examples(tmp_path):
    from aiohttp.test_utils import make_mocked_request

    old_dir = store_mod.HARNESS_DIR
    store_mod.HARNESS_DIR = tmp_path
    try:
        rows = [
            ("pd_false", "cand_false", "notch_ping", "would_annoy"),
            ("pd_missed", "cand_missed", "no_ping", "would_help"),
            ("pd_soft", "cand_soft", "notch_ping", None),
        ]
        for i, (did, cid, action, label) in enumerate(rows):
            ts = f"2026-05-19T12:0{i}:00Z"
            store_mod.append_jsonl(
                "candidates.jsonl",
                {
                    "candidate_id": cid,
                    "ts": ts,
                    "screen": {
                        "frontmost_app": "Chrome",
                        "ocr_snippet": "OPENROUTER_API_KEY=or-v1-secret-should-not-appear",
                        "frame_age_sec": 3,
                    },
                    "scene": {"label": "reading_browser", "source": "rule"},
                    "context": {},
                    "user_pref": {},
                },
            )
            store_mod.append_jsonl(
                "decisions.jsonl",
                {
                    "decision_id": did,
                    "candidate_id": cid,
                    "ts": ts,
                    "action": action,
                    "intent": "goal_aware" if action == "notch_ping" else None,
                    "policy_version": "rule_v0",
                    "reason_codes": ["reading_browser"],
                },
            )
            store_mod.append_jsonl(
                "traces.jsonl",
                {
                    "trace_id": f"tr_{did}",
                    "ts": ts,
                    "state": {
                        "candidate": {
                            "candidate_id": cid,
                            "screen": {
                                "frontmost_app": "Chrome",
                                "ocr_snippet": "OPENROUTER_API_KEY=or-v1-secret-should-not-appear",
                            },
                            "scene": {"label": "reading_browser", "source": "rule"},
                        }
                    },
                    "action": {"decision_id": did, "why_now": "reading stall"},
                    "realization": {"message": "Return to the draft.", "vision_used": True},
                },
            )
            if label:
                store_mod.append_jsonl(
                    "retro_labels.jsonl",
                    {
                        "decision_id": did,
                        "candidate_id": cid,
                        "label": label,
                        "confidence": 1.0,
                        "ts": "2026-05-19T12:10:00Z",
                    },
                )

        store_mod.append_jsonl(
            "outcomes.jsonl",
            {
                "decision_id": "pd_soft",
                "user_action": "timed_out",
                "interaction_summary": {
                    "intent_signal": "rejection_considered",
                    "considered_targets": ["dismiss"],
                },
                "ts": "2026-05-19T12:10:30Z",
                "reward": {"version": "v2", "value": -0.8},
            },
        )

        report = eval_report_mod.build_report(window="365d", max_examples=10)
        assert report["data"]["n_decisions"] == 3
        taxonomy = {row["type"]: row["n"] for row in report["taxonomy"]["by_type"]}
        assert taxonomy["false_interruption"] == 1
        assert taxonomy["missed_help"] == 1
        assert taxonomy["soft_rejection"] == 1
        serialized = json.dumps(report)
        assert "or-v1-secret" not in serialized
        assert "Return to the draft." in serialized

        req = make_mocked_request("GET", "/eval/report?window=365d&max_examples=2")
        resp = asyncio.run(server_mod.get_eval_report(req))
        body = json.loads(resp.text)
        assert body["version"] == eval_report_mod.REPORT_VERSION
        assert len(body["examples"]) <= 2
    finally:
        store_mod.HARNESS_DIR = old_dir


def test_eval_report_distinguishes_queued_from_claimed_missing_outcomes(tmp_path):
    old_dir = store_mod.HARNESS_DIR
    store_mod.HARNESS_DIR = tmp_path
    try:
        for did, trace_delivery in [
            ("pd_queued", {"pushed": True, "channel": "notch_pill"}),
            ("pd_claimed_missing", {"pushed": True, "channel": "notch_pill"}),
            ("pd_blocked", {"pushed": False, "channel": "blocked_by_critic"}),
        ]:
            store_mod.append_jsonl(
                "decisions.jsonl",
                {
                    "decision_id": did,
                    "candidate_id": f"cand_{did}",
                    "ts": "2026-05-19T12:00:00Z",
                    "action": "notch_ping",
                    "intent": "goal_aware",
                },
            )
            store_mod.append_jsonl(
                "traces.jsonl",
                {
                    "trace_id": f"tr_{did}",
                    "ts": "2026-05-19T12:00:00Z",
                    "state": {"candidate": {"candidate_id": f"cand_{did}"}},
                    "action": {"decision_id": did, "action": "notch_ping"},
                    "delivery": trace_delivery,
                },
            )
        store_mod.append_jsonl(
            "deliveries.jsonl",
            {
                "delivery_id": "del_pd_claimed_missing_1",
                "decision_id": "pd_claimed_missing",
                "candidate_id": "cand_pd_claimed_missing",
                "delivery_action": "claimed",
                "channel": "notch_pill",
                "pending_attempts": 1,
                "ts": "2026-05-19T12:00:01Z",
            },
        )

        report = eval_report_mod.build_report(window="365d", max_examples=10)
        taxonomy = {row["type"]: row["n"] for row in report["taxonomy"]["by_type"]}
        assert taxonomy["queued_not_claimed"] == 1
        assert taxonomy["missing_outcome_signal"] == 1
        assert taxonomy["undelivered_ping"] == 1
        assert report["data"]["n_claimed_pings"] == 1
        assert report["data"]["outcome_capture_rate_for_claimed_pings"] == 0.0
    finally:
        store_mod.HARNESS_DIR = old_dir


def test_eval_report_distinguishes_trace_gap_from_claimed_missing_outcome(tmp_path):
    old_dir = store_mod.HARNESS_DIR
    store_mod.HARNESS_DIR = tmp_path
    try:
        for did in ["pd_trace_gap", "pd_claimed_missing"]:
            store_mod.append_jsonl(
                "decisions.jsonl",
                {
                    "decision_id": did,
                    "candidate_id": f"cand_{did}",
                    "ts": "2026-05-19T12:00:00Z",
                    "action": "notch_ping",
                    "intent": "goal_aware",
                },
            )
        store_mod.append_jsonl(
            "traces.jsonl",
            {
                "trace_id": "tr_pd_claimed_missing",
                "ts": "2026-05-19T12:00:00Z",
                "state": {"candidate": {"candidate_id": "cand_pd_claimed_missing"}},
                "action": {"decision_id": "pd_claimed_missing", "action": "notch_ping"},
                "delivery": {"pushed": True, "channel": "notch_pill"},
            },
        )
        store_mod.append_jsonl(
            "deliveries.jsonl",
            {
                "delivery_id": "del_pd_claimed_missing_1",
                "decision_id": "pd_claimed_missing",
                "candidate_id": "cand_pd_claimed_missing",
                "delivery_action": "claimed",
                "channel": "notch_pill",
                "pending_attempts": 1,
                "ts": "2026-05-19T12:00:01Z",
            },
        )

        report = eval_report_mod.build_report(window="365d", max_examples=10)
        taxonomy = {row["type"]: row["n"] for row in report["taxonomy"]["by_type"]}
        assert taxonomy["trace_gap_before_delivery"] == 1
        assert taxonomy["missing_outcome_signal"] == 1
    finally:
        store_mod.HARNESS_DIR = old_dir


def test_eval_report_event_labels_join_to_decision_window(tmp_path):
    old_dir = store_mod.HARNESS_DIR
    store_mod.HARNESS_DIR = tmp_path
    try:
        store_mod.append_jsonl("decisions.jsonl", {
            "decision_id": "pd_recent_event",
            "candidate_id": "cand_recent_event",
            "workflow_event_id": "wev_recent",
            "ts": "2026-05-19T12:00:00Z",
            "action": "no_ping",
        })
        store_mod.append_jsonl("workflow_events.jsonl", {
            "workflow_event_id": "wev_recent",
            "ts": "2026-05-19T12:00:00Z",
            "last_ts": "2026-05-19T12:00:00Z",
            "app": "Chrome",
        })
        store_mod.append_jsonl("workflow_events.jsonl", {
            "workflow_event_id": "wev_missed_without_decision",
            "ts": "2026-05-19T12:06:00Z",
            "last_ts": "2026-05-19T12:06:00Z",
            "app": "Chrome",
        })
        store_mod.append_jsonl("workflow_events.jsonl", {
            "workflow_event_id": "wev_out_of_window",
            "ts": "2024-05-19T12:06:00Z",
            "last_ts": "2024-05-19T12:06:00Z",
            "app": "Chrome",
        })
        store_mod.append_jsonl("retro_labels.jsonl", {
            "label_id": "lab_old_event",
            "workflow_event_id": "wev_old",
            "label_scope": "workflow_event",
            "label": "would_help",
            "ts": "2024-05-19T12:05:00Z",
        })
        store_mod.append_jsonl("retro_labels.jsonl", {
            "label_id": "lab_recent_event",
            "workflow_event_id": "wev_recent",
            "label_scope": "workflow_event",
            "label": "would_help",
            "ts": "2026-05-19T12:05:00Z",
        })
        store_mod.append_jsonl("retro_labels.jsonl", {
            "label_id": "lab_missed_event",
            "workflow_event_id": "wev_missed_without_decision",
            "label_scope": "workflow_event",
            "label": "would_help",
            "ts": "2026-05-19T12:06:00Z",
        })
        store_mod.append_jsonl("retro_labels.jsonl", {
            "label_id": "lab_out_of_window_event",
            "workflow_event_id": "wev_out_of_window",
            "label_scope": "workflow_event",
            "label": "would_help",
            "ts": "2026-05-19T12:07:00Z",
        })
        report = eval_report_mod.build_report(window="365d")
        assert report["data"]["n_event_labels"] == 2
    finally:
        store_mod.HARNESS_DIR = old_dir


def test_trainer_blocks_canary_from_implicit_signal_without_label_rigor(tmp_path):
    old_dir = store_mod.HARNESS_DIR
    store_mod.HARNESS_DIR = tmp_path
    try:
        for i in range(3):
            cid = f"cand_train_{i}"
            did = f"pd_train_{i}"
            store_mod.append_jsonl(
                "candidates.jsonl",
                {
                    "candidate_id": cid,
                    "ts": f"2026-05-19T12:0{i}:00Z",
                    "screen": {
                        "frontmost_app": "Chrome",
                        "ocr_snippet": "TODO ship harness",
                        "frame_age_sec": 5,
                    },
                    "scene": {
                        "label": "coding_with_todo_in_view",
                        "strength": "strong",
                        "source": "rule",
                    },
                    "context": {},
                    "user_pref": {},
                },
            )
            store_mod.append_jsonl(
                "decisions.jsonl",
                {
                    "decision_id": did,
                    "candidate_id": cid,
                    "ts": f"2026-05-19T12:0{i}:01Z",
                    "action": "notch_ping",
                    "reason_codes": ["coding_with_todo_in_view"],
                },
            )
            store_mod.append_jsonl(
                "outcomes.jsonl",
                {
                    "decision_id": did,
                    "user_action": "clicked",
                    "interaction_summary": {"intent_signal": "committed"},
                    "ts": f"2026-05-19T12:0{i}:02Z",
                    "reward": {"version": "v2", "value": 2.0},
                },
            )

        default_result = trainer_mod.run_trainer(window="365d", min_implicit_usable=2, write=False)
        assert default_result["canary_policy"]["status"] == "insufficient_data"

        result = trainer_mod.run_trainer(
            window="365d",
            min_implicit_usable=2,
            min_explicit_labels=0,
        )
        canary = result["canary_policy"]
        assert canary["status"] == "insufficient_data"
        assert canary["variant"] is None
        assert result["calibration"]["readiness"]["comparison_status"] == "insufficient_explicit_labels"
        state = store_mod.read_policy_state()
        assert state["canary_policy"]["status"] == "insufficient_data"
    finally:
        store_mod.HARNESS_DIR = old_dir


def test_shadow_eval_compares_policy_variants_against_labels(tmp_path):
    candidates_path = tmp_path / "candidates.jsonl"
    labels_path = tmp_path / "retro_labels.jsonl"
    outcomes_path = tmp_path / "outcomes.jsonl"
    candidates = [
        {
            "candidate_id": "cand_should_ping",
            "workflow_event_id": "wev_shadow_ping",
            "ts": "2026-05-19T12:00:00Z",
            "screen": {"frame_age_sec": 5, "ocr_snippet": "TODO ship harness"},
            "scene": {"label": "coding_with_todo_in_view", "strength": "strong", "source": "rule"},
            "context": {},
            "user_pref": {},
        },
        {
            "candidate_id": "cand_should_stay_quiet",
            "workflow_event_id": "wev_shadow_quiet",
            "ts": "2026-05-19T12:05:00Z",
            "screen": {"frame_age_sec": 5, "ocr_snippet": ""},
            "scene": {"label": "unknown", "strength": "unknown", "source": "unknown"},
            "context": {},
            "user_pref": {},
        },
        {
            "candidate_id": "cand_old_labeled",
            "ts": "2026-05-01T12:05:00Z",
            "screen": {"frame_age_sec": 5, "ocr_snippet": "old"},
            "scene": {"label": "unknown", "strength": "unknown", "source": "unknown"},
            "context": {},
            "user_pref": {},
        },
    ]
    labels = [
        {"candidate_id": "cand_should_ping", "label": "would_annoy", "ts": "2026-05-19T12:00:30Z"},
        {"candidate_id": "cand_should_ping", "label": "would_help", "ts": "2026-05-19T12:10:00Z"},
        {
            "candidate_id": "cand_should_stay_quiet",
            "label": "should_not_ping",
            "ts": "2026-05-19T12:11:00Z",
        },
        {
            "candidate_id": "cand_old_labeled",
            "label": "would_help",
            "ts": "2026-05-01T12:11:00Z",
        },
        {
            "workflow_event_id": "wev_shadow_ping",
            "label_scope": "workflow_event",
            "label": "should_ping",
            "ts": "2026-05-19T12:12:00Z",
        },
    ]
    candidates_path.write_text("\n".join(json.dumps(row) for row in candidates) + "\n")
    labels_path.write_text("\n".join(json.dumps(row) for row in labels) + "\n")
    outcomes_path.write_text("")

    report = shadow_eval_mod.compare(
        since="2026-05-19T11:00:00Z",
        dataset=str(candidates_path),
        labels_path=str(labels_path),
        outcomes_path=str(outcomes_path),
        variants={"current": {"quiet_hours_start": 3, "quiet_hours_end": 4}},
    )
    assert report["n_candidates"] == 2
    assert report["n_labeled_candidates"] == 2
    assert report["n_labeled_events"] == 1
    assert report["best_by_labeled_f1"] == "current"
    current = report["variants"][0]
    assert current["labels"]["n"] == 2
    assert current["labels"]["agreement_rate"] == 1.0
    assert current["labels"]["tp_should_ping_and_pinged"] == 1
    assert current["labels"]["tn_should_stay_quiet_and_silent"] == 1
    assert current["event_labels"]["n"] == 1
    assert current["event_labels"]["tp_should_ping_and_pinged"] == 1
    assert report["label_support"]["n_units"] == 2
    assert report["label_support"]["positive_units"] == 1
    assert report["label_support"]["negative_units"] == 1


def test_shadow_eval_holdout_is_temporal_and_replays_prior_context(tmp_path):
    candidates_path = tmp_path / "candidates.jsonl"
    labels_path = tmp_path / "retro_labels.jsonl"
    outcomes_path = tmp_path / "outcomes.jsonl"
    candidates = []
    labels = []
    for i in range(5):
        cid = f"cand_temporal_{i}"
        candidates.append({
            "candidate_id": cid,
            "workflow_event_id": f"wev_temporal_{i}",
            "ts": f"2026-05-19T12:0{i}:00Z",
            "screen": {"frame_age_sec": 5, "ocr_snippet": "TODO ship harness" if i == 4 else "read notes"},
            "scene": {
                "label": "coding_with_todo_in_view" if i == 4 else "reading_browser",
                "strength": "strong",
                "source": "rule",
            },
            "context": {},
            "user_pref": {},
        })
        labels.append({
            "candidate_id": cid,
            "label": "would_help" if i == 4 else "good_no_ping",
            "ts": f"2026-05-19T12:1{i}:00Z",
        })
    candidates_path.write_text("\n".join(json.dumps(row) for row in candidates) + "\n")
    labels_path.write_text("\n".join(json.dumps(row) for row in labels) + "\n")
    outcomes_path.write_text("")

    report = shadow_eval_mod.compare(
        since="2026-05-19T11:00:00Z",
        dataset=str(candidates_path),
        labels_path=str(labels_path),
        outcomes_path=str(outcomes_path),
        variants={"current": {}},
        holdout_only=True,
        holdout_fraction=0.2,
    )
    protocol = report["holdout_protocol"]
    assert protocol["method"] == "time_ordered_group_holdout"
    assert protocol["strict_temporal"] is True
    assert protocol["train_end_ts"] < protocol["start_ts"]
    assert report["n_candidates"] == 1
    assert report["n_replay_candidates"] == 5
    assert report["n_labeled_candidates"] == 1
    assert report["variants"][0]["labels"]["n"] == 1


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
    assert summary["dominant_hover_target"] == "dismiss"


def test_interaction_summary_uses_dominant_hover_target():
    summary = server_mod._summarize_interactions([
        {"t_ms": 100, "kind": "hover_start", "target": "dismiss"},
        {"t_ms": 200, "kind": "hover_end", "target": "dismiss"},
        {"t_ms": 250, "kind": "hover_start", "target": "later"},
        {"t_ms": 1250, "kind": "hover_end", "target": "later"},
    ])
    assert summary["dominant_hover_target"] == "later"
    assert summary["intent_signal"] == "snooze_considered"
