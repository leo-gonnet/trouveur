"""SQL for discovery: mining the archive for links, and what the resolver made of them.

Both stages are driven by a version column rather than by `work_item`. The queue exists for work
that can FAIL -- a detail fetch, an embedding call -- and carries claims, attempts and backoff to
prove it. Mining and resolving are pure functions of rows we already hold: they cannot fail
transiently, there is nothing to retry, and a second pass over the same row is free. So the queue
is a WHERE clause on the stage's own version, exactly as `job_facet.derive_version` is what the
derive refill reads -- and with no cursor to carry, because a row's version is written in the same
transaction that read it, so it leaves the set and the next page starts where this one stopped.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from trouveur.db.schema import discovery_lead, job

# One posting with everything a lead needs, its raw payloads included.
#
# The payloads are joined in rather than read from `job.description`, and that is the whole point
# of mining here: `html_to_text` keeps an advert's words and drops its `href`, so the link behind
# "Apply here" survives only in the archive. Both documents, because a source with a detail phase
# puts the description in the second one.
#
# Below the version, in id order, served by job_mined_idx. No cursor parameter: every row this
# returns has its version written in the same transaction, so it leaves the set and the next page
# starts where this one stopped. A keyset cursor would be a parameter nobody can vary.
_MINING_BATCH = """
SELECT j.id,
       j.source,
       j.scope,
       j.url,
       j.title,
       j.company,
       j.location_text,
       coalesce(l.payload::text, '') || ' ' || coalesce(d.payload::text, '') AS payload
FROM job j
LEFT JOIN source_document l ON l.id = j.listing_document_id
LEFT JOIN source_document d ON d.id = j.detail_document_id
WHERE j.mined_version < :version
ORDER BY j.id
LIMIT :chunk
"""


async def mining_batch(conn: AsyncConnection, *, version: int, chunk: int) -> list[sa.Row]:
    """One page of postings not yet mined at `version`."""
    return list(await conn.execute(sa.text(_MINING_BATCH), {"version": version, "chunk": chunk}))


async def record_mined(conn: AsyncConnection, job_ids: Sequence[int], version: int) -> int:
    """Mark postings mined. Written for the whole page in one statement, after its leads land, so
    an interrupted batch is mined again rather than skipped."""
    if not job_ids:
        return 0
    result = await conn.execute(
        job.update().where(job.c.id.in_(list(job_ids))).values(mined_version=version)
    )
    return result.rowcount or 0


# One multi-VALUES INSERT carries a bind parameter per column per row, and asyncpg refuses a
# statement above 32767 of them -- at 14 columns that is ~2340 leads in one call. Mining bounds
# the POSTINGS it reads (MINE_BATCH) and nothing bounds the links inside one, so a page of
# link-heavy descriptions reaches it; and it would raise inside the transaction that also writes
# `record_mined`, so the batch would roll back, stay below MINE_VERSION, and fail identically for
# ever. Chunked well under the cap rather than at it, so adding a column cannot bring it back.
_INSERT_CHUNK = 500


async def insert_leads(conn: AsyncConnection, rows: Sequence[dict]) -> int:
    """Add leads, ignoring any link this origin has already filed.

    DO NOTHING rather than an update: the stored row holds the FIRST time we saw the link, and a
    second sighting of the same URL carries no new information -- the lead is about the link.

    `index_where` repeats the unique index's own predicate, which is how Postgres is told which
    index this conflict is about. Without it the statement is rejected at run time with "there is
    no unique or exclusion constraint matching the ON CONFLICT specification" -- the index is
    partial (see the migration), so nothing matches a bare `(origin, url)` specification.

    It is `sa.text` and NOT `discovery_lead.c.url != ""`, which is the same predicate and reads
    better. Written that way the `''` compiles to a BIND PARAMETER -- `WHERE url != $12` -- and
    Postgres cannot match a partial index against a parameterised predicate, so the statement is
    rejected with that same message. Whether SQLAlchemy inlines the empty string or binds it
    depends on the compilation path, so the failure was intermittent: small statements passed and
    the bigger ones mining produces did not, which is the worst possible version of this bug.
    """
    rows = list(rows)
    if not rows:
        return 0
    written = 0
    for start in range(0, len(rows), _INSERT_CHUNK):
        stmt = pg_insert(discovery_lead).values(rows[start : start + _INSERT_CHUNK])
        result = await conn.execute(
            stmt.on_conflict_do_nothing(
                index_elements=[discovery_lead.c.origin, discovery_lead.c.url],
                index_where=sa.text("url <> ''"),
            )
        )
        written += result.rowcount or 0
    return written


_STALE_LEADS = """
SELECT id, url
FROM discovery_lead
WHERE resolve_version < :version
ORDER BY id
LIMIT :chunk
"""


async def leads_to_resolve(
    conn: AsyncConnection, *, version: int, chunk: int
) -> list[sa.Row]:
    """One page of leads resolved below `version` -- the whole table after a bump."""
    return list(await conn.execute(sa.text(_STALE_LEADS), {"version": version, "chunk": chunk}))


async def write_resolutions(conn: AsyncConnection, rows: Sequence[dict]) -> int:
    """Store what the rules made of each lead. One statement for the page, by id.

    Returns the rows sent rather than a rowcount: this is an executemany, and asyncpg reports -1
    for one of those -- a number that reads as "nothing moved" to every caller that tests it.
    Every row is keyed on a primary key that was just read, so the count is known either way.
    """
    if not rows:
        return 0
    await conn.execute(
        discovery_lead.update()
        .where(discovery_lead.c.id == sa.bindparam("lead_id"))
        .values(
            result=sa.bindparam("result"),
            source=sa.bindparam("source"),
            scope=sa.bindparam("scope"),
            host=sa.bindparam("host"),
            resolve_version=sa.bindparam("resolve_version"),
            resolved_at=sa.func.now(),
        ),
        list(rows),
    )
    return len(rows)


# Every board our leads resolved to, as a disabled candidate. `sources` is passed in because only
# the registry knows which sources have tenants at all: a row for a global source would be swept
# by nothing and would sit in the crawl set for ever looking like work.
#
# `note` is left empty on purpose: `origin = 'discovered'` already says where the board came from,
# and a constant in `note` would sit where the reason a HUMAN enabled or dropped it belongs.
#
# DO NOTHING is load-bearing: discovery must never touch a row an operator has decided about --
# not a board they enabled, and not one they tried and dropped, which would come back as a
# candidate on the next pass and be rejected again.
_PROMOTE = """
INSERT INTO source_tenant (source, scope, enabled, origin)
SELECT DISTINCT l.source, l.scope, false, CAST('discovered' AS tenant_origin)
FROM discovery_lead l
WHERE l.result = 'resolved'
  AND l.source = ANY(CAST(:sources AS text[]))
  AND l.scope IS NOT NULL
