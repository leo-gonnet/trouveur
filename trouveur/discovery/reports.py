"""Answering a reader's "I found this job somewhere else".

The only measure of recall on a job somebody actually wanted, and a lead source at the same time:
whatever the answer turns out to be, the link is filed as a lead and the board behind it proposed.

The web app stores the URL and nothing else -- it never fetches, and it must not resolve either,
since resolving is versioned and a second implementation of it would answer differently the day
the rules change. The runner picks the report up on its next tick and does all three steps: read
the link, look for the posting, record one answer.

Nothing here fetches the reported URL. We never ask the page itself whether it is still open, so
a job we hold but never showed is as far as this can see; importing a posting by URL is a separate
job (docs/tasks/10).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

from trouveur.db.engine import connect
from trouveur.db.queries import admin as admin_q
from trouveur.db.queries import reports as q
from trouveur.db.queries import users as users_q
from trouveur.discovery.resolve import resolve
from trouveur.discovery.work import record_leads
from trouveur.models import Lead, LeadOrigin, LeadResult, ReportOutcome, ReportReason
from trouveur.sources import registry

log = logging.getLogger(__name__)

# Reports per pass. A reader is waiting, and there are never many: this is a person pasting a
# link, not a crawl.
REPORT_BATCH = 20

# Query parameters that say where the reader came from rather than which job they are looking at.
# Short on purpose: dropping an unknown parameter is how a link to one job starts matching
# another, and `?gh_jid=` IS the job on an embedded Greenhouse board.
_TRACKING = frozenset({"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
                       "gh_src", "fbclid", "gclid", "mc_cid", "mc_eid"})

# A path segment or parameter value long enough to be an id and short enough not to be prose.
_ID_CHARS = range(2, 81)


def url_variants(url: str) -> list[str]:
    """Spellings of one pasted link to compare against the URL we stored for the posting.

    The reader's link and ours differ in ways that mean nothing: the board's public host against
    the one its API answers on, `www.`, a trailing slash, the tracking parameter the site they
    found it on added. Comparing the pasted string alone would call almost every reported job
    missing, and canonicalising our own column instead would need the same rules in SQL.
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return []
    query = "&".join(
        f"{name}={value}"
        for name, values in parse_qs(parts.query, keep_blank_values=True).items()
        if name.lower() not in _TRACKING
        for value in values
    )
    bare = parts.hostname.lower().removeprefix("www.")
    trimmed = parts.path.rstrip("/")
    # The link exactly as pasted comes first, so the plain case cannot depend on the rebuilding
    # below: a query string put back together is not always character for character the original.
    variants: dict[str, None] = {url.strip(): None}
    for scheme in ("https", "http"):
        for host in (bare, f"www.{bare}"):
            for path in (trimmed, f"{trimmed}/", parts.path):
                variants.setdefault(urlunsplit((scheme, host, path, query, "")), None)
    return list(variants)


def id_tokens(url: str, scope: str | None) -> list[str]:
    """Every part of a link that could be the posting's own id at its source.

    Path segments and parameter values, because platforms put the id in both, and each one again
    behind the board it belongs to: a board source scopes its ids by tenant (`acme:123`), since
    none of these platforms documents its ids as globally unique.

    Loose on purpose. A token is only ever compared against `external_id` inside one board, where
    nothing else can be equal to it, so a handful of segments that are obviously not ids costs an
    array element and no precision at all.
    """
    parts = urlsplit(url.strip())
    found = [segment for segment in parts.path.split("/") if segment]
    found += [value for values in parse_qs(parts.query).values() for value in values]
    tokens = {token for token in found if len(token) in _ID_CHARS}
    if scope:
        tokens |= {f"{scope}:{token}" for token in tokens}
    return sorted(tokens)


@dataclass(frozen=True)
class Facts:
    """What we know about one reported link. The answer is a pure function of these."""

    # The platform the link named, when it is one we sweep at all, and the board on it. The
    # scope is recorded with the answer but reads no part in it.
    source: str | None
    scope: str | None
    # Whether the board behind the link is one the crawl set actually sweeps. True for a source
    # that sweeps a whole platform: there is no board to be missing.
    swept: bool
    # Our copy of the posting, when the link turned out to name one we hold.
    job_id: int | None = None
    shown: bool = False
    retrieved: bool = False
    in_area: bool = True


