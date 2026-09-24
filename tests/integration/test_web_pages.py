"""Every page renders against real rows.

A template is code that only runs when rendered. Nothing else in the suite executes one, so a
template referencing a column that was renamed raises at request time and is invisible to every
other test -- and this codebase has renamed several (`country` to `countries`, `cost_eur` to
`cost_usd`, the whole per-user overlay moving off the job row).

Driven over ASGI rather than a browser: with no build step and HTMX-only interactivity, this
reaches the same code a browser would for a fraction of the maintenance.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
import sqlalchemy as sa

from tests.integration.test_editions import _all_match_ids, _publish
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
    ["/recommendations", "/search", "/dashboard", "/profile", "/settings", "/admin",
     "/how-it-works"],
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


async def test_search_shows_a_posting_the_user_never_retrieved(client, seeded):
    """Search is the only view of what was actually collected; recommendations is the strict page.

    Asserted here rather than only in the query layer because the distinction is easy to erase
    from a template, and erasing it makes the corpus invisible.
    """
    from trouveur.db.schema import user_job_match

    async with connect() as conn:
        await conn.execute(
            user_job_match.delete().where(user_job_match.c.user_id == seeded["user_id"])
        )
        title = (
            await conn.execute(sa.select(job.c.title).where(job.c.id == seeded["job_id"]))
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
    assert 'class="banner warn"' not in body, "the no-key banner must go once a key is stored"


async def test_the_no_key_banner_is_on_every_page_until_a_key_is_set(client, seeded):
    for path in ("/recommendations", "/search", "/profile"):
        body = (await client.get(path)).text
        assert 'class="banner warn"' in body, path
    body = (await client.get("/recommendations")).text
    assert "Add a key in Settings." in body, "the banner has to say what to do about it"


async def test_the_page_is_a_reading_list_with_no_console_on_it(client, seeded):
    """Everything above the results was pipeline jargon the reader could not act on.

    The last run's candidate and cost counts are diagnostics and live on the dashboard; the
    button is gone because matching is started by the scan, a profile change and a new key.
    """
    body = (await client.get("/recommendations")).text
    assert "Match now" not in body
    assert "candidates" not in body
    assert "retrieved</" not in body, "lifetime pipeline counts belong on the dashboard"
    assert "<h1>" not in body, "the nav already says which page this is"


async def test_the_scoring_switch_pauses_spending_and_says_so(client, seeded):
    """An unticked checkbox posts nothing at all, which is what "off" looks like on the wire."""
    assert (await client.post("/settings/scoring", data={})).status_code == 303
    async with connect() as conn:
        assert (await users_q.get_profile(conn, seeded["user_id"])).scoring_enabled is False
    assert "Scoring is off" in (await client.get("/recommendations")).text

    assert (
        await client.post("/settings/scoring", data={"scoring_enabled": "on"})
    ).status_code == 303
    async with connect() as conn:
        assert (await users_q.get_profile(conn, seeded["user_id"])).scoring_enabled is True
    assert "Scoring is off" not in (await client.get("/recommendations")).text


async def test_the_pause_never_bills_a_re_score(client, seeded):
    """It is a cost control, not part of what a good match is: a bump would bill the user."""
    async with connect() as conn:
        before = (await users_q.get_profile(conn, seeded["user_id"])).version
    assert (await client.post("/settings/scoring", data={})).status_code == 303
    async with connect() as conn:
        assert (await users_q.get_profile(conn, seeded["user_id"])).version == before


async def test_saving_a_scoring_field_bumps_the_profile_version(client, seeded):
    """And a presentation-only field must not, because a bump bills the user for a re-score."""
    async with connect() as conn:
        before = (await users_q.get_profile(conn, seeded["user_id"])).version

    form = {
        "title": "Head of Operations", "years_experience": "9", "objectives": "",
        "languages": "de", "must_have": "", "keywords": "lean",
        "countries": "DE\nAT", "cities": "", "work_modes": ["remote", "hybrid"],
        "min_salary_eur_year": "60000", "confirm": "yes",
    }
    assert (await client.post("/profile", data=form)).status_code == 303
    async with connect() as conn:
        after_scoring = (await users_q.get_profile(conn, seeded["user_id"])).version
    assert after_scoring > before

    assert (await client.post("/settings/scoring", data={})).status_code == 303
    async with connect() as conn:
        row = await users_q.get_profile(conn, seeded["user_id"])
    assert row.version == after_scoring, "a cost setting must not bill a re-score"
    assert row.scoring_enabled is False


async def test_a_scoring_change_queues_a_re_score_without_erasing_anything(client, seeded):
    """The Profile page promises a re-score, and it used to buy one by destroying the evidence.

    `reset_scores` nulled llm_score and scored_at on every row, which emptied the user's whole
    Recommendations history. The version comparison alone does the job now, because retrieval no
    longer re-stamps the version a posting was scored under.
    """
    from trouveur.db.schema import user_job_match

    async with connect() as conn:
        await match_q.set_state(conn, seeded["user_id"], seeded["job_id"], "saved")
        before = (await conn.execute(
            user_job_match.select().where(user_job_match.c.user_id == seeded["user_id"])
        )).one()
    assert before.llm_score == 92

    form = {
        "title": "Something else entirely", "years_experience": "9",
        "countries": "DE\nAT", "confirm": "yes",
    }
    assert (await client.post("/profile", data=form)).status_code == 303

    async with connect() as conn:
        profile = await users_q.get_profile(conn, seeded["user_id"])
        after = (await conn.execute(
            user_job_match.select().where(user_job_match.c.user_id == seeded["user_id"])
        )).one()
        pending = await match_q.pending_rerank(conn, seeded["user_id"], profile.version, 100)
    assert after.llm_score == 92, "a profile edit destroyed the score it was meant to replace"
    assert after.state == "saved", "a profile edit must not touch the user's own decisions"
    assert [row.job_id for row in pending] == [seeded["job_id"]], "no re-score was queued"
    assert (await client.get("/recommendations")).status_code == 200


async def test_retrieval_does_not_restamp_the_version_a_posting_was_scored_under(client, seeded):
    """The root cause, guarded directly.

    Retrieval runs before scoring and touches every row it finds again. While it re-stamped
    profile_version, the rows most in need of a re-score were exactly the ones that looked
    current, so `profile_version < :current` matched nothing at all.
    """
    form = {
        "title": "Something else entirely", "years_experience": "9",
        "countries": "DE\nAT", "confirm": "yes",
    }
    assert (await client.post("/profile", data=form)).status_code == 303

    async with connect() as conn:
        profile = await users_q.get_profile(conn, seeded["user_id"])
        # Exactly what _retrieve writes when it finds the posting again under the new profile.
        await match_q.upsert_matches(
            conn,
            [{
                "user_id": seeded["user_id"], "job_id": seeded["job_id"],
                "profile_version": profile.version, "retrieval_score": 0.7,
                "dense_rank": 2, "lexical_rank": 2,
            }],
        )
        pending = await match_q.pending_rerank(conn, seeded["user_id"], profile.version, 100)
    assert [row.job_id for row in pending] == [seeded["job_id"]], (
        "retrieval re-stamped the score's profile version and hid the pending re-score"
    )


async def test_replacing_todays_edition_is_asked_about_before_it_happens(client, seeded):
    """Today's edition is the only one a save may replace, so the save has to ask first."""
    form = {"title": "Something else entirely", "years_experience": "9", "countries": "DE\nAT"}

    async with connect() as conn:
        before = (await users_q.get_profile(conn, seeded["user_id"])).version

    asked = await client.post("/profile", data=form)
    assert asked.status_code == 200, "a replace went through without asking"
    assert "Replace today&#39;s edition?" in asked.text or "Replace today" in asked.text
    async with connect() as conn:
        assert (await users_q.get_profile(conn, seeded["user_id"])).version == before, (
            "the profile was saved before the user agreed"
        )

    confirmed = await client.post("/profile", data={**form, "confirm": "yes"})
    assert confirmed.status_code == 303
    async with connect() as conn:
        assert (await users_q.get_profile(conn, seeded["user_id"])).version > before


