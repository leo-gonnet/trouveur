"""Every page renders against real rows.

A template is code that only runs when rendered. Nothing else in the suite executes one, so a
template referencing a column that was renamed raises at request time and is invisible to every
other test -- and this codebase has renamed several (`country` to `countries`, `cost_eur` to
`cost_usd`, the whole per-user overlay moving off the job row).

Driven over ASGI rather than a browser: with no build step and HTMX-only interactivity, this
reaches the same code a browser would for a fraction of the maintenance.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import httpx
import pytest
import sqlalchemy as sa

from tests.integration.test_editions import _all_match_ids, _publish
from trouveur import clock
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


@pytest.fixture
async def admin(seeded):
    """A logged-in ADMIN, which the seeded reader deliberately is not: every page here is read as
    an ordinary user unless a test asks otherwise."""
    from tests.integration.seed import seed_user
    from trouveur.web.auth import hash_password

    admin_id, _ = await seed_user("overseer", is_admin=True)
    async with connect() as conn:
        await conn.execute(
            sa.update(app_user)
            .where(app_user.c.id == admin_id)
            .values(password_hash=hash_password(PASSWORD))
        )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as session:
        response = await session.post(
            "/login", data={"username": "overseer", "password": PASSWORD}
        )
        assert response.status_code == 303, "login form did not authenticate"
        yield session


async def _public_id() -> str:
    async with connect() as conn:
        return str((await conn.execute(sa.select(job.c.public_id).limit(1))).scalar())


@pytest.mark.parametrize(
    "path",
    ["/recommendations", "/search", "/dashboard", "/profile", "/settings", "/how-it-works"],
)
async def test_page_renders(client, path):
    response = await client.get(path)
    assert response.status_code == 200, f"{path} returned {response.status_code}"
    assert "<main>" in response.text


@pytest.mark.parametrize("path", ["/admin", "/users"])
async def test_admin_page_renders(admin, path):
    response = await admin.get(path)
    assert response.status_code == 200, f"{path} returned {response.status_code}"
    assert "<main>" in response.text


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/admin"), ("POST", "/admin/run"), ("GET", "/users"), ("POST", "/users")],
)
async def test_an_admin_route_refuses_an_ordinary_user(client, method, path):
    """Checked by prefix in the middleware, not per handler: a per-route check is the line somebody
    forgets on the next route, and the route that leaks is always the newest one.

    Scans owns the SHARED schedule and queues real sweeps, so it is not one reader's to retime.
    """
    response = await client.request(method, path)
    assert response.status_code == 303, f"{method} {path} answered an ordinary user"
    assert response.headers["location"] == "/recommendations"


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


async def test_the_out_of_credit_banner_is_on_every_page_until_credit_is_granted(client, seeded):
    """Credit is what silently stops scoring, so a reader must not have to visit Settings to learn
    they have none. Set by the session middleware, so no route can forget it."""
    for path in ("/recommendations", "/search", "/profile"):
        body = (await client.get(path)).text
        assert 'class="banner warn"' in body, path
    assert "Ask an\n  administrator to top it up." in (await client.get("/search")).text

    async with connect() as conn:
        await users_q.grant_credit(
            conn, seeded["user_id"], amount_usd=Decimal("5"), granted_by=seeded["user_id"],
            note="test",
        )
    assert 'class="banner warn"' not in (await client.get("/recommendations")).text


async def test_the_balance_is_readable_from_the_topbar(client, seeded):
    """The one number that decides whether tomorrow's scan scores anything."""
    async with connect() as conn:
        await users_q.grant_credit(
            conn, seeded["user_id"], amount_usd=Decimal("3.42"), granted_by=seeded["user_id"],
            note="test",
        )
    body = (await client.get("/search")).text
    assert "$3.42" in body


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