def decide(facts: Facts) -> tuple[ReportOutcome, ReportReason | None]:
    """One answer per report, naming the FIRST thing that stopped the job reaching the reader.

    The order is the pipeline's. A posting outside the reader's area was never retrieved either,
    and never scored either, so answering with a later stage would send somebody to look at a
    part that was working.
    """
    if facts.job_id is None:
        if facts.source is None:
            return ReportOutcome.UNKNOWN_PLATFORM, None
        return (ReportOutcome.MISSING_JOB if facts.swept else ReportOutcome.MISSING_BOARD), None
    if facts.shown:
        return ReportOutcome.HAD_AND_RECOMMENDED, None
    if not facts.in_area:
        return ReportOutcome.HAD_NOT_RECOMMENDED, ReportReason.LOCATION
    if not facts.retrieved:
        return ReportOutcome.HAD_NOT_RECOMMENDED, ReportReason.NOT_RETRIEVED
    return ReportOutcome.HAD_NOT_RECOMMENDED, ReportReason.NOT_SCORED


async def answer_pending(limit: int = REPORT_BATCH) -> int:
    """Answer one page of reports. Returns how many were answered."""
    async with connect() as conn:
        rows = await q.pending(conn, chunk=limit)
    if not rows:
        return 0

    # Filed as leads before anything is answered, so the board behind a link is proposed even
    # when the answer turns out to be "we had it already". `record_leads` promotes in the same
    # call, which is what lets the answer below say whether the board is swept.
    await record_leads(
        [Lead(origin=LeadOrigin.USER_REPORT, url=row.url) for row in rows]
    )

    async with connect() as conn:
        tenants = await admin_q.enabled_tenants(conn)
        profiles: dict[int, Any] = {}
        for row in rows:
            if row.user_id not in profiles:
                profiles[row.user_id] = await users_q.get_profile(conn, row.user_id)
            facts = await _facts(conn, row, tenants, profiles[row.user_id])
            outcome, reason = decide(facts)
            await q.answer(
                conn,
                report_id=row.id,
                outcome=outcome.value,
                reason=reason.value if reason else None,
                job_id=facts.job_id,
                source=facts.source,
                scope=facts.scope,
            )
    log.info("answered %d reported link(s)", len(rows))
    return len(rows)


async def _facts(conn, row, tenants: dict[str, list[str]], profile) -> Facts:
    """Everything the answer is made of, for one report."""
    resolution = resolve(row.url)
    resolved = resolution.result is LeadResult.RESOLVED
    # Both or neither: a stored scope under no source would be a board belonging to nothing.
    source = resolution.source if resolved else None
    scope = resolution.scope if resolved else None
    found = await q.find_job(
        conn,
        urls=url_variants(row.url),
        source=source,
        scope=scope,
        ids=id_tokens(row.url, scope),
    )
    facts = Facts(
        source=source,
        scope=scope,
        swept=source is not None and _swept(source, scope, tenants),
        job_id=found.id if found else None,
    )
    # No profile means no area to judge against and no edition to have been in: nothing was
    # hidden from this reader, nothing was ever matched for them at all.
    if found is None or profile is None:
        return facts
    seen = await q.verdict(conn, user_id=row.user_id, job_id=found.id, profile=profile)
    return replace(
        facts,
        shown=bool(seen.shown),
        retrieved=bool(seen.retrieved),
        in_area=bool(seen.in_area),
    )


def _swept(source: str, scope: str | None, tenants: dict[str, list[str]]) -> bool:
    """Whether the nightly sweep actually covers the board a link named.

    A source with no tenants sweeps its platform whole, so there is nothing to be missing. For
    the rest only an ENABLED board counts: discovery has just written this one as a candidate, and
    a candidate is a proposal, not a sweep.
    """
    if not registry.SOURCES[source].tenant_scoped:
        return True
    return bool(scope) and scope in tenants.get(source, [])