async def test_a_profile_change_queues_one_match_run_for_this_user_only(client, seeded):
    """What replaced the Match now button. Still one at a time: a second is a second bill."""
    from trouveur.db.queries import admin as admin_q

    form = {"title": "Something else entirely", "years_experience": "9", "countries": "DE\nAT"}
    assert (await client.post("/profile", data={**form, "confirm": "yes"})).status_code == 303
    assert (
        await client.post("/profile", data={**form, "title": "A third title", "confirm": "yes"})
    ).status_code == 303

    async with connect() as conn:
        runs = await admin_q.recent_runs(conn)
        pending = await admin_q.pending_match_run(conn, seeded["user_id"])
    mine = [run for run in runs if run.match_user_id == seeded["user_id"]]
    assert len(mine) == 1, "a second edit must not queue a second paid run"
    assert mine[0].only_source is None and not mine[0].backfill
    assert pending is not None and pending.id == mine[0].id
    assert "match only" in (await client.get("/admin")).text


async def test_saving_a_key_queues_a_match_so_a_new_user_sees_something(client, seeded):
    """Pasting the key is usually the last step of setting up, and without this the page stays
    empty until the next daily scan -- which reads as the key not having worked."""
    from trouveur.db.queries import admin as admin_q

    assert (await client.post("/settings", data={"api_key": "sk-or-v1-probe"})).status_code == 303
    async with connect() as conn:
        assert await admin_q.pending_match_run(conn, seeded["user_id"]) is not None


