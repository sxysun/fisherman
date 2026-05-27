from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from . import information_diet
from . import metrics as metrics_mod
from . import model_audit
from . import privacy
from . import sql_store
from . import trust
from .realizer import chat_completions_url
from .store import append_jsonl, read_policy_state, write_policy_state


INTERVIEW_VERSION = "goal_interview_v1"
FIRST_QUESTION = "What should the harness steer you toward today, and what should it pull you away from?"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def run(
    *,
    turns: list[dict[str, Any]],
    config: dict[str, Any],
    apply_goal: bool = False,
) -> dict[str, Any]:
    """Run one morning-goal interview step.

    The endpoint is intentionally stateless from the UI's perspective: the UI
    sends all turns so far, and this function either asks one more question or
    proposes a concrete daily steering goal. Each call is appended for audit.
    """
    clean_turns = _clean_turns(turns)
    if not clean_turns:
        result = {
            "version": INTERVIEW_VERSION,
            "status": "needs_more_input",
            "question": FIRST_QUESTION,
            "source": "bootstrap",
        }
        _record(clean_turns, result, applied=False)
        return result

    interview_cfg = config.get("goal_interview") or {}
    if interview_cfg.get("enabled") is False:
        result = _fallback_result(clean_turns, source="disabled")
    elif interview_cfg.get("use_model") is True:
        context = _recent_context()
        try:
            result = _model_result(clean_turns, config, context)
        except Exception as e:
            result = _fallback_result(clean_turns, error=str(e))
    else:
        result = _fallback_result(clean_turns)

    if result.get("status") == "proposal":
        goal = _bounded_text(result.get("proposed_goal"), 240)
        if not goal:
            result = _fallback_result(clean_turns, error="empty_model_goal")
        else:
            result["proposed_goal"] = goal
            result["sensitivity"] = _normalize_sensitivity(result.get("sensitivity"))

    applied = False
    if apply_goal and result.get("status") == "proposal":
        state = read_policy_state()
        state["daily_goal"] = result["proposed_goal"]
        state["sensitivity"] = result.get("sensitivity") or state.get("sensitivity", "balanced")
        state["goal_set_at"] = _now_iso()
        state["goal_source"] = "morning_interview"
        write_policy_state(state)
        result["applied"] = True
        applied = True

    _record(clean_turns, result, applied=applied)
    return result


