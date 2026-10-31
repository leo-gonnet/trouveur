"""The work queue against a real Postgres: what closing, requeueing and a failing source do."""

from __future__ import annotations

from tests.integration.seed import seed_corpus


async def _items(conn) -> dict[tuple[int, str], tuple[int, bool]]:
    rows = await conn.exec_driver_sql(
        "SELECT job_id, kind::text, attempts, not_before > now() + interval '10 minutes' "
        "FROM work_item"
    )
    return {(row[0], row[1]): (row[2], row[3]) for row in rows}


async def _jobs_by_source(conn) -> dict[str, list[int]]:
    """And an empty queue, so a test's own items are the only ones."""
    await conn.exec_driver_sql("DELETE FROM work_item")
    rows = await conn.exec_driver_sql("SELECT source, id FROM job ORDER BY id")
    found: dict[str, list[int]] = {}
    for source, job_id in rows:
        found.setdefault(source, []).append(job_id)
    return found


class _Source:
    def __init__(self, error: Exception | None) -> None:
        self.error = error
        self.calls = 0

    async def fetch_detail(self, client, external_id):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return None


async def test_a_source_failing_in_a_burst_is_paused_without_spending_its_postings_attempts(
    clean_db, gh_board, aa_listing, aa_detail, monkeypatch
):
    """On 09-22 a 403 burst spent all five attempts of 1,500 Workday postings, one by one."""
    from trouveur.db.engine import connect
    from trouveur.ingest import workers
    from trouveur.sources.errors import FetchError
    from trouveur.work import WorkKind, enqueue

    await seed_corpus(gh_board, aa_listing, aa_detail)
    monkeypatch.setattr(workers, "SOURCE_BURST", 1)
    async with connect() as conn:
        jobs = await _jobs_by_source(conn)
        await enqueue(conn, WorkKind.DETAIL, [i for ids in jobs.values() for i in ids], "1")

    failing = _Source(FetchError("HTTP 403 from the board."))
    healthy = _Source(None)
    async with connect() as conn:
        await workers.drain_detail(
            conn, None, {"greenhouse": failing, "arbeitsagentur": healthy}, limit=10
        )
        items = await _items(conn)

    assert len(jobs["greenhouse"]) == 2 and failing.calls == 1
    assert sorted(items[(i, "detail")] for i in jobs["greenhouse"]) == [(0, True), (1, True)]
    assert healthy.calls == 1, "one source failing must not hold back another"
    assert (jobs["arbeitsagentur"][0], "detail") not in items


async def test_one_unexpected_error_charges_its_posting_and_keeps_the_rest_of_the_batch(
    clean_db, gh_board, aa_listing, aa_detail
):
    """Fetches run as one gathered batch, so an exception that is not a SourceError used to throw
    away every fetch of the batch, up to 400 requests, and leave their items claimed."""
    from trouveur.db.engine import connect
    from trouveur.ingest import workers
    from trouveur.work import WorkKind, enqueue

    await seed_corpus(gh_board, aa_listing, aa_detail)
    async with connect() as conn:
        jobs = await _jobs_by_source(conn)
        await enqueue(conn, WorkKind.DETAIL, [i for ids in jobs.values() for i in ids], "1")

    broken = _Source(ValueError("not enough values to unpack"))
    healthy = _Source(None)
    async with connect() as conn:
        await workers.drain_detail(
            conn, None, {"greenhouse": broken, "arbeitsagentur": healthy}, limit=10
        )
        items = await _items(conn)

    assert sorted(items[(i, "detail")][0] for i in jobs["greenhouse"]) == [1, 1]
    assert (jobs["arbeitsagentur"][0], "detail") not in items