async def test_the_ceiling_is_the_users_to_set_while_scoring_is_on(client, seeded):
    assert (
        await client.post(
            "/settings/scoring", data={"scoring_enabled": "on", "daily_ceiling_usd": "0.75"}
        )
    ).status_code == 303
    async with connect() as conn:
        assert (
            await users_q.get_profile(conn, seeded["user_id"])
        ).daily_ceiling_usd == Decimal("0.75")


async def test_the_page_states_the_monthly_worst_case_and_offers_no_monthly_ceiling(
    client, seeded
):
    """One limit, and it is the day's. The month is what a reader worries about, so it is stated
    rather than set: two numbers for one decision is how they come to disagree."""
    async with connect() as conn:
        await users_q.save_profile(conn, seeded["user_id"], {"daily_ceiling_usd": Decimal("0.25")})
    body = (await client.get("/settings")).text
    assert "$7.50" in body, "thirty days of a $0.25 ceiling is the worst case the page must state"
    assert "monthly_budget_usd" not in body, "a second, monthly ceiling must not come back"


async def test_an_absent_ceiling_keeps_the_stored_one_rather_than_resetting_it(client, seeded):
    """The form DISABLES the ceiling while scoring is off, and a disabled input submits nothing.

    Read as "reset to the default", that silently put the user back on the default every time they
    touched the switch -- a cost control quietly widened by the control meant to narrow it.
    """
    async with connect() as conn:
        await users_q.save_profile(conn, seeded["user_id"], {"daily_ceiling_usd": Decimal("0.90")})

    assert (await client.post("/settings/scoring", data={})).status_code == 303
    async with connect() as conn:
        profile = await users_q.get_profile(conn, seeded["user_id"])
    assert profile.daily_ceiling_usd == Decimal("0.90"), "the ceiling was silently reset"


async def test_the_ceiling_cannot_be_changed_while_scoring_is_off(client, seeded):
    """Enforced in the route, not only by the disabled attribute the browser is asked to honour."""
    async with connect() as conn:
        await users_q.save_profile(conn, seeded["user_id"], {"daily_ceiling_usd": Decimal("0.90")})
    assert (
        await client.post("/settings/scoring", data={"daily_ceiling_usd": "999"})
    ).status_code == 303
    async with connect() as conn:
        assert (
            await users_q.get_profile(conn, seeded["user_id"])
        ).daily_ceiling_usd == Decimal("0.90")


async def test_editing_the_ceiling_does_not_queue_a_paid_run(client, seeded):
    """Turning scoring back on is a reason to run; changing a number is not."""
    from trouveur.db.queries import admin as admin_q

    assert (
        await client.post(
            "/settings/scoring", data={"scoring_enabled": "on", "daily_ceiling_usd": "0.9"}
        )
    ).status_code == 303
    async with connect() as conn:
        assert await admin_q.pending_match_run(conn, seeded["user_id"]) is None

    # Off, then on again: that one does queue.
    assert (await client.post("/settings/scoring", data={})).status_code == 303
    assert (
        await client.post("/settings/scoring", data={"scoring_enabled": "on"})
    ).status_code == 303
    async with connect() as conn:
        assert await admin_q.pending_match_run(conn, seeded["user_id"]) is not None


async def test_the_ceiling_input_is_locked_while_scoring_is_off(client, seeded):
    assert (await client.post("/settings/scoring", data={})).status_code == 303
    body = (await client.get("/settings")).text
    assert 'name="daily_ceiling_usd"' in body, "the ceiling is still shown, just not editable"
    assert "disabled" in body
    assert "Save ceiling" not in body


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
        "countries": "DE\nAT", "remote_anywhere": "yes", "cities": "",
        "min_salary_eur_year": "60000",
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
        "countries": "DE\nAT",
    }
    assert (await client.post("/profile", data=form)).status_code == 303

    async with connect() as conn:
        profile = await users_q.get_profile(conn, seeded["user_id"])
        after = (await conn.execute(
            user_job_match.select().where(user_job_match.c.user_id == seeded["user_id"])
        )).one()
        pending = await match_q.pending_rerank(
            conn, seeded["user_id"], profile.version, [seeded["job_id"]]
        )
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
        "countries": "DE\nAT",
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
        pending = await match_q.pending_rerank(
            conn, seeded["user_id"], profile.version, [seeded["job_id"]]
        )
    assert [row.job_id for row in pending] == [seeded["job_id"]], (
        "retrieval re-stamped the score's profile version and hid the pending re-score"
    )


