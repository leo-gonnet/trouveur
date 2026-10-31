"""LinkedIn's visits against a real Postgres, over hand-written search pages: what a visit keeps,
each job judged held or missed once the night has swept, the number the coverage report shows, a
reader's jar from one visit to the next, and the limits a visit never passes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from trouveur.db.engine import connect
from trouveur.db.schema import (
    discovery_lead,
    job,
    linkedin_request,
    linkedin_seen,
    linkedin_visitor,
    source_sweep,
)
from trouveur.discovery import linkedin
from trouveur.discovery.linkedin import Answer


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr(linkedin.asyncio, "sleep", instant)


def _card(job_id: int, company: str | None, title: str, place: str) -> str:
    subtitle = f'<h4 class="base-search-card__subtitle">{company}</h4>' if company else ""
    return (
        f'<div class="base-search-card" data-entity-urn="urn:li:jobPosting:{job_id}">'
        f'<h3 class="base-search-card__title">{title}</h3>{subtitle}'
        f'<span class="job-search-card__location">{place}</span></div>'
    )


class _Site:
    """Our Wien posting seen from a suburb, the same title at the same company in another country,
    a company we have never seen, and two cards nothing can be judged on -- on one search page
    that names their count. Each visit's browser hands back a jar LinkedIn would have set."""

    def __init__(self, company: str, title: str, tag: str) -> None:
        cards = (
            _card(1, f"{company} GmbH", f"{title} {tag}", "Schwechat, Österreich")
            + _card(2, company, title, "München, Bayern, Deutschland")
            + _card(3, "Ferne AG", "Prozessingenieur", "Wien, Wien, Österreich")
            + _card(4, None, "Prozessingenieur", "Wien, Wien, Österreich")
            + _card(5, company, title, "European Union")
        )
        self.page = (
            '<span class="results-context-header__job-count">5</span>' + f"<ul>{cards}</ul>"
        )
        self.opened_with: list[list[dict]] = []
        self.visits = 0

    def __call__(self, cookies: list[dict]):
        self.opened_with.append(cookies)
        self.visits += 1
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, *, params, headers):
        return Answer(200, "", self.page, None, ["bcookie"])

    def cookies(self) -> list[dict]:
        return [{"name": "bcookie", "value": f"secret-{self.visits}", "domain": ".linkedin.com",
                 "path": "/", "secure": True, "expires": None}]


async def _a_while_later() -> None:
    """Move every request back past the gap a visit keeps from the last one."""
    async with connect() as conn:
        await conn.execute(
            linkedin_request.update().values(
                sent_at=linkedin_request.c.sent_at - timedelta(minutes=10)
            )
        )


