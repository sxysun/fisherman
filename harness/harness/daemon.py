from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional

import structlog
from aiohttp import web

from . import critic as critic_mod
from . import experiments as experiments_mod
from . import gate as gate_mod
from . import push as push_mod
from . import realizer as realizer_mod
from . import reward as reward_mod
from . import scene as scene_mod
from . import scene_vlm as scene_vlm_mod
from . import trainer as trainer_mod
from .candidate import synthesize
from .fisherman_client import FishermanClient
from .memory import SessionMemory
from .schemas import (
    CandidateEvent,
    Reward,
    Trace,
    UserPref,
)
from .server import build_app
from .store import (
    attach_outcome_to_trace,
    append_jsonl,
    ensure_dirs,
    read_policy_state,
    tail_jsonl,
    write_policy_state,
)


log = structlog.get_logger("harness.daemon")


NOTCH_BINARY = Path(os.path.expanduser("~/.harness/HarnessNotch"))


def _launch_notch(harness_port: int) -> Optional[subprocess.Popen]:
    if not NOTCH_BINARY.exists():
        log.warning("notch_binary_missing", path=str(NOTCH_BINARY), note="run harness build-notch")
        return None
    env = os.environ.copy()
    env["HARNESS_URL"] = f"http://127.0.0.1:{harness_port}"
    try:
        proc = subprocess.Popen(
            [str(NOTCH_BINARY)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log.info("notch_launched", pid=proc.pid)
        return proc
    except Exception as e:
        log.warning("notch_launch_failed", error=str(e))
        return None


def _stop_notch(proc: Optional[subprocess.Popen]) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
    except Exception:
        pass


def _now_unix() -> float:
    return time.time()


def _user_pref_from_config(config: dict) -> UserPref:
    state = read_policy_state()
    return UserPref(
        frequency=config.get("gate", {}).get("frequency", "medium"),
        allowed_intents=config.get("intents", {}).get("enabled", []),
        quiet_hours=(
            int(config.get("gate", {}).get("quiet_hours_start", 22)),
            int(config.get("gate", {}).get("quiet_hours_end", 8)),
        ),
        snoozed_until=state.get("snoozed_until"),
        muted_intents=state.get("muted_intents", []),
    )


def _compute_reward(outcome_action: Optional[str], reward_cfg: dict) -> Reward:
    weights = reward_cfg.get("weights", {})
    components: dict[str, float] = {}
    value = 0.0
    if outcome_action == "clicked":
        components["welcomed"] = weights.get("welcomed", 3.0)
        value += components["welcomed"]
    elif outcome_action == "dismissed":
        components["annoying"] = weights.get("annoying", -5.0)
        value += components["annoying"]
    elif outcome_action == "timed_out":
        components["duplicate"] = weights.get("duplicate", -1.0) / 3.0
        value += components["duplicate"]
    return Reward(version=reward_cfg.get("version", "v1"), value=value, components=components)


async def run_loop(config: dict) -> None:
    ensure_dirs()
    fisherman_url = config["daemon"]["fisherman_url"]
    poll_sec = float(config["daemon"]["poll_interval_sec"])
    harness_port = int(config["daemon"]["http_port"])
    push_channel = config.get("push", {}).get("channel", "notch_pill")

    fc = FishermanClient(fisherman_url)
    memory_cfg = config.get("memory", {})
    memory = SessionMemory(
        window_min=int(memory_cfg.get("session_window_min", 120)),
        idle_boundary_sec=int(memory_cfg.get("idle_boundary_sec", 90)),
        active_frame_max_age_sec=int(memory_cfg.get("active_frame_max_age_sec", 60)),
    )

    last_push_at_ref: list[Optional[float]] = [None]

    app = build_app(fisherman_url=fisherman_url)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", harness_port)
    await site.start()
    log.info("server_started", port=harness_port)

    notch_proc: Optional[subprocess.Popen] = None
    if push_channel == "notch_pill":
        notch_proc = _launch_notch(harness_port)

    if not await fc.is_alive():
        log.warning("fisherman_unreachable_at_start", url=fisherman_url)

    trainer_task = asyncio.create_task(_trainer_loop(config))
    try:
        while True:
            if push_channel == "notch_pill" and (notch_proc is None or notch_proc.poll() is not None):
                if notch_proc is not None:
                    log.warning("notch_exited", returncode=notch_proc.returncode)
                notch_proc = _launch_notch(harness_port)
            await _tick(
                config=config,
                fc=fc,
                memory=memory,
                last_push_at_ref=last_push_at_ref,
            )
            await asyncio.sleep(poll_sec)
    finally:
        trainer_task.cancel()
        try:
            await trainer_task
        except asyncio.CancelledError:
            pass
        _stop_notch(notch_proc)
        await runner.cleanup()


async def _trainer_loop(config: dict) -> None:
    cfg = config.get("trainer") or {}
    if not bool(cfg.get("enabled", True)):
        return
    interval_hours = float(cfg.get("interval_hours", 24))
    window = str(cfg.get("window", "30d"))
    min_implicit = int(cfg.get("min_implicit_usable", 20))
    min_explicit = int(cfg.get("min_explicit_labels", 0))
    initial_delay_sec = float(cfg.get("initial_delay_sec", 60))
    await asyncio.sleep(max(0.0, initial_delay_sec))
    while True:
        try:
            result = trainer_mod.run_trainer(
                window=window,
                min_implicit_usable=min_implicit,
                min_explicit_labels=min_explicit,
                write=True,
            )
            log.info(
                "trainer_run",
                status=(result.get("canary_policy") or {}).get("status"),
                variant=(result.get("canary_policy") or {}).get("variant"),
            )
        except Exception as e:
            log.warning("trainer_failed", error=str(e))
        await asyncio.sleep(max(1.0, interval_hours * 3600.0))


async def _tick(
    *,
    config: dict,
    fc: FishermanClient,
    memory: SessionMemory,
    last_push_at_ref: list[Optional[float]],
) -> None:
    user_pref = _user_pref_from_config(config)

    minutes_since_last_push = (
        9999.0
        if last_push_at_ref[0] is None
        else (_now_unix() - last_push_at_ref[0]) / 60.0
    )

    event = await synthesize(fc, user_pref=user_pref, minutes_since_last_push=minutes_since_last_push)
    if event is None:
        log.info("no_candidate", reason="fisherman_unreachable")
        return

    event.scene = scene_mod.tag(event, memory.recent_apps())

    # Optional VLM scene-tagger pass. Cheap heuristics inside maybe_tag()
    # decide whether to actually fire the LLM call (cooldown + diff-gating).
    vlm_cfg = dict((config.get("scene_tagger") or {}).get("llm") or {})
    vlm_cfg["privacy"] = config.get("privacy", {})
    if vlm_cfg.get("enabled"):
        try:
            vlm_result = await scene_vlm_mod.maybe_tag(event, fc, vlm_cfg)
        except Exception as e:
            log.warning("scene_vlm_failed", error=str(e))
            vlm_result = None
        if vlm_result:
            scene_vlm_mod.overlay_on_event(event, vlm_result)
            log.info(
                "scene_vlm",
                label=event.scene.label,
                specificity=vlm_result.get("specificity"),
                sensitive=vlm_result.get("sensitive"),
            )

    append_jsonl("candidates.jsonl", event.to_dict())

    memory.record(event)
    recent_outcomes = tail_jsonl("outcomes.jsonl", n=5)
    mem_snap = memory.snapshot(recent_outcomes)

    policy_state = read_policy_state()
    daily_goal = (policy_state.get("daily_goal") or "").strip()
    sensitivity = policy_state.get("sensitivity") or "balanced"
    policy_name, policy_overrides, policy_metadata = trainer_mod.active_policy_config(config, policy_state)
    gate_cfg = {
        "cooldown_min": config["gate"]["cooldown_min"],
        "negative_feedback_backoff_min": config["gate"].get("negative_feedback_backoff_min", 15),
        "resume_suppression_sec": config["gate"].get("resume_suppression_sec", 90),
        "quiet_hours_start": config["gate"]["quiet_hours_start"],
        "quiet_hours_end": config["gate"]["quiet_hours_end"],
        "allowed_intents": config["intents"]["enabled"],
        "daily_goal": daily_goal,
        "sensitivity": sensitivity,
        "policy_learner": config.get("policy_learner", {}),
        "privacy": config.get("privacy", {}),
    }
    gate_cfg.update(policy_overrides)
    decision = gate_mod.decide(
        policy_name,
        event,
        mem_snap,
        recent_outcomes,
        gate_cfg,
    )
    if policy_metadata.get("active_policy") == "canary":
        decision.policy_version = f"{decision.policy_version}+canary"
    decision = experiments_mod.apply(decision, event, config.get("experiment", {}))
    if policy_metadata.get("active_policy") == "canary":
        exp = dict(decision.experiment or {})
        exp["policy_canary"] = policy_metadata
        decision.experiment = exp
    append_jsonl("decisions.jsonl", decision.to_dict() | {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})

    trace = Trace.new(event, mem_snap, recent_outcomes)
    trace.action = decision.to_dict()

    if decision.action == "no_ping":
        log.info("decision", action="no_ping", reasons=decision.reason_codes)
        append_jsonl("traces.jsonl", trace.to_dict())
        return

    # ping path
    log.info("decision", action="notch_ping", intent=decision.intent, reasons=decision.reason_codes)
    realization = await realizer_mod.realize(
        intent=decision.intent or "goal_aware",
        event=event,
        memory=mem_snap,
        fisherman=fc,
        config=dict(config["realizer"]) | {"privacy": config.get("privacy", {})},
        daily_goal=daily_goal,
        why_now=(getattr(decision, "why_now", None) or ", ".join(decision.reason_codes)),
    )
    trace.realization = realization.to_dict()

    if realization.error or not realization.message.strip():
        log.warning("realizer_failed", error=realization.error)
        trace.delivery = {"pushed": False, "channel": "skipped"}
        append_jsonl("traces.jsonl", trace.to_dict())
        return

    critic_cfg = dict(config.get("critic", {}))
    critic_cfg["privacy"] = config.get("privacy", {})
    critic_result = await critic_mod.check(realization.message, event, critic_cfg)
    trace.critic = critic_result.to_dict()

    if not critic_result.pass_:
        log.warning("critic_blocked", flags=critic_result.flags, reasons=critic_result.reasons)
        outcome = {
            "decision_id": decision.decision_id,
            "user_action": "blocked",
            "latency_from_display_ms": 0,
            "explicit_feedback": "critic",
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "flags": critic_result.flags,
        }
        reward = reward_mod.compute_reward(outcome)
        outcome["reward"] = reward
        trace.delivery = {"pushed": False, "channel": "blocked_by_critic"}
        trace.outcome = outcome
        trace.reward = reward
        append_jsonl("outcomes.jsonl", outcome)
        append_jsonl("traces.jsonl", trace.to_dict())
        attach_outcome_to_trace(decision.decision_id, outcome, reward)
        return

    push_cfg = dict(config.get("push", {}))
    push_cfg["harness_port"] = config["daemon"]["http_port"]
    delivery = await push_mod.dispatch(decision, realization, push_cfg)
    trace.delivery = delivery.__dict__
    last_push_at_ref[0] = _now_unix()
    append_jsonl("traces.jsonl", trace.to_dict())
