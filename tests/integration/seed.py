"""Building a realistic starting state.

Shared by every integration test, because "this query executes" only means something against
rows that resemble production: a corpus, derived facets, embeddings, a user with a profile and
matches. Seeding less would let a query pass by touching nothing.
"""

from __future__ import annotations


async def seed_corpus(gh_board, aa_listing, aa_detail):
    """Ingest the fixtures the way the pipeline does, and drain every queue."""
    from trouveur.db.engine import connect
    from trouveur.db.queries import ingest as iq
    from trouveur.ingest import workers
    from trouveur.ingest.persist import persist
    from trouveur.models import DocumentKind, RawDocument
    from trouveur.sources.greenhouse import external_id

    docs = [
        RawDocument(
            source="greenhouse", external_id=external_id("beispiel", job["id"]),
            kind=DocumentKind.LISTING, scope="beispiel", payload=job,
        )
        for job in gh_board["jobs"]
    ]
    aa_id = aa_listing["referenznummer"]
    async with connect() as conn:
        await iq.archive_documents(conn, docs)
        await persist(conn, "greenhouse", [d.external_id for d in docs], requires_detail=False)
        await iq.archive_documents(
            conn,
            [RawDocument(source="arbeitsagentur", external_id=aa_id,
                         kind=DocumentKind.LISTING, payload=aa_listing)],
        )
        await persist(conn, "arbeitsagentur", [aa_id], requires_detail=True)
        await iq.archive_documents(
            conn,
            [RawDocument(source="arbeitsagentur", external_id=aa_id,
                         kind=DocumentKind.DETAIL, payload=aa_detail)],
        )
        await persist(conn, "arbeitsagentur", [aa_id], requires_detail=True)
    for drain in (workers.drain_derive, workers.drain_dedup, workers.drain_embed):
        async with connect() as conn:
            await drain(conn)
    return aa_id


async def seed_user(**values):
    from trouveur.db.engine import connect
    from trouveur.db.queries import users as uq
    from trouveur.match.pipeline import profile_from_row

    async with connect() as conn:
        user_id = await uq.create_user(conn, "verifier", "argon2$fake", "v@example.test")
        await uq.save_profile(conn, user_id, {"countries": ["DE", "AT"], **values})
        return user_id, profile_from_row(await uq.get_profile(conn, user_id))




async def seed_scored_match(user_id: int, profile) -> int:
    """Give the user one retrieved, rule-passed, scored match.

    Pages that show a job card render nothing when the list is empty, so without this the card
    template is never executed and every rendering test passes over an empty page.
    """
    import sqlalchemy as sa

    from trouveur.db.engine import connect
    from trouveur.db.queries import match as match_q
    from trouveur.db.schema import job

    async with connect() as conn:
        job_id = (await conn.execute(sa.select(job.c.id).order_by(job.c.id).limit(1))).scalar()
        await match_q.upsert_matches(
            conn,
            [{
                "user_id": user_id, "job_id": job_id, "profile_version": profile.version,
                "retrieval_score": 0.9, "dense_rank": 1, "lexical_rank": 1,
            }],
        )
        await match_q.apply_rule_verdicts(
            conn,
            [{
                "user_id": user_id, "job_id": job_id,
                "rule_verdict": "pass", "rule_reason": "passed rules",
            }],
        )
        await match_q.apply_scores(
            conn, user_id,
            [{
                "job_id": job_id, "score": 92, "reason": "strong match",
                "red_flags": ["probe flag"], "profile_version": profile.version,
            }],
        )
    return job_id
