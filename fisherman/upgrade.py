"""Self-upgrade machinery for fisherman.

Goal: one command — `fisherman upgrade` — that always Just Works,
whether the user installed via curl|sh from main, cloned the repo,
or is running a dev checkout. Safe by default: backs up the prior
code, never touches user data, polls the daemon for health after
relaunch, rolls back automatically if the new version doesn't come
up.

Source modes (--from selector):
  - default            git fetch + reset to origin/<current branch>
                       inside ~/.fisherman/.git
  - --from-local PATH  rsync from a working tree (developer flow)
  - --from-branch X    git fetch + reset to origin/X

What gets synced (only these — never user data):
  fisherman/                 (Python source incl. data/)
  pyproject.toml
  uv.lock
  menubar/   (only if the source has changed since last install,
              and only if `swift` is on PATH)

What is NEVER touched:
  ~/.fisherman/.env          (FISH_PRIVATE_KEY, friends, deputies)
  ~/.fisherman/frames/       (real captures)
  ~/.fisherman/audio/
  ~/.fisherman/screenpipe-data/
  ~/.fisherman/logs/
  ~/.fisherman/.git/         (the install's git history is preserved)

Backups go to ~/.fisherman/.backup/<utc-timestamp>/. Last 3 are
kept; older ones are pruned automatically.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_INSTALL_DIR = Path.home() / ".fisherman"
BACKUP_DIRNAME = ".backup"
KEEP_BACKUPS = 3
DEFAULT_CONTROL_PORT = 7892


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VERSION_STAMP = ".fisherman-version"


@dataclass(slots=True)
class InstalledVersion:
    """What's currently sitting in ~/.fisherman/."""
    install_dir:  Path
    has_git:      bool
    git_commit:   Optional[str]   # short sha
    git_branch:   Optional[str]
    git_subject:  Optional[str]   # one-line subject of the installed commit
    installed_at: Optional[str]   # ISO8601 timestamp from the stamp file
    source_kind:  Optional[str]   # "git" | "local" | None
    has_venv:     bool
    has_app:      bool            # /Applications/Fisherman.app exists


@dataclass(slots=True)
class SourceVersion:
    """Where new code is coming from."""
    source_dir:  Path             # directory containing fisherman/, pyproject.toml, uv.lock
    git_commit:  Optional[str]
    git_branch:  Optional[str]
    git_subject: Optional[str]
    is_local_dev: bool            # True for --from-local (no commit context if dirty)


def detect_installed(install_dir: Path = DEFAULT_INSTALL_DIR) -> InstalledVersion:
    has_git = (install_dir / ".git").is_dir()

    # Prefer the stamp file written by `fisherman upgrade` — it tracks
    # what was actually synced into the install dir, independent of
    # whatever ~/.fisherman/.git happens to be at. Falls back to git
    # for fresh installs that predate the stamp.
    stamp = _read_version_stamp(install_dir)
    if stamp is not None:
        return InstalledVersion(
            install_dir=install_dir, has_git=has_git,
            git_commit=stamp.get("commit"), git_branch=stamp.get("branch"),
            git_subject=stamp.get("subject"),
            installed_at=stamp.get("installed_at"),
            source_kind=stamp.get("source"),
            has_venv=(install_dir / ".venv" / "bin" / "fisherman").exists(),
            has_app=Path("/Applications/Fisherman.app").exists(),
        )

    commit = branch = subject = None
    if has_git:
        commit  = _git("rev-parse --short HEAD", cwd=install_dir)
        branch  = _git("rev-parse --abbrev-ref HEAD", cwd=install_dir)
        subject = _git("log -1 --pretty=%s", cwd=install_dir)
    return InstalledVersion(
        install_dir=install_dir,
        has_git=has_git,
        git_commit=commit, git_branch=branch, git_subject=subject,
        installed_at=None, source_kind=None,
        has_venv=(install_dir / ".venv" / "bin" / "fisherman").exists(),
        has_app=Path("/Applications/Fisherman.app").exists(),
    )