ON CONFLICT (source, scope) DO NOTHING
"""


async def promote_candidates(conn: AsyncConnection, sources: Sequence[str]) -> int:
    """Write every newly-seen board as a disabled candidate, for a human to promote (task 04)."""
    if not sources:
        return 0
    result = await conn.execute(sa.text(_PROMOTE), {"sources": list(sources)})
    return result.rowcount or 0


# The hosts we could not read, which is the list of platforms worth a rule. Capped because the
# tail is one-off company domains; the count of the whole thing is in `totals`.
_UNREAD_HOSTS = """
SELECT host, count(*) AS leads
FROM discovery_lead
WHERE result = 'unknown_host' AND host IS NOT NULL
GROUP BY host
ORDER BY leads DESC, host
LIMIT :limit
"""


async def unread_hosts(conn: AsyncConnection, *, limit: int = 20) -> list[sa.Row]:
    return list(await conn.execute(sa.text(_UNREAD_HOSTS), {"limit": limit}))


_TOTALS = """
SELECT count(*) AS leads,
       count(*) FILTER (WHERE result = 'resolved') AS resolved,
       count(*) FILTER (WHERE result = 'unknown_host') AS unread,
       count(DISTINCT host) FILTER (WHERE result = 'unknown_host') AS unread_hosts,
       count(*) FILTER (WHERE resolve_version < :version) AS awaiting_resolve
FROM discovery_lead
"""

_UNMINED = """
SELECT count(*) AS unmined FROM job WHERE mined_version < :version
"""


async def totals(conn: AsyncConnection, *, resolve_version: int, mine_version: int) -> dict:
    """One row for the report's header, plus what each stage still has left to do.

    The backlogs are in the same answer as the counts on purpose: a leads panel reading zero means
    something different when a hundred thousand postings have not been mined yet.
    """
    row = (await conn.execute(sa.text(_TOTALS), {"version": resolve_version})).one()
    unmined = await conn.scalar(sa.text(_UNMINED), {"version": mine_version})
    return {
        "leads": int(row.leads),
        "resolved": int(row.resolved),
        "unread": int(row.unread),
        "unread_hosts": int(row.unread_hosts or 0),
        "awaiting_resolve": int(row.awaiting_resolve),
        "unmined_jobs": int(unmined or 0),
    }
