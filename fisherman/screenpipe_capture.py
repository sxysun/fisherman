import json
import re
import struct
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import ProxyHandler, Request, build_opener

import structlog

from fisherman.capture import ScreenFrame

log = structlog.get_logger()

_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")
_NO_PROXY_OPENER = build_opener(ProxyHandler({}))


class ScreenpipeCaptureError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ScreenpipeFrameRef:
    frame_id: int
    timestamp: float
    app_name: str | None
    window_title: str | None
    ocr_text: str


@dataclass(frozen=True, slots=True)
class ScreenpipeFramePayload:
    frame: ScreenFrame
    ocr_text: str
    urls: list[str]
    frame_id: int
    video_path: str | None = None
    video_offset: int = 0


class ScreenpipeCaptureClient:
    def __init__(
        self,
        base_url: str,
        search_limit: int = 50,
        lookback_seconds: float = 15.0,
        timeout: float = 5.0,
        dedupe_cache_size: int = 512,
    ):
        self._base_url = base_url.rstrip("/")
        self._search_limit = search_limit
        self._lookback_seconds = lookback_seconds
        self._timeout = timeout
        self._seen_ids: deque[int] = deque(maxlen=dedupe_cache_size)
        self._seen_lookup: set[int] = set()
        self._last_seen_timestamp: float | None = None

    def poll(self) -> list[ScreenpipeFramePayload]:
        payload = self._fetch_json("/search", self._build_search_params())
        refs = self._parse_search_response(payload)

        # Filter out already-seen frames
        new_refs = [ref for ref in refs if ref.frame_id not in self._seen_lookup]
        if not new_refs:
            return []

        # Batch-fetch video chunk paths and offsets
        video_info = self._fetch_video_info([ref.frame_id for ref in new_refs])

        results: list[ScreenpipeFramePayload] = []
        for ref in new_refs:
            context = self._fetch_frame_context(ref.frame_id, fallback_text=ref.ocr_text)

            # Try to fetch the JPEG frame; proceed without image if unavailable
            # (screenpipe can't extract frames from MP4s still being written)
            jpeg_data = b""
            width, height = 0, 0
            try:
                jpeg_data = self._fetch_bytes(f"/frames/{ref.frame_id}")
                width, height = _extract_image_size(jpeg_data)
            except ScreenpipeCaptureError:
                log.debug("frame_image_unavailable", frame_id=ref.frame_id)

            vinfo = video_info.get(ref.frame_id, {})

            frame = ScreenFrame(
                jpeg_data=jpeg_data,
                width=width,
                height=height,
                app_name=ref.app_name,
                bundle_id=None,
                window_title=ref.window_title,
                timestamp=ref.timestamp,
            )
            results.append(
                ScreenpipeFramePayload(
                    frame=frame,
                    ocr_text=context["text"],
                    urls=context["urls"],
                    frame_id=ref.frame_id,
                    video_path=vinfo.get("file_path"),
                    video_offset=vinfo.get("offset_index", 0),
                )
            )
            self._remember_frame(ref.frame_id, ref.timestamp)

        return results

    def _build_search_params(self) -> dict[str, str]:
        params = {
            "content_type": "ocr",
            "limit": str(self._search_limit),
        }
        if self._last_seen_timestamp is not None:
            start_time = datetime.fromtimestamp(
                max(self._last_seen_timestamp - self._lookback_seconds, 0.0),
                tz=timezone.utc,
            )
            params["start_time"] = _isoformat_z(start_time)
        return params

    def _fetch_video_info(self, frame_ids: list[int]) -> dict[int, dict]:
        """Batch-fetch video chunk file_path and offset_index for frame IDs."""
        if not frame_ids:
            return {}
        ids_str = ",".join(str(fid) for fid in frame_ids)
        query = (
            f"SELECT f.id, f.offset_index, vc.file_path "
            f"FROM frames f JOIN video_chunks vc ON f.video_chunk_id = vc.id "
            f"WHERE f.id IN ({ids_str})"
        )
        try:
            rows = self._post_json("/raw_sql", {"query": query})
        except ScreenpipeCaptureError:
            log.debug("video_info_fetch_failed", count=len(frame_ids))
            return {}
        result: dict[int, dict] = {}
        if isinstance(rows, list):
            for row in rows:
                fid = row.get("id")
                if isinstance(fid, int):
                    result[fid] = row
        return result

    def _post_json(self, path: str, data: dict) -> object:
        url = _build_url(self._base_url, path)
        body = json.dumps(data).encode()
        req = Request(url, data=body, headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        try:
            with _NO_PROXY_OPENER.open(req, timeout=self._timeout) as resp:
                return json.loads(resp.read())
        except HTTPError as exc:
            raise ScreenpipeCaptureError(
                f"screenpipe HTTP {exc.code} for POST {path}"
            ) from exc
        except (URLError, OSError) as exc:
            raise ScreenpipeCaptureError(
                f"screenpipe POST {path} failed: {exc}"
            ) from exc

    def _fetch_frame_context(self, frame_id: int, fallback_text: str) -> dict[str, object]:
        try:
            payload = self._fetch_json(f"/frames/{frame_id}/context")
        except ScreenpipeCaptureError:
            return {"text": fallback_text, "urls": _extract_urls(fallback_text)}

        text = payload.get("text") if isinstance(payload.get("text"), str) else fallback_text
        urls = payload.get("urls")
        if not isinstance(urls, list):
            urls = _extract_urls(text)
        else:
            urls = [url for url in urls if isinstance(url, str)]
        return {"text": text, "urls": urls}

    def _fetch_json(self, path: str, params: dict[str, str] | None = None) -> dict:
        body = self._fetch_bytes(path, params=params)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ScreenpipeCaptureError(
                f"screenpipe returned invalid JSON for {path}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise ScreenpipeCaptureError(
                f"screenpipe returned unexpected JSON payload for {path}"
            )
        return payload

    def _fetch_bytes(self, path: str, params: dict[str, str] | None = None) -> bytes:
        url = _build_url(self._base_url, path, params)
        req = Request(url, headers={"Accept": "application/json, image/jpeg"})
        try:
            with _NO_PROXY_OPENER.open(req, timeout=self._timeout) as resp:
                return resp.read()
        except HTTPError as exc:
            raise ScreenpipeCaptureError(
                f"screenpipe HTTP {exc.code} for {path}"
            ) from exc
        except URLError as exc:
            raise ScreenpipeCaptureError(
                f"failed to reach screenpipe at {self._base_url}: {exc}"
            ) from exc
        except OSError as exc:
            raise ScreenpipeCaptureError(
                f"failed to read screenpipe response for {path}: {exc}"
            ) from exc

    def _remember_frame(self, frame_id: int, timestamp: float) -> None:
        if len(self._seen_ids) == self._seen_ids.maxlen:
            evicted = self._seen_ids.popleft()
            self._seen_lookup.discard(evicted)
        self._seen_ids.append(frame_id)
        self._seen_lookup.add(frame_id)
        self._last_seen_timestamp = max(self._last_seen_timestamp or 0.0, timestamp)

    @staticmethod
    def _parse_search_response(payload: dict) -> list[ScreenpipeFrameRef]:
        grouped: dict[int, dict[str, object]] = {}
        for item in payload.get("data", []):
            if not isinstance(item, dict) or item.get("type") != "OCR":
                continue
            content = item.get("content")
            if not isinstance(content, dict):
                continue

            frame_id = content.get("frame_id")
            timestamp = _parse_timestamp(content.get("timestamp"))
            if not isinstance(frame_id, int) or timestamp is None:
                continue

            group = grouped.setdefault(
                frame_id,
                {
                    "timestamp": timestamp,
                    "app_name": content.get("app_name"),
                    "window_title": content.get("window_name"),
                    "texts": [],
                },
            )
            group["timestamp"] = max(float(group["timestamp"]), timestamp)
            if not group.get("app_name") and isinstance(content.get("app_name"), str):
                group["app_name"] = content.get("app_name")
            if not group.get("window_title") and isinstance(content.get("window_name"), str):
                group["window_title"] = content.get("window_name")

            text = content.get("text")
            if isinstance(text, str) and text.strip():
                group["texts"].append(text.strip())

        refs: list[ScreenpipeFrameRef] = []
        for frame_id, group in grouped.items():
            refs.append(
                ScreenpipeFrameRef(
                    frame_id=frame_id,
                    timestamp=float(group["timestamp"]),
                    app_name=group["app_name"] if isinstance(group["app_name"], str) else None,
                    window_title=group["window_title"]
                    if isinstance(group["window_title"], str)
                    else None,
                    ocr_text=_join_unique_texts(group["texts"]),
                )
            )

        refs.sort(key=lambda ref: (ref.timestamp, ref.frame_id))
        return refs


def _join_unique_texts(texts: list[str]) -> str:
    seen: set[str] = set()
    ordered: list[str] = []
    for text in texts:
        if text not in seen:
            seen.add(text)
            ordered.append(text)
    return "\n".join(ordered)


def _extract_urls(text: str) -> list[str]:
    return _URL_RE.findall(text or "")


def _parse_timestamp(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _isoformat_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _build_url(base_url: str, path: str, params: dict[str, str] | None = None) -> str:
    split = urlsplit(base_url)
    query = urlencode(params or {})
    full_path = f"{split.path.rstrip('/')}{path}"
    return urlunsplit((split.scheme, split.netloc, full_path, query, ""))


def _extract_image_size(data: bytes) -> tuple[int, int]:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return int(width), int(height)

    if data.startswith(b"\xff\xd8"):
        offset = 2
        while offset + 9 < len(data):
            if data[offset] != 0xFF:
                offset += 1
                continue
            marker = data[offset + 1]
            offset += 2
            if marker in (0xD8, 0xD9):
                continue
            if offset + 2 > len(data):
                break
            segment_length = struct.unpack(">H", data[offset:offset + 2])[0]
            if segment_length < 2 or offset + segment_length > len(data):
                break
            if marker in {
                0xC0, 0xC1, 0xC2, 0xC3,
                0xC5, 0xC6, 0xC7,
                0xC9, 0xCA, 0xCB,
                0xCD, 0xCE, 0xCF,
            }:
                height, width = struct.unpack(">HH", data[offset + 3:offset + 7])
                return int(width), int(height)
            offset += segment_length

    raise ScreenpipeCaptureError("unsupported image payload from screenpipe")