def _clean_turns(turns: list[dict[str, Any]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for turn in turns[-8:]:
        if not isinstance(turn, dict):
            continue
        question = _bounded_text(turn.get("question"), 240)
        answer = _bounded_text(turn.get("answer"), 1000)
        if answer:
            out.append({
                "question": privacy.redact_text(question or FIRST_QUESTION),
                "answer": privacy.redact_text(answer),
            })
    return out


def _recent_context() -> dict[str, Any]:
    context: dict[str, Any] = {}
    try:
        diet = information_diet.build_report(window="24h", max_episodes=4)
        context["information_diet"] = {
            "summary": diet.get("summary") or {},
            "recent_episodes": [
                {
                    "task_hypothesis": ep.get("task_hypothesis"),
                    "top_terms": ep.get("top_terms"),
                    "source_domains": ep.get("source_domains"),
                    "workflow_patterns": ep.get("workflow_patterns"),
                    "observed_duration_min": ep.get("observed_duration_min"),
                }
                for ep in (diet.get("episodes") or [])[:4]
            ],
            "skill_hypotheses": [
                {
                    "topic": row.get("topic"),
                    "hypothesis": row.get("hypothesis"),
                    "confidence": row.get("confidence"),
                }
                for row in (diet.get("skill_hypotheses") or [])[:4]
            ],
        }
    except Exception as e:
        context["information_diet_error"] = str(e)[:160]
    context["harness_metrics"] = _fast_metrics_summary()
    state = read_policy_state()
    context["current_goal"] = state.get("daily_goal") or ""
    context["current_sensitivity"] = state.get("sensitivity") or "balanced"
    return context


def _model_result(
    turns: list[dict[str, str]],
    config: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    model_cfg = _model_config(config)
    base_url = str(model_cfg.get("base_url") or "").rstrip("/")
    model = str(model_cfg.get("model") or "")
    if not base_url or not model:
        raise RuntimeError("model_unconfigured")
    trust_check = trust.check_model_endpoint(base_url, config.get("privacy") or {})
    if not trust_check.allowed:
        raise RuntimeError(f"model_endpoint_blocked:{trust_check.reason}")

    messages = _messages(turns, context)
    started = time.time()
    status = "ok"
    error = None
    try:
        result = _call_model(model_cfg, base_url, model, messages)
    except Exception as e:
        status = "error"
        error = str(e)
        raise
    finally:
        model_audit.record_model_call(
            purpose="goal_interview",
            base_url=base_url,
            endpoint=chat_completions_url(base_url),
            model=model,
            status=status,
            prompt_version=INTERVIEW_VERSION,
            latency_ms=int((time.time() - started) * 1000),
            error=error,
            tokens_in=_estimate_tokens(messages),
            extra={"turns": len(turns)},
        )

    status_value = str(result.get("status") or "").strip()
    if status_value == "needs_more_input":
        question = _bounded_text(result.get("question"), 240)
        if not question:
            question = _fallback_question(turns)
        return {
            "version": INTERVIEW_VERSION,
            "status": "needs_more_input",
            "question": question,
            "source": "model",
        }
    return {
        "version": INTERVIEW_VERSION,
        "status": "proposal",
        "proposed_goal": _bounded_text(result.get("proposed_goal"), 240),
        "sensitivity": _normalize_sensitivity(result.get("sensitivity")),
        "rationale": _bounded_text(result.get("rationale"), 360),
        "steering_checks": [
            _bounded_text(item, 120)
            for item in (result.get("steering_checks") or [])
            if _bounded_text(item, 120)
        ][:4],
        "source": "model",
    }


def _model_config(config: dict[str, Any]) -> dict[str, Any]:
    learner = dict(config.get("policy_learner") or {})
    if learner.get("base_url") and learner.get("model"):
        out = learner
    else:
        out = dict(config.get("realizer") or {})
    interview_cfg = config.get("goal_interview") or {}
    if interview_cfg.get("timeout_sec") is not None:
        out["timeout_sec"] = interview_cfg.get("timeout_sec")
    return out


def _messages(turns: list[dict[str, str]], context: dict[str, Any]) -> list[dict[str, str]]:
    compact_context = json.dumps(context, ensure_ascii=True, sort_keys=True, default=str)[:6000]
    compact_turns = json.dumps(turns, ensure_ascii=True, sort_keys=True, default=str)[:5000]
    system = (
        "You run the morning steering interview for a local proactive presence harness. "
        "Your job is to convert the user's priorities into one concrete daily_goal used by "
        "a binary ping/not-ping policy. Preserve attention: the goal should make the harness "
        "interrupt only when it helps the user make progress. If the user's answers are too vague, "
        "ask exactly one sharper follow-up question. Otherwise return a proposal. "
        "Return JSON only."
    )
    user = (
        "Recent local context, already redacted/summarized:\n"
        f"{compact_context}\n\n"
        "Interview turns so far:\n"
        f"{compact_turns}\n\n"
        "Return one of these JSON objects:\n"
        '{"status":"needs_more_input","question":"one pointed question"}\n'
        '{"status":"proposal","proposed_goal":"one terse operational daily goal","sensitivity":"gentle|balanced|responsive","rationale":"why this goal follows","steering_checks":["what a good ping would notice"]}'
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _call_model(cfg: dict[str, Any], base_url: str, model: str, messages: list[dict[str, str]]) -> dict[str, Any]:
    api_key = cfg.get("api_key") or os.environ.get(str(cfg.get("api_key_env") or ""), "")
    body = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": min(int(cfg.get("max_tokens") or 220), 320),
    }
    req = urllib.request.Request(
        chat_completions_url(base_url),
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("x-api-key", api_key)
    timeout = max(1.0, min(float(cfg.get("timeout_sec") or 2), 2.0))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"http_{e.code}") from e
    content = (((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.startswith("json"):
            content = content[4:].strip()
    return _parse_json_object(content)


def _parse_json_object(content: str) -> dict[str, Any]:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(content[start:end + 1])
    if not isinstance(parsed, dict):
        raise RuntimeError("model_returned_non_object")
    return parsed


def _fallback_result(
    turns: list[dict[str, str]],
    *,
    error: str = "",
    source: str = "heuristic",
) -> dict[str, Any]:
    answer = " ".join(turn.get("answer", "") for turn in turns).strip()
    if len(answer) < 32:
        return {
            "version": INTERVIEW_VERSION,
            "status": "needs_more_input",
            "question": _fallback_question(turns),
            "source": source,
            "error": error[:160] or None,
        }
    goal = answer.replace("\n", " ")
    goal = " ".join(goal.split())
    if not goal.lower().startswith(("ship", "finish", "make", "debug", "write", "review", "learn", "decide", "build")):
        goal = f"make concrete progress on {goal}"
    return {
        "version": INTERVIEW_VERSION,
        "status": "proposal",
        "proposed_goal": _bounded_text(goal, 220),
        "sensitivity": _heuristic_sensitivity(answer),
        "rationale": (
            "Synthesized locally from the interview answer."
            if not error
            else "Synthesized locally because the model endpoint was unavailable."
        ),
        "steering_checks": [
            "visible drift from the stated work",
            "open loops that block the stated work",
        ],
        "source": source,
        "error": error[:160] or None,
    }


def _fast_metrics_summary() -> dict[str, Any]:
    since = metrics_mod.since_iso("24h")
    try:
        action_counts = sql_store.value_counts("decisions", "action", since_iso=since, limit=12)
        return {
            "n_decisions": sql_store.count_payload_rows("decisions", since_iso=since),
            "n_pings": int(action_counts.get("notch_ping") or 0),
            "n_outcomes": sql_store.count_payload_rows("outcomes", since_iso=since),
            "n_labels": sql_store.count_payload_rows("retro_labels", since_iso=since),
        }
    except Exception as e:
        return {"error": str(e)[:160]}


def _fallback_question(turns: list[dict[str, str]]) -> str:
    if len(turns) == 1:
        return "What would count as a useful interruption today, and what would be noise?"
    return "What is the concrete artifact or decision you want to have by the end of today?"


def _heuristic_sensitivity(text: str) -> str:
    lowered = text.lower()
    if any(word in lowered for word in ("urgent", "ship", "finish", "debug", "deadline", "today")):
        return "responsive"
    if any(word in lowered for word in ("read", "think", "explore", "recover", "rest")):
        return "gentle"
    return "balanced"


def _normalize_sensitivity(value: Any) -> str:
    raw = str(value or "balanced").lower()
    return raw if raw in {"gentle", "balanced", "responsive"} else "balanced"


def _bounded_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _estimate_tokens(messages: list[dict[str, str]]) -> int:
    return max(1, sum(len(str(msg.get("content") or "")) for msg in messages) // 4)


def _record(turns: list[dict[str, str]], result: dict[str, Any], *, applied: bool) -> None:
    append_jsonl("goal_interviews.jsonl", {
        "interview_id": f"goalint_{int(time.time() * 1000)}",
        "version": INTERVIEW_VERSION,
        "ts": _now_iso(),
        "turns": turns,
        "result": result,
        "applied": applied,
    })
