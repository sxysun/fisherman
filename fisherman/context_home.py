"""Context-home export, import, and delete helpers.

The archive format is intentionally plain JSON so users can inspect it
before moving data between Local Only, Fisherman Cloud, and self-hosted
homes. Images are optional because they make archives large and sensitive.
"""

from __future__ import annotations

import base64
import datetime as _dt
import json
import os
from pathlib import Path
from typing import Any

from fisherman.audio_store import AudioStore
from fisherman.capture import ScreenFrame
from fisherman.config import FishermanConfig
from fisherman.frame_store import FrameStore


ARCHIVE_FORMAT = "fisherman.context.v1"


def _utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _ts_seconds(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            dt = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_dt.timezone.utc)
            return dt.timestamp()
    raise ValueError(f"invalid timestamp: {value!r}")


def _in_window(ts: float, since_ts: float | None, until_ts: float | None) -> bool:
    if since_ts is not None and ts < since_ts:
        return False
    if until_ts is not None and ts > until_ts:
        return False
    return True


def _frame_json_paths(frames_dir: str) -> list[Path]:
    base = Path(os.path.expanduser(frames_dir))
    if not base.is_dir():
        return []
    return sorted(base.glob("*/*.json"), reverse=True)


def _iter_local_frames(
    cfg: FishermanConfig,
    *,
    since_ts: float | None,
    until_ts: float | None,
    limit: int,
) -> list[tuple[dict, Path]]:
    out: list[tuple[dict, Path]] = []
    for path in _frame_json_paths(cfg.frames_dir):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        try:
            ts = _ts_seconds(row.get("ts"))
        except Exception:
            continue
        if not _in_window(ts, since_ts, until_ts):
            continue
        row["ts"] = ts
        row.setdefault("ts_ms", int(ts * 1000))
        jpg = path.with_suffix(".jpg")
        row["has_image"] = jpg.is_file()
        out.append((row, path))
        if len(out) >= limit:
            break
    return out


def _iter_local_audio_records(
    cfg: FishermanConfig,
    *,
    since_ts: float | None,
    until_ts: float | None,
    limit: int,
) -> list[tuple[dict, Path]]:
    base = Path(os.path.expanduser(cfg.audio_dir))
    if not base.is_dir():
        return []
    out: list[tuple[dict, Path]] = []
    for path in sorted(base.glob("*/*.jsonl"), reverse=True):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                ts = _ts_seconds(row.get("ts"))
            except Exception:
                continue
            if not _in_window(ts, since_ts, until_ts):
                continue
            row["ts"] = ts
            row.setdefault("ts_ms", int(ts * 1000))
            out.append((row, path))
            if len(out) >= limit:
                return out
    return out


def load_archive(path: str | os.PathLike[str]) -> dict:
    archive = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if archive.get("format") != ARCHIVE_FORMAT:
        raise ValueError(f"unsupported archive format: {archive.get('format')!r}")
    frames = archive.get("frames")
    audio = archive.get("audio_transcripts")
    if not isinstance(frames, list) or not isinstance(audio, list):
        raise ValueError("archive must contain frames and audio_transcripts arrays")
    return archive


