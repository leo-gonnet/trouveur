"""Shared polite HTTP client.

Every adapter goes through this: it enforces the per-host delay and the identifying User-Agent
that AGENTS.md requires, so politeness is not re-implemented (or forgotten) per source.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from trouveur.config import USER_AGENT, get_settings

log = logging.getLogger(__name__)


class PoliteClient:
    def __init__(self, delay: float | None = None, timeout: float | None = None) -> None:
        settings = get_settings()
        self._delay = settings.request_delay_seconds if delay is None else delay
        self._timeout = settings.http_timeout_seconds if timeout is None else timeout
        self._last: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> PoliteClient:
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def _throttle(self, url: str) -> None:
        host = urlsplit(url).netloc
        async with self._lock:
            elapsed = time.monotonic() - self._last.get(host, 0.0)
            if elapsed < self._delay:
                await asyncio.sleep(self._delay - elapsed)
            self._last[host] = time.monotonic()

    async def get(
        self, url: str, *, params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        assert self._client is not None, "use PoliteClient as an async context manager"
        await self._throttle(url)
        return await self._client.get(url, params=params, headers=headers)

    async def get_json(
        self, url: str, *, params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        response = await self.get(url, params=params, headers=headers)
        response.raise_for_status()
        return response.json()
