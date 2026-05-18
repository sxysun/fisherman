from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import click
import structlog

from . import config as config_mod
from . import critic as critic_mod
from . import daemon as daemon_mod
from . import push as push_mod
from . import realizer as realizer_mod
from .candidate import synthesize
from .fisherman_client import FishermanClient
from .schemas import (
    CandidateEvent,
    ContextSignals,
    MemorySnapshot,
    ProactiveDecision,
    ScreenContext,
    SceneTag,
    UserPref,
)
from .store import (
    HARNESS_DIR,
    ensure_dirs,
    read_policy_state,
    tail_jsonl,
    write_policy_state,
)


NOTCH_DIR = Path(__file__).resolve().parent.parent / "notch"
HARNESS_REPO = Path(__file__).resolve().parent.parent
DATASETS_DIR = HARNESS_REPO / "datasets"
REPORTS_DIR = HARNESS_REPO / "reports"


def _configure_logging(level: str = "info") -> None:
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(__import__("logging"), level.upper(), 20)
        ),
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(colors=True),
        ],
    )


@click.group()
def main() -> None:
    """Fisherman harness — proactive presence over screen context."""
    _configure_logging()


@main.command()
@click.option("--force", is_flag=True, default=False, help="Overwrite ~/.harness/config.toml with current defaults.")
@click.option("--build-notch/--no-build-notch", default=True, help="Build the Swift notch app and install it.")
def install(force: bool, build_notch: bool) -> None:
    """Create ~/.harness/, write default config, init empty state, build notch app."""
    ensure_dirs()
    cfg_path = config_mod.write_default(force=force)
    state = read_policy_state()
    if "active_policy" not in state:
        state["active_policy"] = "rule_v0"
    if "muted_intents" not in state:
        state["muted_intents"] = []
    state.setdefault("snoozed_until", None)
    write_policy_state(state)
    click.echo(f"config: {cfg_path}{'  (kept existing — pass --force to overwrite)' if (cfg_path.exists() and not force) else ''}")
    click.echo(f"state:  {HARNESS_DIR}")
    if build_notch:
        click.echo("")
        _build_notch()
    click.echo("")
    click.echo("Next:  harness start --foreground")


@main.command("build-notch")
def build_notch_cmd() -> None:
    """Build the Swift notch app (HarnessNotch) and install it to ~/.harness/."""
    _build_notch()


def _build_notch() -> None:
    build_script = NOTCH_DIR / "build.sh"
    if not build_script.exists():
        raise click.ClickException(f"missing {build_script}")
    click.echo(f"[notch] building from {NOTCH_DIR}")
    try:
        subprocess.run([str(build_script)], check=True)
    except subprocess.CalledProcessError as e:
        raise click.ClickException(f"swift build failed (exit {e.returncode})")
    click.echo("[notch] ok")


@main.command()
@click.option("--foreground/--no-foreground", default=True, help="Run in current shell (default).")
def start(foreground: bool) -> None:
    """Start the harness daemon."""
    try:
        cfg = config_mod.load()
    except FileNotFoundError as e:
        raise click.ClickException(str(e))

    if not foreground:
        raise click.ClickException(
            "daemonization not implemented yet — use --foreground (the default).\n"
            "Run in tmux/screen or under launchd if you need it backgrounded."
        )

    click.echo(f"harness daemon: {cfg['daemon']['fisherman_url']} → :{cfg['daemon']['http_port']}")
    click.echo(f"policy: {cfg['gate']['active_policy']}   intents: {', '.join(cfg['intents']['enabled'])}")
    click.echo("ctrl-c to stop.")
    click.echo("")

    try:
        asyncio.run(daemon_mod.run_loop(cfg))
    except KeyboardInterrupt:
        click.echo("\nstopped.")


@main.command()
def status() -> None:
    """Show daemon state, last decision, snooze/mute settings."""
    state = read_policy_state()
    decisions = tail_jsonl("decisions.jsonl", n=1)
    outcomes = tail_jsonl("outcomes.jsonl", n=1)
    click.echo(f"policy: {state.get('active_policy', '(not set)')}")
    click.echo(f"snoozed_until: {state.get('snoozed_until')}")
    click.echo(f"muted_intents: {state.get('muted_intents', [])}")
    if decisions:
        d = decisions[-1]
        click.echo(f"last decision: {d.get('action')} intent={d.get('intent')} reasons={d.get('reason_codes')}")
    else:
        click.echo("last decision: (none)")
    if outcomes:
        o = outcomes[-1]
        click.echo(f"last outcome:  {o.get('user_action')} for {o.get('decision_id')}")
    else:
        click.echo("last outcome:  (none)")


