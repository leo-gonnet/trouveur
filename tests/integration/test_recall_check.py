"""The recall check against real search: its placed rank must be the rank search gives."""

from __future__ import annotations

import sqlalchemy as sa

from trouveur.config import get_settings
from trouveur.db.engine import connect
from trouveur.db.queries import freshness
from trouveur.db.queries import match as match_q
from trouveur.db.queries import users as users_q
from trouveur.match import expand, recall, retrieve
from trouveur.match.pipeline import profile_from_row


async def _stored_rank(user_id: int, job_id: int) -> int | None:
    async with connect() as conn:
        return await conn.scalar(
            sa.text(
                "SELECT n.rank FROM recall_needle n JOIN recall_check c ON c.id = n.check_id "
                "WHERE c.user_id = :user AND n.job_id = :job ORDER BY c.id DESC LIMIT 1"
            ),
            {"user": user_id, "job": job_id},
        )


async def test_a_recent_kept_job_gets_the_rank_search_gives_it_and_keeps_it_once_gone(seeded):
    """Inside the window the job is in tonight's arms already, so the placed rank must equal the
    fused rank a run computes. Then the job closes and its vector is deleted, as happens to every
    kept job sooner or later: the check must still place it, at the same rank."""
    user_id, job_id = seeded["user_id"], seeded["job_id"]
    settings = get_settings()
    async with connect() as conn:
        await conn.exec_driver_sql(
            "UPDATE user_job_match SET state = 'saved' WHERE user_id = $1 AND job_id = $2",
            (user_id, job_id),
        )
        profile = profile_from_row(await users_q.get_profile(conn, user_id))
        expansion = await match_q.get_query_expansion(
            conn, user_id, profile.version, recall.QUERY_EXPANSION_VERSION
        )
        queries = expansion.queries if expansion else expand.deterministic_queries(profile)
        adverts = expansion.adverts if expansion else []
        fresh_since = freshness.fresh_since(settings.retrieval_horizon_days)
        arms = await retrieve.retrieve_arms(
            conn, profile, queries, adverts, fresh_since=fresh_since, seen_since=fresh_since
        )
    fused = [found for found, _ in retrieve.fuse(arms, retrieve.FUSED_LIMIT)]
    assert job_id in fused, "the fixture's scored job must be one search returns"

    await recall.check_user(user_id, settings)
    assert await _stored_rank(user_id, job_id) == fused.index(job_id) + 1

    async with connect() as conn:
        await conn.exec_driver_sql("UPDATE job SET closed_at = now() WHERE id = $1", (job_id,))
        await conn.exec_driver_sql("DELETE FROM job_embedding WHERE job_id = $1", (job_id,))
    await recall.check_user(user_id, settings)
    assert await _stored_rank(user_id, job_id) == fused.index(job_id) + 1
