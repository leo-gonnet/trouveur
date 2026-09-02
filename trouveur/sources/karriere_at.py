"""karriere.at — primary source for Austria.

robots.txt (checked 2026-08-31): `User-agent: * / Disallow:` — crawling is explicitly permitted.

Keyword search (the default) has no working pagination: `?page=`, `?seite=` and `/seite-N` all
return the same 15 rows, because the real listing is XHR-driven. Recall therefore scales with the
number of profile keywords, not with page depth. Titles are rendered server-side, so candidates
can be filtered before paying for a detail page.

The sitemap sweep (opt-in, `sitemap_sweep > 0`) carries no titles, so it spends one request per
job to discover mostly irrelevant vacancies. Prefer more keywords.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.parse import quote

from trouveur.models import Job
from trouveur.sources import jsonld
from trouveur.sources.http import PoliteClient

log = logging.getLogger(__name__)

BASE = "https://www.karriere.at"
SITEMAP_URL = f"{BASE}/static/sitemaps/sitemap-jobs-https.xml"

_SITEMAP_ENTRY = re.compile(
    r"<url>\s*<loc>(?P<loc>[^<]+)</loc>\s*(?:<lastmod>(?P<lastmod>[^<]+)</lastmod>)?",
    re.IGNORECASE,
)
# Search results are server-rendered, so id and title come from the listing itself.
_LISTING = re.compile(
    r'titleLink"\s+href="https://www\.karriere\.at/jobs/(?P<id>\d+)"[^>]*>(?P<title>[^<]+)<'
)
_JOB_ID = re.compile(r"/jobs/(\d+)")


class KarriereAtSource:
    name = "karriere_at"

    def __init__(
        self,
        keywords: list[str] | None = None,
        *,
        sitemap_sweep: int = 0,
        title_filter=None,
    ) -> None:
        self.keywords = keywords or ["Wirtschaftsingenieur"]
        self.sitemap_sweep = sitemap_sweep
        # Optional callable(title) -> bool, applied before fetching a detail page. The pipeline
        # passes the rules filter here so irrelevant titles never cost a request.
        self.title_filter = title_filter

    async def fetch(self, client: PoliteClient, since: datetime | None) -> AsyncIterator[Job]:
        candidates: dict[str, str | None] = {}

        for keyword in self.keywords:
            try:
                for job_id, title in await self._search(client, keyword):
                    candidates.setdefault(job_id, title)
            except Exception as exc:  # noqa: BLE001 - one bad keyword must not stop the source
                log.warning("karriere.at: search for %r failed: %s", keyword, exc)

        log.info(
            "karriere.at: %d candidates from %d keyword(s)",
            len(candidates), len(self.keywords),
        )

        if self.sitemap_sweep > 0:
            try:
                for job_id in (await self._changed_ids(client, since))[: self.sitemap_sweep]:
                    candidates.setdefault(job_id, None)
            except Exception as exc:  # noqa: BLE001
                log.warning("karriere.at: sitemap sweep failed: %s", exc)

        for job_id, title in candidates.items():
            # Title is known for search hits, so skip obvious misses before spending a request.
            if title and self.title_filter and not self.title_filter(title):
                continue
            job = await self._detail(client, job_id)
            if job is not None:
                yield job

    async def _search(self, client: PoliteClient, keyword: str) -> list[tuple[str, str]]:
        url = f"{BASE}/jobs/{quote(keyword.lower().replace(' ', '-'))}"
        response = await client.get(url)
        if response.status_code != 200:
            log.debug("karriere.at: search %s returned %s", url, response.status_code)
            return []
        return [
            (m.group("id"), _unescape(m.group("title").strip()))
            for m in _LISTING.finditer(response.text)
        ]

    async def _detail(self, client: PoliteClient, job_id: str) -> Job | None:
        url = f"{BASE}/jobs/{job_id}"
        try:
            response = await client.get(url)
            if response.status_code != 200:
                return None
            posting = jsonld.find_job_posting(response.text)
        except Exception as exc:  # noqa: BLE001 - one bad page must not stop the sweep
            log.debug("karriere.at: skipping %s (%s)", url, exc)
            return None
        if posting is None:
            return None
        return jsonld.to_job(posting, source=self.name, url=url, native_id=job_id)

    async def _changed_ids(self, client: PoliteClient, since: datetime | None) -> list[str]:
        response = await client.get(SITEMAP_URL)
        response.raise_for_status()

        entries: list[tuple[str, datetime | None]] = []
        for match in _SITEMAP_ENTRY.finditer(response.text):
            lastmod = _parse(match.group("lastmod"))
            if since is not None and lastmod is not None and lastmod < since:
                continue
            found = _JOB_ID.search(match.group("loc"))
            if found:
                entries.append((found.group(1), lastmod))

        entries.sort(key=lambda e: e[1] or datetime.min.replace(tzinfo=UTC), reverse=True)
        return [job_id for job_id, _ in entries]


def _unescape(text: str) -> str:
    return html.unescape(text)


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