async def test_logout_clears_the_session(client):
    assert (await client.post("/logout")).status_code == 303
    response = await client.get("/recommendations")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


async def test_the_live_panel_renders_a_run_in_flight(client):
    """An empty panel exercises none of it.

    The progress bar, the cancel button and the per-source table only render when a run is
    actually in flight, so without a run in the fixture the whole feature is untested and
    StrictUndefined never sees the variables the fragment reads.
    """
    from trouveur.db.queries import admin as admin_q
    from trouveur.db.queries import ingest as ingest_q
    from trouveur.models import RunTrigger

    async with connect() as conn:
        run_id = await admin_q.enqueue_run(conn, trigger=RunTrigger.MANUAL)
        claimed = await admin_q.claim_next_run(conn)
        await admin_q.start_run_progress(conn, claimed.id, 4)
        await admin_q.advance_run_progress(conn, claimed.id, done=1, current_source="greenhouse")
        sweep_id, _ = await ingest_q.start_sweep(conn, "greenhouse")
        await ingest_q.record_sweep_progress(conn, sweep_id, documents_seen=1234)

    body = (await client.get("/admin/status")).text
    assert 'role="progressbar"' in body
    assert "1 of 4 sources done" in body
    assert "greenhouse" in body and "1,234" in body
    assert f"/admin/run/{run_id}/cancel" in body


async def test_cancelling_a_running_run_asks_rather_than_kills(client):
    """A running sweep must not be torn down mid-source: it would look complete when it is not."""
    from trouveur.db.queries import admin as admin_q
    from trouveur.models import RunStatus, RunTrigger

    async with connect() as conn:
        await admin_q.enqueue_run(conn, trigger=RunTrigger.MANUAL)
        claimed = await admin_q.claim_next_run(conn)

    response = await client.post(f"/admin/run/{claimed.id}/cancel", follow_redirects=False)
    assert response.status_code == 303

    async with connect() as conn:
        assert await admin_q.cancel_requested(conn, claimed.id)
        row = next(r for r in await admin_q.recent_runs(conn) if r.id == claimed.id)
    assert row.status == RunStatus.RUNNING.value, "a running sweep must finish its source first"


async def test_cancelling_a_queued_run_ends_it_immediately(client):
    """Nothing has started, so there is no partial sweep to protect."""
    from trouveur.db.queries import admin as admin_q
    from trouveur.models import RunStatus, RunTrigger

    async with connect() as conn:
        run_id = await admin_q.enqueue_run(conn, trigger=RunTrigger.MANUAL)

    await client.post(f"/admin/run/{run_id}/cancel", follow_redirects=False)

    async with connect() as conn:
        row = next(r for r in await admin_q.recent_runs(conn) if r.id == run_id)
    assert row.status == RunStatus.CANCELLED.value


async def test_an_off_list_filter_value_is_refused_rather_than_saved(client, seeded):
    """`work_modes` is a list of an enum. A free-text value that reached the table validated on
    the next read instead, so the Profile page and the match run both failed for that user
    from then on."""
    form = {"title": "x", "work_modes": ["Remote"]}
    assert (await client.post("/profile", data=form)).status_code == 400
    assert (await client.get("/profile")).status_code == 200

    form = {"title": "x", "countries": "Austria"}
    assert (await client.post("/profile", data=form)).status_code == 400


