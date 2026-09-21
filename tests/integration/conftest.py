"""Fixtures for tests that need a real Postgres.

Skipped as a package unless TROUVEUR_TEST_DATABASE_URL is set, so the default suite stays offline
and fast. This is the deliberate exception to "no database in tests": SQL that compiles is not SQL
that runs, and nothing else catches that class of bug.
"""

from __future__ import annotations

import os

import pytest

TEST_DB = os.environ.get("TROUVEUR_TEST_DATABASE_URL")
collect_ignore_glob = [] if TEST_DB else ["test_*.py"]

if TEST_DB:
    os.environ["DATABASE_URL"] = TEST_DB
    os.environ.setdefault("EMBEDDING_PROVIDER", "deterministic")
    # Fixtures carry fixed publication dates and only get older. Left at the production horizon,
    # the seeded corpus would fall out of the embeddable set and most of these tests would pass
    # over nothing at all -- which is how a corpus that embeds no rows looks like a corpus that
    # embeds every row. The horizon is tested on its own terms in test_freshness_horizon.py.
    os.environ.setdefault("RETRIEVAL_HORIZON_DAYS", "36500")
    os.environ.setdefault("ENCRYPTION_KEY", "integration-test-encryption-key")
    # The session cookie is Secure in production, and an HTTP client will not send a Secure cookie
    # back over plain http. Opting out here rather than weakening the default, which is the
    # setting that matters.
    os.environ.setdefault("COOKIE_SECURE", "false")


@pytest.fixture
async def clean_db():
    """Truncate between tests, and rebuild the engine around each test's event loop.

    The engine is a module-level singleton whose pool binds to the loop that created it, and
    pytest-asyncio gives every test a fresh loop. Reusing it across tests fails with a pending-task
    error that has nothing to do with the code under test.
    """
    from trouveur.db import engine as engine_module
    from trouveur.db.engine import connect

    engine_module._engine = None
    async with connect() as conn:
        await conn.exec_driver_sql(
            """
            DO $$ DECLARE t text; BEGIN
              FOR t IN SELECT tablename FROM pg_tables
                       WHERE schemaname='public' AND tablename <> 'alembic_version'
              LOOP EXECUTE format('TRUNCATE %I CASCADE', t); END LOOP; END $$;
            """
        )
    yield
    if engine_module._engine is not None:
        await engine_module._engine.dispose()
        engine_module._engine = None




@pytest.fixture
async def seeded(clean_db, gh_board, aa_listing, aa_detail):
    """A corpus, derived and embedded, plus a user with a profile.

    Shared because "this query executes" only means something against rows that resemble
    production. A query run against empty tables can pass by touching nothing.
    """
    from tests.integration.seed import seed_corpus, seed_scored_match, seed_user

    aa_external_id = await seed_corpus(gh_board, aa_listing, aa_detail)
    user_id, profile = await seed_user(title="Process Engineer", keywords=["Prozessoptimierung"])
    # At least one scored, passing match, so pages that render a job card actually render one.
    # Without it every page test passes over an empty list and the card template is never run.
    job_id = await seed_scored_match(user_id, profile)
    return {
        "user_id": user_id, "profile": profile, "job_id": job_id,
        "aa_external_id": aa_external_id,
    }