def _read_version_stamp(install_dir: Path) -> Optional[dict]:
    p = install_dir / VERSION_STAMP
    if not p.is_file():
        return None
    try:
        import json as _json
        return _json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def write_version_stamp(install_dir: Path, src: "SourceVersion") -> None:
    import json as _json
    p = install_dir / VERSION_STAMP
    p.write_text(_json.dumps({
        "commit":   src.git_commit,
        "branch":   src.git_branch,
        "subject":  src.git_subject,
        "source":   "local" if src.is_local_dev else "git",
        "installed_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }, indent=2) + "\n")


def detect_source_local(path: Path) -> SourceVersion:
    path = path.resolve()
    if not (path / "fisherman").is_dir() or not (path / "pyproject.toml").is_file():
        raise ValueError(
            f"--from-local {path} doesn't look like a fisherman checkout "
            f"(no fisherman/ dir + pyproject.toml)"
        )
    has_git = (path / ".git").is_dir()
    commit = branch = subject = None
    if has_git:
        commit  = _git("rev-parse --short HEAD", cwd=path)
        branch  = _git("rev-parse --abbrev-ref HEAD", cwd=path)
        subject = _git("log -1 --pretty=%s", cwd=path)
        dirty = _git("status --porcelain", cwd=path)
        if dirty:
            commit = (commit or "") + "-dirty"
    return SourceVersion(
        source_dir=path, git_commit=commit, git_branch=branch,
        git_subject=subject, is_local_dev=True,
    )


def fetch_source_from_git(
    install_dir: Path, branch: Optional[str] = None
) -> SourceVersion:
    """Fetch from origin and report what `origin/<branch>` would point
    at, WITHOUT mutating the working tree. The actual `git reset --hard`
    happens later via `apply_git_source` — splitting these lets the
    caller back up the OLD code first.
    """
    if not (install_dir / ".git").is_dir():
        raise RuntimeError(
            f"{install_dir} has no .git/ — can't fetch updates. "
            f"Re-run install.sh to bootstrap a fresh checkout, or pass "
            f"--from-local <path>."
        )
    _git("fetch --quiet origin", cwd=install_dir, check=True)
    target_branch = (
        branch or _git("rev-parse --abbrev-ref HEAD", cwd=install_dir) or "main"
    )
    new_full = _git(f"rev-parse origin/{target_branch}", cwd=install_dir)
    new_short = _git(f"rev-parse --short origin/{target_branch}", cwd=install_dir)
    new_subject = _git(f"log -1 --pretty=%s {new_full}", cwd=install_dir)
    return SourceVersion(
        source_dir=install_dir,
        git_commit=new_short,
        git_branch=target_branch,
        git_subject=new_subject,
        is_local_dev=False,
    )


def apply_git_source(install_dir: Path, src: "SourceVersion") -> str:
    """`git reset --hard` to the source's commit. Returns the previous
    HEAD's full SHA (for git-based rollback)."""
    prev = _git("rev-parse HEAD", cwd=install_dir)
    _git(
        f"reset --quiet --hard {src.git_commit}",
        cwd=install_dir, check=True,
    )
    return prev or ""


def detect_source_in_install(install_dir: Path) -> SourceVersion:
    return SourceVersion(
        source_dir=install_dir,
        git_commit=_git("rev-parse --short HEAD", cwd=install_dir),
        git_branch=_git("rev-parse --abbrev-ref HEAD", cwd=install_dir),
        git_subject=_git("log -1 --pretty=%s", cwd=install_dir),
        is_local_dev=False,
    )


def commits_between(install_dir: Path, src: SourceVersion,
                    installed: InstalledVersion, max_lines: int = 20) -> list[str]:
    """One-line summaries of commits between installed and source.
    Empty if we can't reason about it (e.g. --from-local with dirty
    tree, or different repos)."""
    if not (src.git_commit and installed.git_commit
            and (installed.install_dir / ".git").is_dir()
            and (src.source_dir / ".git").is_dir()):
        return []
    # When source != install dir, we can only diff if both are the
    # same repo (same .git/objects). Easier: use src's git, asking for
    # commits since installed_commit.
    out = _git(
        f"log --pretty=%h\\ %s {installed.git_commit}..{src.git_commit}",
        cwd=src.source_dir,
    )
    if not out:
        return []
    return out.splitlines()[:max_lines]


# ---------------------------------------------------------------------------
# Backup + sync
# ---------------------------------------------------------------------------

def make_backup(install_dir: Path) -> Path:
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    bdir = install_dir / BACKUP_DIRNAME / ts
    bdir.mkdir(parents=True, exist_ok=True)
    for item in ("fisherman", "pyproject.toml", "uv.lock"):
        src = install_dir / item
        if src.exists():
            dst = bdir / item
            if src.is_dir():
                shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy2(src, dst)
    return bdir


def prune_backups(install_dir: Path, keep: int = KEEP_BACKUPS) -> int:
    base = install_dir / BACKUP_DIRNAME
    if not base.is_dir():
        return 0
    snaps = sorted(p for p in base.iterdir() if p.is_dir())
    to_drop = snaps[:-keep] if len(snaps) > keep else []
    for p in to_drop:
        shutil.rmtree(p, ignore_errors=True)
    return len(to_drop)


def restore_backup(install_dir: Path, backup_dir: Path) -> None:
    for item in ("fisherman", "pyproject.toml", "uv.lock"):
        src = backup_dir / item
        dst = install_dir / item
        if not src.exists():
            continue
        if dst.exists():
            if dst.is_dir():
                shutil.rmtree(dst)
            else:
                dst.unlink()
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)


def sync_python_code(src: Path, install_dir: Path) -> dict:
    """Rsync the Python source tree + lockfiles into the install dir.

    Returns a small report {"files_changed": int, "menubar_changed": bool}.

    When `src == install_dir` (the git path: `fisherman upgrade` did
    `git fetch + reset --hard` in place, so the working tree IS already
    the new code), the file-copy steps are no-ops — rsync of a dir to
    itself works but `shutil.copy2(same, same)` raises SameFileError.
    """
    same = (src.resolve() == install_dir.resolve())

    if same:
        files_changed = 0
    else:
        cmd = [
            "rsync", "-a", "--delete",
            "--exclude=__pycache__",
            "--exclude=.pytest_cache",
            "--out-format=%n",
            f"{src}/fisherman/", f"{install_dir}/fisherman/",
        ]
        out = subprocess.run(cmd, capture_output=True, text=True, check=True)
        files_changed = len([ln for ln in out.stdout.splitlines() if ln.strip()])

        # pyproject.toml + uv.lock — required for `uv sync` to pick up new deps.
        for f in ("pyproject.toml", "uv.lock"):
            s = src / f
            d = install_dir / f
            if s.exists() and s.resolve() != d.resolve():
                shutil.copy2(s, d)

    # Detect whether menubar source changed since last install. We hash
    # Sources/, Package.swift, AND Packages/ (manual SwiftPM deps like
    # DynamicNotchKit) — missing Packages would make `swift build` fail
    # mid-upgrade if we didn't catch it here. When src == install_dir
    # (git path), the working tree IS the new source so there's nothing
    # to compare; `--force-menubar` is the escape hatch when a stale
    # /Applications app needs a forced rebuild.
    src_mb = src / "menubar"
    inst_mb = install_dir / "menubar"
    menubar_changed = False
    if src_mb.is_dir():
        if not inst_mb.is_dir():
            menubar_changed = True
        elif not same:
            menubar_changed = (
                _hash_tree(src_mb / "Sources") != _hash_tree(inst_mb / "Sources")
                or _hash_tree(src_mb / "Package.swift") != _hash_tree(inst_mb / "Package.swift")
                or _hash_tree(src_mb / "Packages") != _hash_tree(inst_mb / "Packages")
                or _hash_tree(src_mb / "Info.plist") != _hash_tree(inst_mb / "Info.plist")
            )
        if menubar_changed and not same:
            subprocess.run([
                "rsync", "-a", "--delete",
                "--exclude=.build",
                f"{src_mb}/", f"{inst_mb}/",
            ], check=True)

    return {"files_changed": files_changed, "menubar_changed": menubar_changed}


# ---------------------------------------------------------------------------
# uv sync + menubar build + /Applications swap
# ---------------------------------------------------------------------------

def find_uv() -> str:
    for p in (Path.home() / ".local/bin/uv",
              Path("/usr/local/bin/uv"),
              Path("/opt/homebrew/bin/uv")):
        if p.exists():
            return str(p)
    found = shutil.which("uv")
    if found:
        return found
    raise RuntimeError("uv not found on PATH; install with `curl -LsSf https://astral.sh/uv/install.sh | sh`")


def uv_sync(install_dir: Path, *, quiet: bool = True) -> None:
    uv = find_uv()
    args = [uv, "sync"]
    if quiet:
        args.append("--quiet")
    subprocess.run(args, cwd=str(install_dir), check=True)


def ensure_path_symlink(install_dir: Path) -> tuple[str, Optional[Path]]:
    """Make `fisherman` runnable from any shell.

    Strategy: symlink ~/.local/bin/fisherman → <install>/.venv/bin/fisherman
    if ~/.local/bin is on $PATH and we wouldn't be stomping a different
    file. Falls back gracefully — never fatal.

    Returns (status, link_path) where status is one of:
      "created"   — link did not exist, we made it
      "ok"        — link already points where we'd want it
      "skipped"   — ~/.local/bin not on PATH (we don't want to silently
                    create a link the user can't reach)
      "conflict"  — something else lives at the path; we left it alone
      "no-venv"   — no venv binary to link to
    """
    target = install_dir / ".venv" / "bin" / "fisherman"
    if not target.exists():
        return "no-venv", None

    candidate = Path.home() / ".local" / "bin" / "fisherman"
    path_dirs = os.environ.get("PATH", "").split(":")
    if str(candidate.parent) not in path_dirs:
        return "skipped", candidate

    candidate.parent.mkdir(parents=True, exist_ok=True)

    if candidate.is_symlink():
        try:
            existing = candidate.resolve(strict=False)
            if existing == target.resolve(strict=False):
                return "ok", candidate
        except OSError:
            pass
        # Stale or wrong symlink — replace it (it's our own link).
        candidate.unlink()
        candidate.symlink_to(target)
        return "created", candidate

    if candidate.exists():
        # Real file lives there — don't stomp user customization.
        return "conflict", candidate

    candidate.symlink_to(target)
    return "created", candidate


def have_swift() -> bool:
    return shutil.which("swift") is not None


def build_menubar_app(install_dir: Path) -> Path:
    """Build + sign the menubar Swift app. Returns path to the assembled .app."""
    if not have_swift():
        raise RuntimeError(
            "Swift toolchain not found — install Xcode Command Line Tools "
            "(`xcode-select --install`) before upgrading the menubar app."
        )
    mb = install_dir / "menubar"
    subprocess.run(["swift", "build", "-c", "release"], cwd=str(mb), check=True)

    identity = _signing_identity()
    sign_id = identity or "-"
    bin_path = mb / ".build/release/FishermanMenu"
    subprocess.run(["codesign", "--force", "--sign", sign_id, str(bin_path)], check=True)

    app = mb / ".build/Fisherman.app"
    if app.exists():
        shutil.rmtree(app)
    (app / "Contents/MacOS").mkdir(parents=True)
    (app / "Contents/Resources").mkdir(parents=True)
    shutil.copy2(bin_path, app / "Contents/MacOS/FishermanMenu")
    shutil.copy2(mb / "Info.plist", app / "Contents/Info.plist")
    icon = mb / "AppIcon.icns"
    if icon.exists():
        shutil.copy2(icon, app / "Contents/Resources/AppIcon.icns")

    # Strip xattrs (macOS 15 codesign hygiene).
    subprocess.run(["xattr", "-cr", str(app)], check=False)
    for pattern in ("._*", ".DS_Store"):
        for p in app.rglob(pattern):
            try:
                p.unlink()
            except OSError:
                pass
    subprocess.run(["codesign", "--force", "--deep", "--sign", sign_id, str(app)], check=True)
    return app


def install_app(app: Path) -> None:
    target = Path("/Applications/Fisherman.app")
    # Stop running menubar so the binary isn't busy. Wait for actual
    # exit — `pkill` is async and the bundle replace below races it.
    _kill_and_wait("FishermanMenu", timeout=5.0)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(app, target)
    subprocess.run(["xattr", "-cr", str(target)], check=False)


def _kill_and_wait(pattern: str, timeout: float = 5.0) -> None:
    """SIGTERM matching processes and wait until they're gone."""
    subprocess.run(["pkill", "-f", pattern], check=False)
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            return
        time.sleep(0.2)
    # Last resort
    subprocess.run(["pkill", "-9", "-f", pattern], check=False)
    time.sleep(0.5)


def menubar_running() -> bool:
    """True iff /Applications/Fisherman.app's binary is in the process list.

    `pgrep -x FishermanMenu` was flaky when invoked from a process the
    OS just forked (the in-kernel comm cache can be stale for a few
    hundred ms — the Diagnostics tab shells out from FishermanMenu and
    hit this every time). `pgrep -lf` (full command line, with command
    printed) lets us match against the actual .app binary path.

    NOTE: `-l` (lowercase L) prints the command, NOT `-a`. macOS BSD
    pgrep doesn't support `-a` — using it silently drops the cmdline
    output and we'd see only PIDs.
    """
    r = subprocess.run(
        ["pgrep", "-lf", "FishermanMenu"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return False
    for line in r.stdout.splitlines():
        # Format: "<pid> <full command line>"
        parts = line.strip().split(" ", 1)
        if len(parts) < 2:
            continue
        cmd = parts[1]
        if "/Fisherman.app/Contents/MacOS/FishermanMenu" in cmd:
            return True
        # Dev installs run from .build/ before the .app swap.
        if cmd.rstrip().endswith("/.build/release/FishermanMenu"):
            return True
    return False


def launch_app(retries: int = 3) -> bool:
    """Bring /Applications/Fisherman.app to a running state.

    Robust against:
      - `open` returning -600 (LaunchServices procNotFound — happens
        when the app was just pkill'd and the system hasn't fully
        released it). Sleep + retry, then fall back to direct binary.
      - The app already running (just no-op).
    """
    if menubar_running():
        return True

    target = "/Applications/Fisherman.app"
    if not Path(target).exists():
        return False

    for attempt in range(retries):
        r = subprocess.run(
            ["open", "-a", "Fisherman"],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            for _ in range(20):  # up to 10s for the menubar to register
                if menubar_running():
                    return True
                time.sleep(0.5)
        # -600 = LaunchServices procNotFound; usually a stale-process race
        time.sleep(2.0)

    # Fallback: launch the binary directly. Skips LaunchServices entirely.
    binary = Path(target) / "Contents" / "MacOS" / "FishermanMenu"
    if binary.exists():
        subprocess.Popen(
            [str(binary)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        for _ in range(20):
            if menubar_running():
                return True
            time.sleep(0.5)
    return False


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

def diagnose() -> dict:
    """Snapshot of every subsystem fisherman depends on.

    Returns a flat dict {check: {ok, detail}}. Used by `fisherman repair`
    and `fisherman doctor`."""
    out: dict = {}
    out["menubar"] = {
        "ok": menubar_running(),
        "detail": "FishermanMenu process found"
                  if menubar_running() else "FishermanMenu NOT running",
    }
    daemon = daemon_status()
    out["daemon"] = {
        "ok": daemon is not None,
        "detail": (f"control port up; frames_sent={daemon.get('frames_sent')}"
                   if daemon else "no response on 127.0.0.1:7892"),
    }
    sp_path = shutil.which("screenpipe")
    sp_detail = sp_path or "not on PATH (install from https://docs.screenpi.pe/)"
    if sp_path:
        # Surface the deprecated-brew-bottle situation so users plan
        # the migration before 2026-08-25 (when the formula is removed).
        try:
            r = subprocess.run(
                ["brew", "info", "--json", "screenpipe"],
                capture_output=True, text=True, timeout=4,
            )
            if r.returncode == 0 and '"deprecated":true' in r.stdout:
                sp_detail += "  ⚠ brew formula deprecated; disabled 2026-08-25"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    out["screenpipe_binary"] = {
        "ok": sp_path is not None,
        "detail": sp_detail,
    }
    sp_running = subprocess.run(
        ["pgrep", "-f", "screenpipe"], capture_output=True,
    ).returncode == 0
    out["screenpipe_process"] = {
        "ok": sp_running,
        "detail": "screenpipe process found"
                  if sp_running else "screenpipe NOT running (menubar should spawn it)",
    }
    sp_http = False
    try:
        with urllib.request.urlopen("http://127.0.0.1:3030/health", timeout=2) as r:
            sp_http = r.status == 200
    except (urllib.error.URLError, OSError):
        sp_http = False
    out["screenpipe_http"] = {
        "ok": sp_http,
        "detail": "127.0.0.1:3030 reachable" if sp_http else "127.0.0.1:3030 not reachable",
    }
    out["app_bundle"] = {
        "ok": Path("/Applications/Fisherman.app").exists(),
        "detail": "/Applications/Fisherman.app exists"
                  if Path("/Applications/Fisherman.app").exists()
                  else "/Applications/Fisherman.app MISSING",
    }
    # Screenpipe DB size — when it grows past a few hundred MB, /search
    # SQL latency exceeds the daemon's poll timeout and frames stop
    # flowing. We warn loudly so the user knows to bump
    # FISH_SCREENPIPE_SEARCH_TIMEOUT or trim the DB.
    sp_db = Path.home() / ".fisherman" / "screenpipe-data" / "db.sqlite"
    if sp_db.exists():
        size_mb = sp_db.stat().st_size / (1024 * 1024)
        if size_mb > 1000:
            out["screenpipe_db_size"] = {
                "ok": False,
                "detail": (f"{size_mb:.0f} MB — /search likely slow; "
                           f"raise FISH_SCREENPIPE_SEARCH_TIMEOUT or trim the DB"),
            }
        else:
            out["screenpipe_db_size"] = {
                "ok": True,
                "detail": f"{size_mb:.0f} MB",
            }
    return out


def repair() -> dict:
    """Try to bring everything back to a healthy state.

    Order matters: screenpipe binary must exist, then menubar must
    launch, then the menubar (re)spawns screenpipe + daemon.
    Returns the post-repair diagnose() snapshot.
    """
    # 1. Refresh LaunchServices registration for the .app — this clears
    #    "stale process" issues that cause `open` to return -600.
    lsregister = (
        "/System/Library/Frameworks/CoreServices.framework/Versions/A/"
        "Frameworks/LaunchServices.framework/Versions/A/Support/lsregister"
    )
    if Path(lsregister).exists():
        subprocess.run(
            [lsregister, "-f", "/Applications/Fisherman.app"],
            capture_output=True,
        )
    # 2. Flush zombies of menubar + screenpipe (menubar will respawn screenpipe).
    _kill_and_wait("FishermanMenu", timeout=5.0)
    _kill_and_wait("screenpipe.*fisherman", timeout=3.0)
    # 3. Bring the menubar back up (which spawns daemon + screenpipe).
    launch_app(retries=3)
    # 4. Give it a moment for screenpipe + daemon to come up.
    time.sleep(3.0)
    return diagnose()


# ---------------------------------------------------------------------------
# Daemon health-check
# ---------------------------------------------------------------------------

def daemon_status(port: int = DEFAULT_CONTROL_PORT,
                  timeout: float = 1.0) -> Optional[dict]:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/status", timeout=timeout
        ) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return None


def stop_daemon(port: int = DEFAULT_CONTROL_PORT) -> None:
    """Politely stop the daemon. Returns when the port is free."""
    try:
        out = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}"], capture_output=True, text=True,
        )
        for pid_str in out.stdout.strip().splitlines():
            try:
                os.kill(int(pid_str), signal.SIGTERM)
            except (ValueError, ProcessLookupError):
                pass
    except FileNotFoundError:
        return
    # Wait up to 5s for it to release.
    for _ in range(50):
        if daemon_status(port, timeout=0.2) is None:
            time.sleep(0.2)
            return
        time.sleep(0.1)


def wait_for_daemon(port: int = DEFAULT_CONTROL_PORT,
                    timeout: float = 15.0) -> Optional[dict]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = daemon_status(port, timeout=0.5)
        if s is not None:
            return s
        time.sleep(0.5)
    return None


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _git(args: str, cwd: Path, check: bool = False) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git"] + args.split(), cwd=str(cwd),
            capture_output=True, text=True, check=check,
        )
    except FileNotFoundError:
        return None
    if out.returncode != 0 and not check:
        return None
    return out.stdout.strip() or None


def _signing_identity() -> Optional[str]:
    out = subprocess.run(
        ["security", "find-identity", "-v", "-p", "codesigning"],
        capture_output=True, text=True,
    )
    for line in out.stdout.splitlines():
        if '"' in line:
            # Format: 1) <hash> "<identity>"
            return line.split('"', 2)[1]
    return None


def _hash_tree(p: Path) -> str:
    """Stable hash of a file or directory's contents (excludes mtimes)."""
    import hashlib
    h = hashlib.sha256()
    if not p.exists():
        return ""
    if p.is_file():
        h.update(p.read_bytes())
        return h.hexdigest()
    for sub in sorted(p.rglob("*")):
        if sub.is_file() and "/.build/" not in str(sub) and "__pycache__" not in str(sub):
            h.update(str(sub.relative_to(p)).encode())
            h.update(b"\x00")
            h.update(sub.read_bytes())
            h.update(b"\x01")
    return h.hexdigest()