async def test_saving_a_profile_destroys_no_edition_and_asks_nothing(client, seeded):
    """There is no confirm step, because a save no longer destroys anything.

    A profile change used to replace today's edition, so the form had to ask first. Now it
    publishes a second edition for the day beside the first, which leaves nothing to agree to --
    and removes the one exception to "an edition is never rewritten".
    """
    from datetime import date as _date

    user_id = seeded["user_id"]
    today = _date(2026, 9, 24)
    ids = await _all_match_ids(user_id)
    await _publish(user_id, ids, today, version=1)

    async with connect() as conn:
        before = (await users_q.get_profile(conn, user_id)).version

    form = {"title": "Something else entirely", "years_experience": "9", "countries": "DE\nAT"}
    saved = await client.post("/profile", data=form)
    assert saved.status_code == 303, "a save was interrupted by a page that no longer exists"

    async with connect() as conn:
        assert (await users_q.get_profile(conn, user_id)).version > before
        kept = await match_q.edition(conn, user_id, today, 1, limit=100)
    assert len(kept) == len(ids), "saving a profile destroyed a published edition"


async def test_a_profile_change_queues_one_match_run_for_this_user_only(client, admin, seeded):
    """What replaced the Match now button. Still one at a time: a second is a second bill."""
    from trouveur.db.queries import admin as admin_q

    form = {"title": "Something else entirely", "years_experience": "9", "countries": "DE\nAT"}
    assert (await client.post("/profile", data=form)).status_code == 303
    assert (
        await client.post("/profile", data={**form, "title": "A third title"})
    ).status_code == 303

    async with connect() as conn:
        runs = await admin_q.recent_runs(conn)
        pending = await admin_q.pending_match_run(conn, seeded["user_id"])
    mine = [run for run in runs if run.match_user_id == seeded["user_id"]]
    assert len(mine) == 1, "a second edit must not queue a second paid run"
    assert mine[0].only_source is None and not mine[0].backfill
    assert pending is not None and pending.id == mine[0].id
    assert "match only" in (await admin.get("/admin")).text


async def test_granting_credit_queues_a_match_so_a_new_user_sees_something(client, admin, seeded):
    """Credit is what stops a run, so granting some is a reason to start one. Without this the
    person waits for the next daily scan -- which reads as the top-up not having worked."""
    from trouveur.db.queries import admin as admin_q

    response = await admin.post(
        f"/users/{seeded['user_id']}/credit", data={"amount_usd": "5.00", "note": "welcome"}
    )
    assert response.status_code == 303
    async with connect() as conn:
        assert await admin_q.pending_match_run(conn, seeded["user_id"]) is not None
        credit = await users_q.credit(conn, seeded["user_id"])
    assert credit.balance_usd == Decimal("5.00")


async def test_logout_clears_the_session(client):
    assert (await client.post("/logout")).status_code == 303
    response = await client.get("/recommendations")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


async def test_the_live_panel_renders_a_run_in_flight(admin):
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

    body = (await admin.get("/admin/status")).text
    assert 'role="progressbar"' in body
    assert "1 of 4 sources done" in body
    assert "greenhouse" in body and "1,234" in body
    assert f"/admin/run/{run_id}/cancel" in body


