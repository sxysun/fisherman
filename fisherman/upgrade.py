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
  fisherman/, mirror/, relay/ (Python source incl. data/)
  pyproject.toml
  uv.lock
  menubar/   (only if the source has changed since last install,
              and only if `swift` is on PATH)

What is NEVER touched:
  ~/.fisherman/.env          (FISH_PRIVATE_KEY, friends, deputies)
  ~/.fisherman/frames/       (real captures)
  ~/.fisherman/audio/
  ~/.fisherman/logs/
  ~/.fisherman/.git/         (the install's git history is preserved)

Backups go to ~/.fisherman/.backup/<utc-timestamp>/. Last 3 are
kept; older ones are pruned automatically.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_INSTALL_DIR = Path.home() / ".fisherman"
BACKUP_DIRNAME = ".backup"
KEEP_BACKUPS = 3
DEFAULT_CONTROL_PORT = 7892
DEFAULT_RELEASE_REPO = "sxysun/fisherman"
PYTHON_PACKAGES = ("fisherman", "mirror", "relay")


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
    version:      Optional[str] = None


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
            version=stamp.get("version"),
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
        version=None,
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
    for item in (*PYTHON_PACKAGES, "pyproject.toml", "uv.lock"):
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
    for item in (*PYTHON_PACKAGES, "pyproject.toml", "uv.lock"):
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
    """Rsync Python packages + lockfiles into the install dir.

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
        files_changed = 0
        for package in PYTHON_PACKAGES:
            package_src = src / package
            if not package_src.is_dir():
                continue
            package_dst = install_dir / package
            cmd = [
                "rsync", "-a", "--delete",
                "--exclude=__pycache__",
                "--exclude=.pytest_cache",
                "--out-format=%n",
                f"{package_src}/", f"{package_dst}/",
            ]
            out = subprocess.run(cmd, capture_output=True, text=True, check=True)
            files_changed += len([ln for ln in out.stdout.splitlines() if ln.strip()])

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
    for p in (Path.home() / ".cargo/bin/uv",
              Path.home() / ".local/bin/uv",
              Path("/opt/homebrew/bin/uv"),
              Path("/usr/local/bin/uv")):
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
    if os.uname().machine == "arm64":
        python_exe = install_dir / ".venv" / "bin" / "python"
        if python_exe.exists():
            try:
                arch = subprocess.run(
                    [
                        str(python_exe),
                        "-c",
                        "import platform; print(platform.machine())",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                ).stdout.strip()
                if arch == "x86_64":
                    shutil.rmtree(install_dir / ".venv")
            except OSError:
                pass
        args.extend(["--python", "3.12", "--python-preference", "managed"])
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
# GitHub Release DMG upgrades
# ---------------------------------------------------------------------------

def latest_dmg_release(repo: str | None = None) -> dict:
    """Return metadata for the latest GitHub Release DMG asset."""
    repo = repo or os.environ.get("FISHERMAN_RELEASE_REPO") or DEFAULT_RELEASE_REPO
    api_url = os.environ.get(
        "FISHERMAN_RELEASE_API_URL",
        f"https://api.github.com/repos/{repo}/releases/latest",
    )
    release = _fetch_json(api_url)
    assets = release.get("assets") or []
    dmg = next(
        (
            a for a in assets
            if str(a.get("name") or "").startswith("Fisherman-")
            and str(a.get("name") or "").endswith(".dmg")
        ),
        None,
    )
    if not dmg:
        raise RuntimeError("latest GitHub Release has no Fisherman-*.dmg asset")

    sha = next(
        (
            a for a in assets
            if str(a.get("name") or "") == f"{dmg.get('name')}.sha256"
            or str(a.get("name") or "").endswith(".dmg.sha256")
        ),
        None,
    )
    if not sha:
        raise RuntimeError("latest GitHub Release has no DMG .sha256 asset")

    tag = str(release.get("tag_name") or "")
    return {
        "tag_name": tag,
        "version": tag[1:] if tag.startswith("v") else tag,
        "name": release.get("name") or tag,
        "published_at": release.get("published_at"),
        "html_url": release.get("html_url"),
        "dmg_name": dmg.get("name"),
        "dmg_url": dmg.get("browser_download_url"),
        "sha256_name": sha.get("name"),
        "sha256_url": sha.get("browser_download_url"),
    }


def install_dmg_release(install_dir: Path, release: dict | None = None) -> dict:
    """Download the latest release DMG, install its app, and run its bootstrap."""
    release = release or latest_dmg_release()
    dmg_url = release.get("dmg_url")
    sha_url = release.get("sha256_url")
    if not dmg_url or not sha_url:
        raise RuntimeError("release metadata is missing DMG or checksum URL")

    backup = make_backup(install_dir)
    mount_point: Path | None = None
    with tempfile.TemporaryDirectory(prefix="fisherman-release-") as tmp:
        tmp_path = Path(tmp)
        dmg_path = tmp_path / str(release.get("dmg_name") or "Fisherman.dmg")
        sha_path = tmp_path / str(release.get("sha256_name") or "Fisherman.dmg.sha256")
        mount_point = tmp_path / "mount"
        mount_point.mkdir()

        _download_file(dmg_url, dmg_path)
        _download_file(sha_url, sha_path)
        _verify_sha256(dmg_path, sha_path)

        try:
            subprocess.run(
                [
                    "hdiutil", "attach", "-readonly", "-nobrowse",
                    "-mountpoint", str(mount_point), str(dmg_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            app = mount_point / "Fisherman.app"
            resources = app / "Contents" / "Resources"
            bootstrap = resources / "bootstrap-user-install.sh"
            source = resources / "fisherman-source"
            release_json = resources / "fisherman-release.json"
            if not app.is_dir() or not bootstrap.is_file() or not source.is_dir():
                raise RuntimeError("DMG is missing Fisherman.app bootstrap resources")

            stop_daemon()
            subprocess.run(
                [
                    "/bin/bash", str(bootstrap), str(source),
                    str(install_dir), str(release_json),
                ],
                check=True,
            )
            install_app(app)
        except Exception:
            restore_backup(install_dir, backup)
            raise
        finally:
            if mount_point is not None:
                subprocess.run(
                    ["hdiutil", "detach", str(mount_point)],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

    launch_app()
    return {"release": release, "backup": str(backup)}


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

def diagnose() -> dict:
    """Snapshot of every subsystem fisherman depends on.

    Returns a flat dict {check: {ok, detail}}. Used by `fisherman repair`
    and `fisherman doctor`."""
    out: dict = {}
    try:
        from fisherman.config import FishermanConfig
        cfg = FishermanConfig()
        backend_ok = True
        backend_bits = [
            cfg.backend_summary,
            f"ingest={'enabled' if cfg.streaming_enabled else 'disabled'}",
            f"relay={cfg.status_relay_url}",
        ]
        if cfg.backend_mode in {"cloud", "self_hosted"} and not cfg.streaming_enabled:
            backend_ok = False
        if cfg.backend_mode == "cloud" and not cfg.streaming_enabled:
            if cfg.cloud_ingest_block_detail:
                backend_bits.append(cfg.cloud_ingest_block_detail)
            elif cfg.cloud_ingest_block_reason:
                backend_bits.append(cfg.cloud_ingest_block_reason)
            else:
                backend_bits.append("review Cloud release or finish account setup")
        out["backend"] = {
            "ok": backend_ok,
            "detail": "; ".join(backend_bits),
        }
        out["identity"] = {
            "ok": bool(cfg.private_key),
            "detail": "FISH_PRIVATE_KEY present"
                      if cfg.private_key else "FISH_PRIVATE_KEY missing",
        }
    except Exception as e:
        out["backend"] = {"ok": False, "detail": f"config error: {e}"}
    out["menubar"] = {
        "ok": menubar_running(),
        "detail": "FishermanMenu process found"
                  if menubar_running() else "FishermanMenu NOT running",
    }
    daemon = daemon_status()
    daemon_error = daemon.get("error") if daemon else None
    stream_error = daemon.get("stream_error") if daemon else None
    daemon_detail = "no response on 127.0.0.1:7892"
    if daemon:
        daemon_detail = f"control port up; frames_sent={daemon.get('frames_sent')}"
        if daemon_error:
            daemon_detail += f"; capture error={daemon_error}"
        if stream_error:
            daemon_detail += f"; stream error={stream_error}"
    out["daemon"] = {
        "ok": daemon is not None and not daemon_error and not stream_error,
        "detail": daemon_detail,
    }
    capture_backend = (
        str(daemon.get("capture_backend") or "").strip().lower()
        if daemon else ""
    ) or "native"
    out["capture"] = {
        "ok": daemon is not None and daemon_error is None,
        "detail": (
            f"{capture_backend} capture"
            if daemon_error is None
            else f"{capture_backend} capture error: {daemon_error}"
        ),
    }
    if "backend" in out:
        try:
            cfg = FishermanConfig()
            relay_ok, relay_detail = relay_health(cfg.status_relay_url, daemon=daemon)
            out["status_relay"] = {"ok": relay_ok, "detail": relay_detail}
        except Exception as e:
            out["status_relay"] = {"ok": False, "detail": f"relay config error: {e}"}
    out["app_bundle"] = {
        "ok": Path("/Applications/Fisherman.app").exists(),
        "detail": "/Applications/Fisherman.app exists"
                  if Path("/Applications/Fisherman.app").exists()
                  else "/Applications/Fisherman.app MISSING",
    }
    return out


def repair() -> dict:
    """Try to bring everything back to a healthy state.

    Order matters: the menubar must launch, then it starts the daemon.
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
    # 2. Flush zombies of menubar and daemon processes.
    _kill_and_wait("FishermanMenu", timeout=5.0)
    # 3. Bring the menubar back up (which starts the daemon).
    launch_app(retries=3)
    _maybe_start_local_relay()
    # 4. Give it a moment for the daemon to come up.
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


def relay_health(relay_url: str, *, daemon: Optional[dict] = None,
                 timeout: float = 5.0) -> tuple[bool, str]:
    relay_url = (relay_url or "").rstrip("/")
    if not relay_url:
        return False, "no relay URL configured"
    health_url = relay_url + "/health"
    relay_ws = bool(daemon and daemon.get("relay_connected"))
    try:
        with urllib.request.urlopen(health_url, timeout=timeout) as r:
            ok = r.status == 200
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as e:
        if relay_ws:
            return True, (
                f"{relay_url} health probe flaky: {e}; "
                "daemon RPC mailbox connected"
            )
        return False, f"{relay_url} not reachable: {e}"
    detail = f"{relay_url} reachable"
    if daemon is not None:
        detail += "; daemon RPC mailbox " + ("connected" if relay_ws else "not connected yet")
    return ok, detail


def _local_relay_port(relay_url: str) -> Optional[int]:
    try:
        parsed = urllib.parse.urlparse(relay_url)
    except Exception:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return None
    return parsed.port or (443 if parsed.scheme == "https" else 80)


def _maybe_start_local_relay() -> None:
    try:
        from fisherman.config import FishermanConfig
        cfg = FishermanConfig()
        port = _local_relay_port(cfg.status_relay_url)
        if port is None:
            return
        ok, _ = relay_health(cfg.status_relay_url, timeout=0.5)
        if ok:
            return
    except Exception:
        return

    python_exe = DEFAULT_INSTALL_DIR / ".venv" / "bin" / "python"
    if not python_exe.exists():
        python_exe = Path(sys.executable)
    data_dir = DEFAULT_INSTALL_DIR / "relay"
    log_dir = DEFAULT_INSTALL_DIR / "logs"
    data_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "relay.log"
    db_path = data_dir / "events.sqlite"
    logf = open(log_path, "ab")
    subprocess.Popen(
        [
            str(python_exe),
            "-u",
            "-m",
            "relay.server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--db-path",
            str(db_path),
        ],
        cwd=str(DEFAULT_INSTALL_DIR),
        stdout=logf,
        stderr=logf,
        start_new_session=True,
    )


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

def _request(url: str) -> urllib.request.Request:
    headers = {
        "Accept": "application/vnd.github+json, application/octet-stream;q=0.9, */*;q=0.8",
        "User-Agent": "fisherman-updater",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("FISHERMAN_GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, headers=headers)


def _fetch_json(url: str) -> dict:
    with urllib.request.urlopen(_request(url), timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _download_file(url: str, dst: Path) -> None:
    with urllib.request.urlopen(_request(url), timeout=60) as resp:
        with dst.open("wb") as f:
            shutil.copyfileobj(resp, f)


def _verify_sha256(path: Path, sha_file: Path) -> None:
    expected = sha_file.read_text().strip().split()[0].lower()
    actual = hashlib.sha256(path.read_bytes()).hexdigest().lower()
    if actual != expected:
        raise RuntimeError(
            f"sha256 mismatch for {path.name}: expected {expected}, got {actual}"
        )

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