@main.command()
@click.option("--intent", default="focus_nudge", help="Intent to realize. Default: focus_nudge.")
@click.option("--push/--no-push", default=False, help="Also drop into the notch via /pending (daemon must be running).")
@click.option("--message", default=None, help="Override the candidate's OCR snippet (useful for prompt iteration).")
@click.option("--app", default=None, help="Override the candidate's frontmost_app.")
def test(intent: str, push: bool, message: Optional[str], app: Optional[str]) -> None:
    """Force a realizer + critic call for the given intent. Skips the gate.

    Synthesizes a candidate from Fisherman if reachable (else stubs one), runs
    the realizer against the configured LLM, prints the message + critic
    verdict. With --push, also writes to ~/.harness/pending/<id>.json so a
    running daemon's notch app surfaces it.
    """
    try:
        cfg = config_mod.load()
    except FileNotFoundError as e:
        raise click.ClickException(str(e))

    if intent not in cfg["intents"]["enabled"]:
        click.echo(f"warning: intent {intent!r} not in enabled intents {cfg['intents']['enabled']}", err=True)

    async def _run() -> None:
        fc = FishermanClient(cfg["daemon"]["fisherman_url"])
        user_pref = UserPref(allowed_intents=cfg["intents"]["enabled"])
        event = await synthesize(fc, user_pref=user_pref, minutes_since_last_push=999.0)
        if event is None:
            click.echo("(fisherman unreachable — using stub candidate)")
            event = CandidateEvent(
                screen=ScreenContext(
                    active=True,
                    frontmost_app=app or "(stub)",
                    ocr_snippet=message or "stubbed test ocr",
                    frame_age_sec=0.0,
                ),
                scene=SceneTag(label="testing", strength="strong", source="rule"),
                context=ContextSignals(),
                user_pref=user_pref,
            )
        if message is not None:
            event.screen.ocr_snippet = message
        if app is not None:
            event.screen.frontmost_app = app
        mem = MemorySnapshot.build(
            recent_apps=[event.screen.frontmost_app or "?"] * 3,
            recent_scenes=[],
            recent_outcomes=[],
            app_switches_last_15m=0,
            minutes_on_current_app=0.0,
        )

        click.echo(f"intent:    {intent}")
        click.echo(f"app:       {event.screen.frontmost_app}")
        click.echo(f"ocr:       {(event.screen.ocr_snippet or '')[:140]}")
        click.echo(f"realizer:  {cfg['realizer']['base_url']}  model={cfg['realizer']['model']}")
        click.echo("")
        click.echo("calling realizer...")

        r = await realizer_mod.realize(
            intent=intent, event=event, memory=mem, fisherman=fc, config=cfg["realizer"]
        )
        click.echo(f"  latency:  {r.latency_ms} ms")
        click.echo(f"  tokens:   in={r.tokens_in} out={r.tokens_out}")
        click.echo(f"  prompt:   {r.prompt_version}")
        if r.vision_used:
            click.echo(f"  vision:   ✓ image attached ({r.image_bytes // 1024} KB JPEG)")
        else:
            click.echo("  vision:   (no image attached)")
        if r.tool_calls:
            click.echo(f"  tools:    {len(r.tool_calls)} call(s)")
            for tc in r.tool_calls:
                click.echo(f"    - {tc.name}({tc.arguments}) → {tc.result_summary}")
        if r.error:
            click.echo("")
            raise click.ClickException(f"realizer error: {r.error}")
        click.echo("")
        click.echo(f"  message:  {r.message!r}")

        click.echo("")
        click.echo("running critic...")
        crit = await critic_mod.check(r.message, event, cfg.get("critic", {}))
        verdict = "PASS" if crit.pass_ else "BLOCK"
        click.echo(f"  {verdict}")
        if crit.flags:
            click.echo(f"  flags:   {crit.flags}")
        if crit.reasons:
            click.echo(f"  reasons: {crit.reasons}")

        if push:
            if not crit.pass_:
                click.echo("")
                click.echo("not pushing (critic blocked).", err=True)
                return
            decision = ProactiveDecision(
                decision_id=f"pd_test_{int(time.time())}",
                candidate_id=event.candidate_id,
                policy_version="manual_test",
                action="notch_ping",
                intent=intent,
                reason_codes=["manual_test"],
                confidence=1.0,
                propensity=1.0,
            )
            push_cfg = dict(cfg.get("push", {}))
            push_cfg["harness_port"] = cfg["daemon"]["http_port"]
            delivery = await push_mod.dispatch(decision, r, push_cfg)
            click.echo("")
            click.echo(f"pushed: {delivery.pushed}  channel={delivery.channel}")
            click.echo("(if daemon isn't running, the pending file will sit until one starts)")

    asyncio.run(_run())


