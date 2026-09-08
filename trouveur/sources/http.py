"""Shared polite HTTP client.

Every adapter goes through this so that politeness is implemented once rather than forgotten once
per source: a per-host minimum interval, an identifying User-Agent, and retries that back off
instead of hammering a source that is already struggling.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from trouveur.config import USER_AGENT, get_settings
from trouveur.sources.errors import FetchError

log = logging.getLogger(__name__)

_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 4


class PoliteClient:
    def __init__(self, delay: float | None = None, timeout: float | None = None) -> None:
        settings = get_settings()
        self._delay = settings.request_delay_seconds if delay is None else delay
        self._timeout = settings.http_timeout_seconds if timeout is None else timeout
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> PoliteClient:
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def _throttle(self, host: str) -> None:
        """Rate-limit per host, not globally.

        The lock is per host too: a global lock would serialise every source behind the slowest
        one, which at this fan-out costs far more than it protects.
        """
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            elapsed = time.monotonic() - self._last.get(host, 0.0)
            if elapsed < self._delay:
                await asyncio.sleep(self._delay - elapsed)
            self._last[host] = time.monotonic()

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        if self._client is None:
            raise FetchError("PoliteClient must be used as an async context manager.")
        host = urlsplit(url).netloc
        last_error = ""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            await self._throttle(host)
            try:
                response = await self._client.get(url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code not in _RETRY_STATUS:
                    return response
                last_error = f"HTTP {response.status_code}"
                retry_after = _retry_after(response)
                if retry_after is not None:
                    await asyncio.sleep(retry_after)
                    continue
            if attempt < _MAX_ATTEMPTS:
                # Jittered, so a source that rate-limited a burst does not receive the whole
                # burst again in lockstep once the backoff expires.
                await asyncio.sleep((2 ** (attempt - 1)) + random.uniform(0, 0.5))
        raise FetchError(
            f"Giving up on {url} after {_MAX_ATTEMPTS} attempts; last failure was {last_error}."
        )

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        response = await self.get(url, params=params, headers=headers)
        if response.status_code != 200:
            raise FetchError(f"{url} returned HTTP {response.status_code}.")
        try:
            return response.json()
        except ValueError as exc:
            raise FetchError(f"{url} returned a body that is not JSON: {exc}") from exc


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return min(float(raw), 120.0)
    except ValueError:
        return None
