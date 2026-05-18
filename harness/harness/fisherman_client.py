from __future__ import annotations

import asyncio
from urllib.parse import urlencode
from typing import Any, Optional

import aiohttp


class FishermanClient:
    """The ONLY file in the harness that knows Fisherman's HTTP API."""

    def __init__(self, base_url: str, timeout_sec: float = 3.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = aiohttp.ClientTimeout(total=timeout_sec)

    async def _get_json(self, path: str) -> Optional[Any]:
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as s:
                async with s.get(f"{self.base_url}{path}") as r:
                    if r.status != 200:
                        return None
                    return await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None

    async def _get_bytes(self, path: str) -> Optional[bytes]:
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as s:
                async with s.get(f"{self.base_url}{path}") as r:
                    if r.status != 200:
                        return None
                    return await r.read()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None

    async def get_status(self) -> Optional[dict]:
        return await self._get_json("/status")

    async def list_frames(self, count: int = 50) -> list[dict]:
        result = await self._get_json(f"/frames?count={count}")
        return result if isinstance(result, list) else []

    async def get_frame_image(self, ts_ms: int) -> Optional[bytes]:
        return await self._get_bytes(f"/frames/{ts_ms}/image")

    async def query_frames(
        self,
        *,
        since: Optional[str] = None,
        app: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        params = []
        if since:
            params.append(("since", since))
        if app:
            params.append(("app", app))
        if search:
            params.append(("search", search))
        params.append(("limit", str(limit)))
        result = await self._get_json("/query?" + urlencode(params))
        return result if isinstance(result, list) else []

    async def get_transcripts(self, since: str = "5m", limit: int = 50) -> list[dict]:
        result = await self._get_json(f"/transcripts?since={since}&limit={limit}")
        return result if isinstance(result, list) else []

    async def is_alive(self) -> bool:
        status = await self.get_status()
        return status is not None