@pytest.mark.parametrize("tag", ["(m/w/d)", "- H/F"])
@pytest.mark.parametrize("named_by", ["company", "slug"])
async def test_each_job_is_judged_at_first_sight_and_counted_in_the_report(
    seeded, monkeypatch, named_by, tag
):
    from trouveur.coverage import report

    async with connect() as conn:
        ours = (
            await conn.execute(
                sa.select(job.c.id, job.c.company, job.c.title)
                .where(job.c.source == "greenhouse", job.c.title == "Werkstudent Logistik")
            )
        ).one()
        if named_by == "slug":
            # As Ashby, Lever and most Personio boards store it: no company, only the board.
            board = {"company": None, "scope": "beispieltechgmbh"}
            await conn.execute(job.update().where(job.c.id == ours.id).values(**board))
    site = _Site(ours.company, ours.title, tag)
    settings = linkedin.get_settings().model_copy(update={"linkedin_max_requests": 20})

    found = await linkedin.visit_due(settings, force=True, browser=site)
    assert found is not None and found.blocked is None and found.error is None
    async with connect() as conn:
        assert await conn.scalar(sa.select(sa.func.count()).select_from(discovery_lead)) == 0, (
            "a card is judged after the night's sweep, never when it is seen"
        )
        assert await conn.scalar(sa.select(sa.func.count()).select_from(linkedin_seen)) == 5

    await linkedin.record_seen()

    async with connect() as conn:
        leads = (
            await conn.execute(
                sa.select(
                    discovery_lead.c.payload["linkedin_id"].astext.label("linkedin_id"),
                    discovery_lead.c.result,
                    discovery_lead.c.held,
                    discovery_lead.c.held_job_id,
                ).order_by(discovery_lead.c.id)
            )
        ).all()
        card = await report(conn, horizon_days=7)
    recall, missed = card.recall, card.misses
    assert [(row.linkedin_id, row.held, row.held_job_id) for row in leads] == [
        ("1", True, ours.id),
        ("2", False, None),
        ("3", False, None),
        ("4", None, None),
        ("5", None, None),
    ], "our Wien copy is not the München job, and a wide region proves nothing"
    assert {row.result for row in leads} == {"no_url"}
    assert [(row.origin, row.seen, row.judged, row.held) for row in recall] == [
        ("linkedin", 5, 3, 1)
    ]
    assert (missed.jobs, missed.companies) == (2, 2)
    assert sorted((row.company, row.jobs, row.sample_title) for row in missed.top) == [
        ("Beispiel Tech", 1, "Werkstudent Logistik"), ("Ferne AG", 1, "Prozessingenieur")
    ]

    assert await linkedin.visit_due(settings, force=True, browser=site) is None, (
        "a visit right after another is one fast client"
    )
    await _a_while_later()
    assert await linkedin.visit_due(settings, force=True, browser=site) is not None
    assert await linkedin.record_seen() == [], "a job seen before is not judged again"
    async with connect() as conn:
        assert await conn.scalar(sa.select(sa.func.count()).select_from(discovery_lead)) == 5
        assert await conn.scalar(sa.select(sa.func.count()).select_from(linkedin_seen)) == 0


async def test_a_reader_keeps_one_jar_until_it_expires_and_its_values_stay_out_of_the_log(seeded):
    site = _Site("Nahe", "Prozessingenieur", "(m/w/d)")
    settings = linkedin.get_settings().model_copy(update={"linkedin_max_requests": 20})

    await linkedin.visit_due(settings, force=True, browser=site)
    await _a_while_later()
    await linkedin.visit_due(settings, force=True, browser=site)
    assert site.opened_with[0] == [] and site.opened_with[1][0]["value"] == "secret-1"

    async with connect() as conn:
        await conn.execute(
            linkedin_visitor.update().values(jar_expires_at=datetime.now(UTC) - timedelta(hours=1))
        )
        logged = (await conn.execute(sa.select(linkedin_request))).all()
    assert logged and all(row.user_id == seeded["user_id"] for row in logged)
    assert not any("secret" in str(tuple(row)) for row in logged)
    await _a_while_later()
    await linkedin.visit_due(settings, force=True, browser=site)
    assert site.opened_with[2] == [], "an expired jar is a new visitor"


async def test_a_visit_never_passes_the_days_budget_nor_runs_inside_a_pause(seeded):
    site = _Site("Nahe", "Prozessingenieur", "(m/w/d)")
    one = linkedin.get_settings().model_copy(update={"linkedin_max_requests": 1})

    found = await linkedin.visit_due(one, force=True, browser=site)
    assert found is not None and found.requests == 1
    await _a_while_later()
    assert await linkedin.visit_due(one, force=True, browser=site) is None, (
        "the window's budget is spent, and force does not lift it"
    )

    async with connect() as conn:
        await conn.execute(linkedin_request.delete())
        await conn.execute(
            source_sweep.insert().values(
                source="linkedin", finished_at=datetime.now(UTC), ok=False, blocked=True,
                error="LinkedIn refused it with HTTP 429.",
            )
        )
    # Four, so the slowdown a block earns still leaves a budget: only the pause can stop this.
    four = one.model_copy(update={"linkedin_max_requests": 4})
    assert await linkedin.visit_due(four, force=True, browser=site) is None, (
        "a block pauses the source, and force does not lift that either"
    )