@main.command()
@click.option("--since", default="24h", help="Window: 24h, 7d, etc.")
@click.option("--out", default=None, help="Destination .jsonl. Default: datasets/dogfood/<today>.jsonl")
def collect(since: str, out: Optional[str]) -> None:
    """Freeze current candidates.jsonl (filtered by --since) into a dataset."""
    since_iso = _resolve_since(since)
    src = HARNESS_DIR / "candidates.jsonl"
    if not src.exists():
        raise click.ClickException(f"{src} not found — has the daemon ever run?")
    if out is None:
        date = time.strftime("%Y-%m-%d", time.gmtime())
        out = str(DATASETS_DIR / "dogfood" / f"{date}.jsonl")
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n_total = 0
    n_kept = 0
    with open(src) as fin, open(out_path, "w") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            n_total += 1
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since_iso and row.get("ts", "") < since_iso:
                continue
            fout.write(line + "\n")
            n_kept += 1
    click.echo(f"collected {n_kept}/{n_total} candidates → {out_path}")


@main.command()
@click.option("--policy", required=True, help="Policy module name in policies/ (e.g., rule_v0).")
@click.option("--dataset", default=None, help="Path to candidate jsonl. Default: live candidates.jsonl.")
@click.option("--since", default=None, help="Filter by duration like 24h or 7d.")
@click.option("--out", default=None, help="Output predictions JSON. Default: reports/<policy>_<ts>.json")
def replay(policy: str, dataset: Optional[str], since: Optional[str], out: Optional[str]) -> None:
    """Shadow-replay a policy against frozen candidates (no LLM, no push)."""
    dataset = dataset or str(HARNESS_DIR / "candidates.jsonl")
    if out is None:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        out = str(REPORTS_DIR / f"{policy}_{ts}.json")
    cmd = [
        sys.executable, "-m", "eval.replay",
        "--policy", policy, "--dataset", dataset, "--out", out,
    ]
    if since:
        cmd += ["--since", since]
    try:
        subprocess.run(cmd, check=True, cwd=str(HARNESS_REPO))
    except subprocess.CalledProcessError as e:
        raise click.ClickException(f"replay failed (exit {e.returncode})")
    click.echo(f"predictions: {out}")


@main.command()
@click.option("--predictions", required=True, help="predictions JSON from `harness replay`.")
@click.option("--out", default=None, help="Output report JSON. Stdout if omitted.")
def score(predictions: str, out: Optional[str]) -> None:
    """Score predictions against live outcomes + retro labels."""
    cmd = [sys.executable, "-m", "eval.score", "--predictions", predictions]
    if out:
        cmd += ["--out", out]
    try:
        subprocess.run(cmd, check=True, cwd=str(HARNESS_REPO))
    except subprocess.CalledProcessError as e:
        raise click.ClickException(f"score failed (exit {e.returncode})")


@main.command()
def label() -> None:
    """Open the retro labeling UI (web) in your default browser. Daemon must be running."""
    try:
        cfg = config_mod.load()
    except FileNotFoundError as e:
        raise click.ClickException(str(e))
    port = int(cfg["daemon"]["http_port"])
    url = f"http://localhost:{port}/label"
    click.echo(f"opening {url}")
    click.echo("(daemon must be running — start with `harness start --foreground` if not)")
    try:
        subprocess.run(["open", url], check=False, timeout=3)
    except Exception:
        click.echo(f"could not auto-open — visit {url} manually")