async def test_closing_a_posting_drops_its_detail_and_embed_work_but_not_its_derive(
    clean_db, gh_board, aa_listing, aa_detail
):
    from trouveur.db.engine import connect
    from trouveur.db.queries import ingest as iq
    from trouveur.work import WorkKind, enqueue

    await seed_corpus(gh_board, aa_listing, aa_detail)
    async with connect() as conn:
        closing, staying = (await _jobs_by_source(conn))["greenhouse"]
        for kind in (WorkKind.DETAIL, WorkKind.EMBED, WorkKind.DERIVE):
            await enqueue(conn, kind, [closing, staying], "999")
        await iq.close_retired(conn, [closing])
        left = set(await _items(conn))

    assert left == {
        (closing, "derive"),
        (staying, "detail"), (staying, "embed"), (staying, "derive"),
    }


async def test_requeue_revives_parked_items_and_drops_those_of_closed_postings(
    clean_db, gh_board, aa_listing, aa_detail
):
    from trouveur.db.engine import connect
    from trouveur.work import WorkKind, enqueue, requeue_parked
    from trouveur.work.queue import MAX_ATTEMPTS

    await seed_corpus(gh_board, aa_listing, aa_detail)
    async with connect() as conn:
        closed, live = (await _jobs_by_source(conn))["greenhouse"]
        await enqueue(conn, WorkKind.DETAIL, [closed, live], "1")
        await conn.exec_driver_sql(
            f"UPDATE work_item SET attempts = {MAX_ATTEMPTS}, "
            "not_before = now() + interval '30 days', last_error = 'HTTP 403'"
        )
        # Closed before this change existed, so its item is still queued.
        await conn.exec_driver_sql(f"UPDATE job SET closed_at = now() WHERE id = {closed}")

        assert await requeue_parked(conn, WorkKind.DETAIL) == (1, 1)
        assert await _items(conn) == {(live, "detail"): (0, False)}


async def test_a_posting_is_ready_once_the_match_has_nothing_left_to_wait_for(
    clean_db, gh_board, aa_listing, aa_detail
):
    """A parked detail must not hide its posting for good."""
    from trouveur.db.engine import connect
    from trouveur.work import MAX_ATTEMPTS, WorkKind, claim, complete, enqueue, fail

    await seed_corpus(gh_board, aa_listing, aa_detail)
    async with connect() as conn:
        fetched, failing = (await _jobs_by_source(conn))["greenhouse"]
        await conn.exec_driver_sql("UPDATE job SET ready_at = NULL")
        await enqueue(conn, WorkKind.DETAIL, [fetched, failing], "1")
        await enqueue(conn, WorkKind.DEDUP, [fetched], "1")
        items = {item.job_id: item.id for item in await claim(conn, WorkKind.DETAIL, 2, "test")}
        await complete(conn, [items[fetched]])
        await fail(conn, [items[failing]], "HTTP 500 from the board.")
        ready = dict((await conn.exec_driver_sql("SELECT id, ready_at FROM job")).all())
        assert ready[fetched] is not None, "dedup is not something the match reads"
        assert ready[failing] is None

        await conn.exec_driver_sql(
            "UPDATE work_item SET attempts = $1 - 1 WHERE job_id = $2", (MAX_ATTEMPTS, failing)
        )
        await fail(conn, [items[failing]], "HTTP 500 from the board.")
        parked = await conn.exec_driver_sql("SELECT ready_at FROM job WHERE id = $1", (failing,))
        assert parked.scalar_one() is not None


async def test_a_re_derive_does_not_make_a_posting_new_again(
    clean_db, gh_board, aa_listing, aa_detail
):
    """Otherwise a DERIVE_VERSION bump puts the whole week back into the next run."""
    from trouveur.db.engine import connect
    from trouveur.ingest import workers
    from trouveur.work import WorkKind, refill

    await seed_corpus(gh_board, aa_listing, aa_detail)
    async with connect() as conn:
        await conn.exec_driver_sql("UPDATE job SET ready_at = now() - interval '3 days'")
        before = dict((await conn.exec_driver_sql("SELECT id, ready_at FROM job")).all())
        await refill(conn, WorkKind.DERIVE, "999")
        assert await workers.drain_derive(conn) > 0
        after = dict((await conn.exec_driver_sql("SELECT id, ready_at FROM job")).all())
    assert after == before
