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

import json
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from trouveur.db.queries import location
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
    """Write every newly-seen board as a disabled candidate, for its trial sweep to judge."""
    if not sources:
        return 0
    result = await conn.execute(sa.text(_PROMOTE), {"sources": list(sources)})
    return result.rowcount or 0


# The verdict on a trial: a board holding an open posting inside this area is swept nightly from
# now on, and no human saw it happen.
#
# Run once per area the readers actually asked for, because the filter's parameters are per area.
# It is `location.SQL` verbatim and not a second opinion: a board judged by a rule of its own
# would turn on boards whose postings the matcher then filters out, and turn away boards whose
# postings it would have shown. AGENTS.md calls that re-deriving downstream.
#
# Idempotent and not keyed on "swept tonight", which is why it needs no handle on the run: a board
# already enabled is excluded by the WHERE, and one whose trial landed in a run that died before
# the verdict is judged by the next run instead of being lost. `dropped_at IS NULL` and
# `decided_at IS NULL` keep an operator's decision, whichever way they made it.
#
# The filter is necessary and not sufficient here, and this is the one caller where that is so.
# It admits a posting nothing could place, which is right when being wrong costs one row ranked
# too high and wrong when it costs a board swept every night for ever. So the trial asks for a
# posting that was PLACED too: a country, a country it may be in, or a role derived remote.
#
# Not applied to the filter's twin branch, a posting whose source named no place at all: that is
# an aggregator blanking a field, and a company's own board is all discovery ever resolves to.
#
# Only candidates, so this can never widen the crawl beyond what discovery proposed, and never
# narrows it: nothing here writes `enabled = false`.
_ENABLE_WITH_LOCAL_JOBS = f"""
UPDATE source_tenant t
SET enabled = true
WHERE t.origin = 'discovered'
  AND NOT t.enabled
  AND t.dropped_at IS NULL
  AND t.decided_at IS NULL
  AND EXISTS (
      SELECT 1
      FROM job j
      JOIN job_facet f ON f.job_id = j.id
      WHERE j.source = t.source
        AND j.scope = t.scope
        AND j.closed_at IS NULL
        AND (
            cardinality(f.countries) > 0
            OR cardinality(f.unplaced_countries) > 0
            OR f.work_mode = 'remote'
        )
        AND {location.SQL}
  )
"""


async def enable_boards_with_local_jobs(conn: AsyncConnection, area: object) -> int:
    """Switch on every candidate board holding an open posting inside `area`. Returns how many."""
    result = await conn.execute(
        sa.text(_ENABLE_WITH_LOCAL_JOBS), location.params(area)
    )
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


# Per origin, and per list for an imported one. A board two origins named counts for both: the
# question is what each origin found, not which found it first. Only discovered boards, so a list
# naming boards we already swept by hand brings none.
_BY_ORIGIN = """
WITH leads AS (
    SELECT origin::text AS origin, coalesce(list_name, '') AS list_name, count(*) AS leads
    FROM discovery_lead
    GROUP BY 1, 2
),
boards AS (
    SELECT DISTINCT l.origin::text AS origin, coalesce(l.list_name, '') AS list_name,
           t.source, t.scope, t.enabled, h.trial_at IS NOT NULL AS tried
    FROM discovery_lead l
    JOIN source_tenant t ON t.source = l.source AND t.scope = l.scope
    LEFT JOIN source_scope_health h ON h.source = t.source AND h.scope = t.scope
    WHERE l.result = 'resolved' AND t.origin = 'discovered'
)
SELECT d.origin, d.list_name, d.leads,
       count(b.origin) AS boards,
       count(b.origin) FILTER (WHERE b.tried) AS tried,
       count(b.origin) FILTER (WHERE b.enabled) AS kept
FROM leads d
LEFT JOIN boards b ON b.origin = d.origin AND b.list_name = d.list_name
GROUP BY d.origin, d.list_name, d.leads
ORDER BY d.leads DESC, d.origin, d.list_name
"""


async def by_origin(conn: AsyncConnection) -> list[sa.Row]:
    """Leads, boards proposed, boards tried and boards kept, per origin and list."""
    return list(await conn.execute(sa.text(_BY_ORIGIN)))


async def payload_values(conn: AsyncConnection, *, origin: str, key: str) -> set[str]:
    """Every value one origin stored under `key` in its leads' payload."""
    rows = await conn.execute(
        sa.text(
            "SELECT DISTINCT payload->>:key AS value FROM discovery_lead "
            "WHERE origin = CAST(:origin AS lead_origin)"
        ),
        {"origin": origin, "key": key},
    )
    return {row.value for row in rows if row.value}


# Whether we already hold a job another site showed us, by company, title and place, open copies
# only. The fold is applied to both sides here and nowhere else, so the two spellings cannot drift
# apart. It drops a gender tag (`(m/w/d)`, `w/m/d`) from a title and a legal form from a company,
# which is where one employer's job differs between its board and an aggregator; everything else
# must match exactly.
#
# `[\W_]` and not `[[:alnum:]]`: `sa.text` would read `:alnum` as a bind parameter.
_GENDER_TAG = r"\(?\m[mwfdix]\s*/\s*[mwfdix](\s*/\s*[mwfdix])?\M\)?"
_LEGAL_FORM = r"\m(gmbh|mbh|ag|se|kg|kgaa|co|inc|ltd|llc|plc|sa|sas|srl|spa|bv|nv|ab|as|oy)\M"


def _fold(expression: str) -> str:
    return rf"btrim(regexp_replace({expression}, '[\W_]+', ' ', 'g'))"


def _title(column: str) -> str:
    return _fold(f"regexp_replace(lower({column}), '{_GENDER_TAG}', ' ', 'g')")


def _company(column: str) -> str:
    stripped = f"regexp_replace({_fold(f'lower({column})')}, '{_LEGAL_FORM}', ' ', 'g')"
    return rf"btrim(regexp_replace({stripped}, '\s+', ' ', 'g'))"


# A town on the card is matched against our towns near it, a country only when the card names no
# town. A posting not yet derived has no facets and is not found, which errs the same way.
_HELD = f"""
WITH wanted AS (
    SELECT w.key, {_company("w.company")} AS company, {_title("w.title")} AS title,
           w.place_ids, w.countries
    FROM jsonb_to_recordset(CAST(:wanted AS jsonb))
         AS w(key bigint, company text, title text, place_ids integer[], countries text[])
)
SELECT DISTINCT ON (w.key) w.key, j.id
FROM wanted w
JOIN job j ON {_company("j.company")} = w.company AND {_title("j.title")} = w.title
JOIN job_facet f ON f.job_id = j.id
WHERE j.closed_at IS NULL AND w.company <> '' AND w.title <> ''
  AND (f.place_ids && w.place_ids
       OR (cardinality(w.place_ids) = 0
           AND (f.countries && w.countries OR f.unplaced_countries && w.countries)))
ORDER BY w.key, j.id
"""


async def held_jobs(
    conn: AsyncConnection, wanted: dict[int, tuple[str, str, list[int], list[str]]]
) -> dict[int, int]:
    """Our open copy of each wanted `key: (company, title, place_ids, countries)`."""
    if not wanted:
        return {}
    rows = [
        {"key": key, "company": company, "title": title, "place_ids": ids, "countries": codes}
        for key, (company, title, ids, codes) in wanted.items()
    ]
    result = await conn.execute(sa.text(_HELD), {"wanted": json.dumps(rows)})
    return {row.key: row.id for row in result}
