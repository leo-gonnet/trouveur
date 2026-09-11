"""Every page renders against real rows.

A template is code that only runs when rendered. Nothing else in the suite executes one, so a
template referencing a column that was renamed raises at request time and is invisible to every
other test -- and this codebase has renamed several (`country` to `countries`, `cost_eur` to
`cost_usd`, the whole per-user overlay moving off the job row).

Driven over ASGI rather than a browser: with no build step and HTMX-only interactivity, this
reaches the same code a browser would for a fraction of the maintenance.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
import sqlalchemy as sa

from trouveur.db.engine import connect
from trouveur.db.queries import match as match_q
from trouveur.db.queries import users as users_q
from trouveur.db.schema import app_user, job
from trouveur.web.app import app

PASSWORD = "integration-test-password"


@pytest.fixture
async def client(seeded):
    """A logged-in client, authenticated the way a browser is: by POSTing the login form."""
    from trouveur.web.auth import hash_password

    async with connect() as conn:
        user = await users_q.get_user(conn, seeded["user_id"])
        await conn.execute(
            sa.update(app_user)
            .where(app_user.c.id == seeded["user_id"])
            .values(password_hash=hash_password(PASSWORD))
        )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as session:
        response = await session.post(
            "/login", data={"username": user.username, "password": PASSWORD}
        )
        assert response.status_code == 303, "login form did not authenticate"
        yield session


async def _public_id() -> str:
    async with connect() as conn:
        return str((await conn.execute(sa.select(job.c.public_id).limit(1))).scalar())


@pytest.mark.parametrize(
    "path",
    ["/recommendations", "/search", "/dashboard", "/profile", "/settings", "/admin"],
)
async def test_page_renders(client, path):
    response = await client.get(path)
    assert response.status_code == 200, f"{path} returned {response.status_code}"
    assert "<main>" in response.text


async def test_pages_that_show_a_job_card_actually_render_one(client):
    """Otherwise every rendering assertion passes over an empty list.

    A page test that never executes the card template proves only that the query returned; the
    macro is where a field the query does not select surfaces.
    """
    for path in ("/recommendations", "/search"):
        body = (await client.get(path)).text
        assert "job-title" in body, f"{path} rendered no job card, so the macro was not exercised"
        assert "strong match" in body or path == "/search"


async def test_job_detail_renders(client):
    response = await client.get(f"/job/{await _public_id()}")
    assert response.status_code == 200
    assert "Derived facets" in response.text


async def test_unknown_job_is_a_404_not_a_crash(client):
    response = await client.get("/job/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


async def test_search_serves_a_fragment_to_htmx_and_a_page_to_a_browser(client):
    page = await client.get("/search?q=ingenieur")
    fragment = await client.get("/search?q=ingenieur", headers={"HX-Request": "true"})
    assert "<main>" in page.text
    # The fragment swaps into #results; returning the whole document would nest a page inside it.
    assert "<main>" not in fragment.text


async def test_search_shows_a_posting_that_the_rules_rejected(client, seeded):
    """Search is the only view of what was actually collected; recommendations is the strict page.

    Asserted here rather than only in the query layer because the distinction is easy to erase
    from a template, and erasing it makes the corpus invisible.
    """
    async with connect() as conn:
        job_id = (await conn.execute(sa.select(job.c.id).limit(1))).scalar()
        await match_q.apply_rule_verdicts(
            conn,
            [{
                "user_id": seeded["user_id"], "job_id": job_id,
                "rule_verdict": "reject", "rule_reason": "deal-breaker",
            }],
        )
        title = (
            await conn.execute(sa.select(job.c.title).where(job.c.id == job_id))
        ).scalar()

    response = await client.get("/search")
    assert title[:20] in response.text


async def test_a_stored_api_key_is_never_rendered(client, seeded):
    """The settings page must be able to say *which* key is stored without disclosing it.

    Checked over the rendered body rather than by reading the template, because the leak that
    matters is a context value that reaches any page, not one particular field.
    """
    from trouveur.crypto import encrypt, fingerprint

    secret = "sk-or-v1-integration-test-secret-value"
    async with connect() as conn:
        await users_q.save_credential(
            conn, seeded["user_id"],
            api_key_encrypted=encrypt(secret), api_key_fingerprint=fingerprint(secret),
            model="probe/model", provider_pin=None, monthly_budget_usd=Decimal("5"),
        )

    body = (await client.get("/settings")).text
    assert secret not in body
    assert secret[-8:] not in body
    assert fingerprint(secret) in body, "the page must identify which key is stored"


async def test_saving_a_scoring_field_bumps_the_profile_version(client, seeded):
    """And a presentation-only field must not, because a bump bills the user for a re-score."""
    async with connect() as conn:
        before = (await users_q.get_profile(conn, seeded["user_id"])).version

    form = {
        "title": "Head of Operations", "years_experience": "9", "objectives": "",
        "languages": "de", "must_have": "", "deal_breakers": "", "keywords": "lean",
        "countries": "DE,AT", "cities": "", "work_modes": "", "seniorities": "",
        "employment_types": "", "min_salary_eur_year": "60000",
        "retrieval_limit": "400", "rerank_limit": "150",
    }
    assert (await client.post("/profile", data=form)).status_code == 303
    async with connect() as conn:
        after_scoring = (await users_q.get_profile(conn, seeded["user_id"])).version
    assert after_scoring > before

    presentation_only = {**form, "rerank_limit": "200"}
    assert (await client.post("/profile", data=presentation_only)).status_code == 303
    async with connect() as conn:
        after_presentation = (await users_q.get_profile(conn, seeded["user_id"])).version
    assert after_presentation == after_scoring


async def test_logout_clears_the_session(client):
    assert (await client.post("/logout")).status_code == 303
    response = await client.get("/recommendations")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")