async def test_cancelling_a_running_run_asks_rather_than_kills(admin):
    """A running sweep must not be torn down mid-source: it would look complete when it is not."""
    from trouveur.db.queries import admin as admin_q
    from trouveur.models import RunStatus, RunTrigger

    async with connect() as conn:
        await admin_q.enqueue_run(conn, trigger=RunTrigger.MANUAL)
        claimed = await admin_q.claim_next_run(conn)

    response = await admin.post(f"/admin/run/{claimed.id}/cancel", follow_redirects=False)
    assert response.status_code == 303

    async with connect() as conn:
        assert await admin_q.cancel_requested(conn, claimed.id)
        row = next(r for r in await admin_q.recent_runs(conn) if r.id == claimed.id)
    assert row.status == RunStatus.RUNNING.value, "a running sweep must finish its source first"


async def test_cancelling_a_queued_run_ends_it_immediately(admin):
    """Nothing has started, so there is no partial sweep to protect."""
    from trouveur.db.queries import admin as admin_q
    from trouveur.models import RunStatus, RunTrigger

    async with connect() as conn:
        run_id = await admin_q.enqueue_run(conn, trigger=RunTrigger.MANUAL)

    await admin.post(f"/admin/run/{run_id}/cancel", follow_redirects=False)

    async with connect() as conn:
        row = next(r for r in await admin_q.recent_runs(conn) if r.id == run_id)
    assert row.status == RunStatus.CANCELLED.value


async def test_an_off_list_filter_value_is_refused_rather_than_saved(client, seeded):
    """`countries` is validated against what derivation can produce. A free-text value that
    reached the table validated on the next read instead, so the Profile page and the match run
    both failed for that user from then on -- `Remote` was the one that did it, which is now a
    checkbox on the same filter rather than a country somebody could type."""
    form = {"title": "x", "countries": "Remote"}
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

    form = {"title": "x", "background": "a" * BACKGROUND_MAX_CHARS}
    assert (await client.post("/profile", data=form)).status_code == 303


async def test_the_profile_form_offers_location_and_nothing_else_to_filter_on(client, seeded):
    """Location is the only hard filter, and fully-remote is part of it rather than a work mode.

    The three enum pickers this replaced each needed a "not stated" tick box beside them, whose
    only job was to undo the filter the user had just set. A picker reappearing here means a facet
    has started dropping postings that state nothing for it again.
    """
    body = (await client.get("/profile")).text
    assert 'name="remote_anywhere"' in body
    for name in ("work_modes", "seniorities", "employment_types"):
        assert f'name="{name}"' not in body, name
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
    today = clock.today()
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


async def test_a_long_edition_is_paged_and_the_pager_renders(client, seeded, monkeypatch):
    """Nothing bounds an edition's length any more, so the page has to.

    Rendered with a page size of one rather than a fixture of 51 postings, because a fixture that
    fits on one page never executes the pager markup at all -- and under StrictUndefined a name
    the route forgot to pass fails only when it is reached.
    """
    from trouveur.web import app as web_app

    user_id = seeded["user_id"]
    ids = await _all_match_ids(user_id)
    day = date(2026, 9, 14)
    await _publish(user_id, ids, day)
    monkeypatch.setattr(web_app, "EDITION_PAGE", 1)

    first = (await client.get(f"/recommendations?edition={day}&v=1")).text
    assert "next" in first
    assert "previous" not in first

    cursor = re.search(r"after=(\d+_\d+)", first).group(1)
    second = (await client.get(f"/recommendations?edition={day}&v=1&after={cursor}")).text
    assert "previous" in second

    past_the_end = await client.get(f"/recommendations?edition={day}&v=1&after=0_0")
    assert past_the_end.status_code == 200
    assert "Nothing further in this edition" in past_the_end.text