async def test_an_over_long_background_is_refused_rather_than_truncated(client, seeded):
    """The cap exists because both prompts that read this field have a finite attention budget.
    Silently storing the first 4,000 characters would leave the user with half a sentence and no
    way to know the rest never arrived."""
    from trouveur.models import BACKGROUND_MAX_CHARS

    form = {"title": "x", "background": "a" * (BACKGROUND_MAX_CHARS + 1)}
    assert (await client.post("/profile", data=form)).status_code == 400

    form = {"title": "x", "background": "a" * BACKGROUND_MAX_CHARS, "confirm": "yes"}
    assert (await client.post("/profile", data=form)).status_code == 303


async def test_the_profile_form_offers_every_filter_value_including_not_stated(client, seeded):
    """Each hard filter drops postings whose facet is unknown once it is set; offering `unknown`
    as a choice is how a user keeps them, so it must not be filtered out of the options."""
    body = (await client.get("/profile")).text
    for name, value in (("work_modes", "unknown"), ("seniorities", "intern"),
                        ("employment_types", "apprenticeship")):
        assert f'name="{name}" value="{value}"' in body, (name, value)
    assert '<option value="AT">Austria</option>' in body
    assert '<option value="de">German</option>' in body


# ---------- Editions ----------
# These live here rather than beside the other edition tests because the logged-in `client`
# fixture does, and the page is the half of an edition a person actually touches.

async def test_the_page_defaults_to_the_latest_edition(client, seeded):
    user_id = seeded["user_id"]
    ids = await _all_match_ids(user_id)
    await _publish(user_id, ids[:1], date(2026, 9, 14))
    await _publish(user_id, ids[1:], date(2026, 9, 15), version=2)

    body = (await client.get("/recommendations")).text
    assert "Tue 15 Sep 2026" in body


async def test_todays_edition_is_labelled_today_and_older_ones_are_dated(client, seeded):
    """A date the reader has to compare against a calendar is not an answer to "is this new?"."""
    user_id = seeded["user_id"]
    ids = await _all_match_ids(user_id)
    today = datetime.now(UTC).date()
    await _publish(user_id, ids[:1], date(2026, 9, 14))
    await _publish(user_id, ids[1:], today, version=2)

    body = (await client.get("/recommendations")).text
    assert "Today" in body
    assert today.strftime("%a %d %b %Y") not in body, "today is dated as well as named"
    assert "Mon 14 Sep 2026" in body


async def test_an_edition_read_under_an_older_profile_says_so(client, seeded):
    """And one read under the current profile says nothing: silence means "this is you"."""
    user_id = seeded["user_id"]
    ids = await _all_match_ids(user_id)
    async with connect() as conn:
        current = (await users_q.get_profile(conn, user_id)).version

    await _publish(user_id, ids[:1], date(2026, 9, 14), version=current)
    body = (await client.get("/recommendations")).text
    assert "older profile" not in body

    async with connect() as conn:
        await users_q.save_profile(conn, user_id, {"title": "A different job entirely"})
    body = (await client.get("/recommendations?edition=2026-09-14")).text
    assert f"older profile (v{current})" in body


async def test_an_older_edition_is_reachable_by_date(client, seeded):
    user_id = seeded["user_id"]
    ids = await _all_match_ids(user_id)
    await _publish(user_id, ids[:1], date(2026, 9, 14))
    await _publish(user_id, ids[1:], date(2026, 9, 15), version=2)

    body = (await client.get("/recommendations?edition=2026-09-14")).text
    assert "Mon 14 Sep 2026" in body


async def test_an_unknown_or_malformed_edition_falls_back_to_the_latest(client, seeded):
    """A stale bookmark deserves the current edition, not an error page."""
    user_id = seeded["user_id"]
    await _publish(user_id, await _all_match_ids(user_id), date(2026, 9, 15))

    for query in ("?edition=1999-01-01", "?edition=not-a-date", "?edition="):
        response = await client.get(f"/recommendations{query}")
        assert response.status_code == 200
        assert "Tue 15 Sep 2026" in response.text


async def test_the_card_states_how_old_the_advert_is(client, seeded):
    """An edition is keyed on discovery, so the card has to say when it was published."""
    user_id = seeded["user_id"]
    await _publish(user_id, await _all_match_ids(user_id), date(2026, 9, 15))
    async with connect() as conn:
        await conn.exec_driver_sql("UPDATE job SET posted_at = now() - interval '3 days'")
    body = (await client.get("/recommendations")).text
    assert "posted 3 days ago" in body
