"""Shared polite HTTP client. Every adapter goes through it."""

from __future__ import annotations

import asyncio
import copy
import logging
import random
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from trouveur.config import USER_AGENT, get_settings
from trouveur.sources.errors import FetchError, SourceBlocked

log = logging.getLogger(__name__)

_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 4

# Bot walls, matched only on a refusal or where JSON was expected: Cloudflare injects a
# `challenge-platform` script into ordinary pages it serves, so that name alone proves nothing.
_CHALLENGE_MARKERS = (
    "<title>just a moment...</title>",
    "_cf_chl_opt",
    "attention required! | cloudflare",
    "captcha-delivery.com",
    "px-captcha",
    "_incapsula_resource",
    "pardon our interruption",
)


def throttle_key(url: str) -> str:
    """The provider a URL belongs to, which is what the politeness budget is owed to.

    The registrable domain, NOT the hostname: Personio and Breezy give every tenant its own
    subdomain, so a per-host budget would be one budget per board -- a thousand requests a second
    at one provider, with every counter still reading as compliant. Over-grouping is the safe
    direction to be wrong in.
    """
    host = urlsplit(url).netloc.rsplit("@", 1)[-1].split(":", 1)[0].lower()
    labels = [label for label in host.split(".") if label]
    if len(labels) <= 2:
        return host
    return ".".join(labels[-2:])


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

    def paced(self, delay: float) -> PoliteClient:
        """The same client, waiting `delay` between requests to one provider.

        Shares the connection pool and the per-provider clock, so two sources on one provider
        still space their requests from each other.
        """
        view = copy.copy(self)
        view._delay = delay
        return view

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def _throttle(self, host: str) -> None:
        """Rate-limit per provider, not globally and not per hostname."""
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
        return await self.request("GET", url, params=params, headers=headers)

    async def post(
        self,
        url: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        return await self.request("POST", url, json=json, params=params, headers=headers)

    async def request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """One retry-and-throttle path for every verb; POST is here so Workday's body search
        cannot become a second place to forget the politeness budget."""
        if self._client is None:
            raise FetchError("PoliteClient must be used as an async context manager.")
        host = throttle_key(url)
        last_error = ""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            await self._throttle(host)
            try:
                response = await self._client.request(
                    method, url, params=params, headers=headers, json=json
                )
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code in {403, 429, 503} and is_challenge(response):
                    raise SourceBlocked(
                        f"{url} answered HTTP {response.status_code} with a bot challenge page."
                    )
                if response.status_code not in _RETRY_STATUS:
                    return response
                last_error = f"HTTP {response.status_code}"
                retry_after = _retry_after(response)
                if retry_after is not None:
                    await asyncio.sleep(retry_after)
                    continue
            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep((2 ** (attempt - 1)) + random.uniform(0, 0.5))
        # Only 429 is a refusal. A 503 can mean "no such board": Traffit answers an unknown
        # company that way.
        error = SourceBlocked if last_error == "HTTP 429" else FetchError
        raise error(
            f"Giving up on {url} after {_MAX_ATTEMPTS} attempts; last failure was {last_error}."
        )

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        return _decode(url, await self.get(url, params=params, headers=headers))

    async def post_json(
        self,
        url: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        return _decode(url, await self.post(url, json=json, params=params, headers=headers))


def is_challenge(response: httpx.Response) -> bool:
    if "html" not in response.headers.get("content-type", "").lower():
        return False
    head = response.text[:20_000].lower()
    return any(marker in head for marker in _CHALLENGE_MARKERS)


def _decode(url: str, response: httpx.Response) -> Any:
    if response.status_code != 200:
        raise FetchError(f"{url} returned HTTP {response.status_code}.")
    if not response.content.strip():
        raise SourceBlocked(f"{url} returned HTTP 200 with an empty body where JSON was expected.")
    if is_challenge(response):
        raise SourceBlocked(f"{url} returned a bot challenge page where JSON was expected.")
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