async def test_dismissing_on_one_page_does_not_skip_the_next_page(client, seeded, monkeypatch):
    """The reason an edition is paged by cursor and not by offset.

    The dismiss filter runs before the page is cut, so with OFFSET the boundary moves under the
    reader: they are looking at page one, they dismiss what is on it, and the "next" link they
    already have now points one row too far. The posting that shifted across the boundary is
    never shown on any page, and nothing says so -- dismissing is an htmx swap of the single
    widget, so the list on screen does not shrink to hint at it either.

    A cursor is anchored to the row the reader last saw, so it means the same thing before and
    after the set changes underneath it.
    """
    from trouveur.web import app as web_app

    user_id = seeded["user_id"]
    ids = await _all_match_ids(user_id)
    assert len(ids) >= 3, "the skip needs a page to fall off the end of"
    day = date(2026, 9, 14)
    await _publish(user_id, ids, day)
    monkeypatch.setattr(web_app, "EDITION_PAGE", 1)

    # Every posting is published at the same score, so the order is the tiebreak: job_id DESC.
    first, second = sorted(ids, reverse=True)[:2]

    page_one = (await client.get(f"/recommendations?edition={day}&v=1")).text
    assert f'/job/{first}/state' in page_one
    next_link = re.search(r'href="([^"]*after=[^"]*)"', page_one).group(1).replace("&amp;", "&")

    # The reader dismisses what is on the page they are looking at, then follows the link that
    # was already rendered for them.
    assert (
        await client.post(f"/job/{first}/state", data={"state": "dismissed"})
    ).status_code == 200

    page_two = (await client.get(next_link)).text
    assert f'/job/{second}/state' in page_two, (
        "the posting after the dismissed one was stepped over and is now unreachable"
    )


async def test_an_admin_creates_an_account_and_its_password_is_shown_exactly_once(admin):
    """There is no invitation email and no reset link, so the generated password has to reach the
    admin somehow -- and it must not reach the URL, the proxy log or the next page."""
    response = await admin.post(
        "/users", data={"username": "newcomer", "email": "n@example.test", "credit": "5.00"}
    )
    assert response.status_code == 200, "the password must be rendered, never redirected to"
    shown = re.search(r"password <code>([^<]+)</code>", response.text)
    assert shown, "the created account's password was not shown at all"
    password = shown.group(1)

    assert password not in (await admin.get("/users")).text, "the password was shown twice"

    async with connect() as conn:
        created = await users_q.get_user_by_username(conn, "newcomer")
        credit = await users_q.credit(conn, created.id)
    assert created is not None and created.is_admin is False
    assert credit.balance_usd == Decimal("5.00")
    assert created.password_hash != password, "the password must be stored as a hash"

    # And it is the password that was shown: the new account can log in with it.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as session:
        login = await session.post(
            "/login", data={"username": "newcomer", "password": password},
            follow_redirects=False,
        )
    assert login.status_code == 303, "the password shown does not work"


async def test_a_duplicate_username_is_refused_rather_than_colliding(admin, seeded):
    async with connect() as conn:
        existing = (await users_q.get_user(conn, seeded["user_id"])).username
    response = await admin.post("/users", data={"username": existing})
    assert response.status_code == 200
    assert "already exists" in response.text


async def test_changing_a_password_requires_the_current_one(client, seeded):
    """A borrowed session must not be able to lock the owner out of their own account."""
    from trouveur.web.auth import verify_password

    response = await client.post(
        "/settings/password",
        data={
            "current_password": "not the password",
            "new_password": "a-new-long-password",
            "repeat_password": "a-new-long-password",
        },
    )
    assert response.headers["location"] == "/settings?password=wrong"
    async with connect() as conn:
        assert verify_password(
            (await users_q.get_user(conn, seeded["user_id"])).password_hash, PASSWORD
        )

    response = await client.post(
        "/settings/password",
        data={
            "current_password": PASSWORD,
            "new_password": "a-new-long-password",
            "repeat_password": "a-new-long-password",
        },
    )
    assert response.headers["location"] == "/settings?password=changed"
    async with connect() as conn:
        assert verify_password(
            (await users_q.get_user(conn, seeded["user_id"])).password_hash, "a-new-long-password"
        )