@main.command()
def dashboard() -> None:
    """Open the settings + diagnostics dashboard in your default browser."""
    try:
        cfg = config_mod.load()
    except FileNotFoundError as e:
        raise click.ClickException(str(e))
    port = int(cfg["daemon"]["http_port"])
    url = f"http://localhost:{port}/dashboard"
    click.echo(f"opening {url}")
    try:
        subprocess.run(["open", url], check=False, timeout=3)
    except Exception:
        click.echo(f"could not auto-open — visit {url} manually")


@main.command()
def stop() -> None:
    """Stop a running daemon (by port :7893) and its notch app."""
    daemon_killed = False
    try:
        result = subprocess.run(
            ["lsof", "-t", "-i", ":7893"], capture_output=True, text=True, timeout=2
        )
        pids = [int(p) for p in result.stdout.split() if p.strip().isdigit()]
    except Exception:
        pids = []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            click.echo(f"sent SIGTERM to daemon pid {pid}")
            daemon_killed = True
        except ProcessLookupError:
            pass
    if not daemon_killed:
        click.echo("no daemon on :7893")

    try:
        subprocess.run(["pkill", "-f", "HarnessNotch"], check=False, timeout=2)
        click.echo("killed any HarnessNotch processes")
    except Exception:
        pass


@main.command()
@click.option("--since", default=None, help="ISO timestamp or duration like '1h', '24h', '7d'.")
@click.option("--action", default=None, help="Filter by action (no_ping / notch_ping).")
@click.option("--intent", default=None, help="Filter by intent.")
@click.option("-n", "--limit", default=20, help="Max rows.")
def inspect(since: Optional[str], action: Optional[str], intent: Optional[str], limit: int) -> None:
    """Walk recent decisions with reason_codes."""
    since_iso = _resolve_since(since)
    rows = tail_jsonl("decisions.jsonl", n=None)
    if since_iso:
        rows = [r for r in rows if r.get("ts", "") >= since_iso]
    if action:
        rows = [r for r in rows if r.get("action") == action]
    if intent:
        rows = [r for r in rows if r.get("intent") == intent]
    rows = rows[-limit:]
    for r in rows:
        ts = r.get("ts", "(no ts)")
        act = r.get("action", "?")
        it = r.get("intent") or "-"
        rc = r.get("reason_codes", [])
        click.echo(f"{ts}  {act:11s}  intent={it:24s}  reasons={rc}")
    click.echo(f"({len(rows)} rows)")


@main.command()
@click.argument("duration", type=str)
def snooze(duration: str) -> None:
    """Snooze pings for a duration: '30m', '2h', '1d'."""
    seconds = _duration_seconds(duration)
    until = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + seconds))
    state = read_policy_state()
    state["snoozed_until"] = until
    write_policy_state(state)
    click.echo(f"snoozed until {until}")


@main.command()
def unsnooze() -> None:
    """Clear snooze."""
    state = read_policy_state()
    state["snoozed_until"] = None
    write_policy_state(state)
    click.echo("snooze cleared.")


@main.command()
@click.argument("intent", type=str)
def mute(intent: str) -> None:
    """Mute an intent permanently (until `harness unmute`)."""
    state = read_policy_state()
    muted = set(state.get("muted_intents", []))
    muted.add(intent)
    state["muted_intents"] = sorted(muted)
    write_policy_state(state)
    click.echo(f"muted: {state['muted_intents']}")


@main.command()
@click.option("--all", "all_", is_flag=True, default=False)
@click.argument("intent", required=False)
def unmute(intent: Optional[str], all_: bool) -> None:
    state = read_policy_state()
    if all_:
        state["muted_intents"] = []
    elif intent:
        state["muted_intents"] = [m for m in state.get("muted_intents", []) if m != intent]
    write_policy_state(state)
    click.echo(f"muted: {state['muted_intents']}")


def _resolve_since(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    if s.endswith(("s", "m", "h", "d")):
        secs = _duration_seconds(s)
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - secs))
    return s


def _duration_seconds(s: str) -> int:
    try:
        n = int(s[:-1])
    except ValueError:
        n = 1
    unit = s[-1]
    return {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(unit, 60) * n


if __name__ == "__main__":
    main()