def write_archive(path: str | os.PathLike[str], archive: dict) -> None:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(json.dumps(archive, indent=2) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
        os.chmod(target, 0o600)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def export_local_context(
    path: str | os.PathLike[str],
    cfg: FishermanConfig,
    *,
    since_ts: float | None = None,
    until_ts: float | None = None,
    limit: int = 5000,
    include_images: bool = False,
) -> dict:
    frame_rows: list[dict] = []
    for row, meta_path in _iter_local_frames(
        cfg,
        since_ts=since_ts,
        until_ts=until_ts,
        limit=max(1, int(limit)),
    ):
        exported = {
            "ts": row.get("ts"),
            "ts_ms": row.get("ts_ms"),
            "app": row.get("app"),
            "bundle": row.get("bundle"),
            "window": row.get("window"),
            "w": row.get("w"),
            "h": row.get("h"),
            "ocr_text": row.get("ocr_text") or "",
            "urls": row.get("urls") or [],
            "tier_hint": row.get("tier_hint"),
            "routing_signals": row.get("routing_signals"),
            "has_image": bool(row.get("has_image")),
        }
        if include_images and exported["has_image"]:
            try:
                exported["image_b64"] = base64.b64encode(
                    meta_path.with_suffix(".jpg").read_bytes()
                ).decode("ascii")
            except OSError:
                exported["image_b64"] = None
        frame_rows.append(exported)

    audio_rows = [
        {
            "ts": row.get("ts"),
            "ts_ms": row.get("ts_ms"),
            "transcript": row.get("transcript") or "",
            "meeting_app": row.get("meeting_app"),
            "device_name": row.get("device_name"),
            "is_input_device": bool(row.get("is_input_device")),
        }
        for row, _path in _iter_local_audio_records(
            cfg,
            since_ts=since_ts,
            until_ts=until_ts,
            limit=max(1, int(limit)),
        )
    ]

    archive = {
        "format": ARCHIVE_FORMAT,
        "exported_at": _utc_now(),
        "source": {
            "kind": "local",
            "frames_dir": os.path.expanduser(cfg.frames_dir),
            "audio_dir": os.path.expanduser(cfg.audio_dir),
        },
        "options": {
            "since_ts": since_ts,
            "until_ts": until_ts,
            "limit": limit,
            "include_images": include_images,
        },
        "frames": frame_rows,
        "audio_transcripts": audio_rows,
    }
    write_archive(path, archive)
    return {
        "ok": True,
        "path": str(Path(path).expanduser()),
        "frames": len(frame_rows),
        "audio_transcripts": len(audio_rows),
        "include_images": include_images,
    }


def import_local_context(path: str | os.PathLike[str], cfg: FishermanConfig) -> dict:
    archive = load_archive(path)
    frame_store = FrameStore(cfg.frames_dir, cfg.local_frames_max)
    audio_store = AudioStore(cfg.audio_dir, cfg.audio_max_days)
    imported_frames = 0
    imported_audio = 0

    for row in archive["frames"]:
        if not isinstance(row, dict):
            continue
        try:
            ts = _ts_seconds(row.get("ts"))
        except Exception:
            continue
        jpeg_data = b""
        image_b64 = row.get("image_b64")
        if isinstance(image_b64, str) and image_b64:
            try:
                jpeg_data = base64.b64decode(image_b64, validate=True)
            except Exception:
                jpeg_data = b""
        frame = ScreenFrame(
            jpeg_data=jpeg_data,
            width=int(row.get("w") or row.get("width") or 0),
            height=int(row.get("h") or row.get("height") or 0),
            app_name=row.get("app"),
            bundle_id=row.get("bundle") or row.get("bundle_id"),
            window_title=row.get("window"),
            timestamp=ts,
        )
        frame_store.save(
            frame,
            str(row.get("ocr_text") or ""),
            list(row.get("urls") or []),
        )
        imported_frames += 1

    for row in archive["audio_transcripts"]:
        if not isinstance(row, dict):
            continue
        transcript = str(row.get("transcript") or "")
        if not transcript:
            continue
        try:
            ts = _ts_seconds(row.get("ts"))
        except Exception:
            continue
        audio_store.save(
            ts,
            transcript,
            row.get("meeting_app"),
            row.get("device_name"),
            bool(row.get("is_input_device")),
        )
        imported_audio += 1

    return {
        "ok": True,
        "path": str(Path(path).expanduser()),
        "imported_frames": imported_frames,
        "imported_audio_transcripts": imported_audio,
    }


def delete_local_context(
    cfg: FishermanConfig,
    *,
    since_ts: float | None = None,
    until_ts: float | None = None,
    limit: int = 50000,
    dry_run: bool = False,
) -> dict:
    frames = _iter_local_frames(
        cfg,
        since_ts=since_ts,
        until_ts=until_ts,
        limit=max(1, int(limit)),
    )
    frame_files = 0
    if not dry_run:
        for _row, meta_path in frames:
            for path in (meta_path, meta_path.with_suffix(".jpg")):
                try:
                    path.unlink()
                    frame_files += 1
                except FileNotFoundError:
                    pass
            parent = meta_path.parent
            try:
                if parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
            except OSError:
                pass

    audio_records = _iter_local_audio_records(
        cfg,
        since_ts=since_ts,
        until_ts=until_ts,
        limit=max(1, int(limit)),
    )
    affected_audio_paths = {path for _row, path in audio_records}
    deleted_audio = 0
    if not dry_run:
        for path in affected_audio_paths:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            kept: list[str] = []
            for line in lines:
                try:
                    row = json.loads(line)
                    ts = _ts_seconds(row.get("ts"))
                except Exception:
                    kept.append(line)
                    continue
                if _in_window(ts, since_ts, until_ts):
                    deleted_audio += 1
                else:
                    kept.append(line)
            if kept:
                path.write_text("\n".join(kept) + "\n", encoding="utf-8")
            else:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            parent = path.parent
            try:
                if parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
            except OSError:
                pass

    return {
        "ok": True,
        "dry_run": dry_run,
        "frames": len(frames),
        "audio_transcripts": len(audio_records) if dry_run else deleted_audio,
        "frame_files_removed": frame_files,
    }