async def test_a_short_or_mismatched_new_password_is_refused(client):
    """The same floor as the CLI: this login faces the internet."""
    short = await client.post(
        "/settings/password",
        data={"current_password": PASSWORD, "new_password": "short", "repeat_password": "short"},
    )
    assert short.headers["location"] == "/settings?password=short"
    mismatch = await client.post(
        "/settings/password",
        data={
            "current_password": PASSWORD,
            "new_password": "a-long-enough-password",
            "repeat_password": "a-different-password",
        },
    )
    assert mismatch.headers["location"] == "/settings?password=mismatch"


async def test_an_admin_is_shown_no_balance_because_they_need_none(admin):
    """An admin grants credit and has none of their own, so a zero balance in their topbar would
    read as a problem to fix rather than as a category that does not apply."""
    body = (await admin.get("/users")).text
    assert "needs none" in body
    # The specific banner, not any banner: the one about a missing installation key is an admin's
    # to act on and must still reach them.
    assert "Your credit is used up" not in body, "an admin was told to top themselves up"
    assert 'title="LLM credit remaining"' not in body, "an admin was shown a balance"


async def test_disabling_an_account_revokes_the_session_it_already_holds(client, admin, seeded):
    """Not only future logins. A session is a signed token with a 30-day life, so checking
    is_active at login alone means an admin disabling an account revokes nothing its holder can
    already do -- for a month, while the Users page says "cannot log in"."""
    assert (await client.get("/recommendations")).status_code == 200

    response = await admin.post(f"/users/{seeded['user_id']}/active", data={"is_active": "off"})
    assert response.status_code == 303

    revoked = await client.get("/recommendations")
    assert revoked.status_code == 303, "a disabled account kept browsing on its existing session"
    assert revoked.headers["location"].startswith("/login")
    # And the cookie is cleared, so they are not bounced between /login and a dead session.
    assert "trouveur_session=" in revoked.headers.get("set-cookie", "")

    # Every page, not just the one: the check is in the middleware.
    for path in ("/settings", "/search", "/profile"):
        assert (await client.get(path)).status_code == 303, path


async def test_a_non_numeric_ceiling_falls_back_instead_of_500ing(client, seeded):
    """`Decimal("nan")` and `Decimal("inf")` PARSE, so neither reaches the except clause. A NaN
    then raises from the first comparison that touches it -- a 500 on a form meant to fall back --
    and an Infinity is worse than a crash: accepted as a ceiling, it removes the cap entirely."""
    async with connect() as conn:
        await users_q.save_profile(conn, seeded["user_id"], {"daily_ceiling_usd": Decimal("0.25")})

    for bad in ("nan", "inf", "-inf", "NaN", "abc", "1e999999999"):
        response = await client.post(
            "/settings/scoring", data={"scoring_enabled": "on", "daily_ceiling_usd": bad}
        )
        assert response.status_code == 303, f"{bad!r} produced {response.status_code}"
        async with connect() as conn:
            ceiling = (await users_q.get_profile(conn, seeded["user_id"])).daily_ceiling_usd
        assert ceiling == Decimal("0.25"), f"{bad!r} was stored as a ceiling of {ceiling}"


async def test_a_non_numeric_credit_grant_is_refused_rather_than_crashing(admin, seeded):
    """The same helper, reached through the two money fields this page added."""
    for bad in ("nan", "inf", "abc"):
        response = await admin.post(
            f"/users/{seeded['user_id']}/credit", data={"amount_usd": bad, "note": "probe"}
        )
        assert response.status_code == 303, f"{bad!r} produced {response.status_code}"
    async with connect() as conn:
        assert (await users_q.credit(conn, seeded["user_id"])).balance_usd == 0

    created = await admin.post("/users", data={"username": "oddball", "credit": "nan"})
    assert created.status_code == 200
    async with connect() as conn:
        row = await users_q.get_user_by_username(conn, "oddball")
        assert (await users_q.credit(conn, row.id)).balance_usd == 0
