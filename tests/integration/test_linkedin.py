"""The daily LinkedIn pass against a real Postgres, over hand-written search pages: each job
judged held or missed at first sight, and the number the coverage report shows."""

from __future__ import annotations

import sqlalchemy as sa

from trouveur.db.engine import connect
from trouveur.db.schema import discovery_lead, job
from trouveur.discovery import linkedin


class _Response:
    def __init__(self, url: str, text: str) -> None:
        self.url, self.text, self.status_code = url, text, 200


def _card(job_id: int, company: str | None, title: str) -> str:
    subtitle = f'<h4 class="base-search-card__subtitle">{company}</h4>' if company else ""
    return (
        f'<div class="base-search-card" data-entity-urn="urn:li:jobPosting:{job_id}">'
        f'<h3 class="base-search-card__title">{title}</h3>{subtitle}'
        '<span class="job-search-card__location">Wien, Österreich</span></div>'
    )


class _Site:
    """Job 1 is a posting we hold under a slightly different name, job 2 a company we have never
    seen, job 3 a card that names no company."""

    def __init__(self, company: str, title: str) -> None:
        self.cards = (
            _card(1, f"{company} GmbH", f"{title} (m/w/d)")
            + _card(2, "Ferne AG", "Prozessingenieur")
            + _card(3, None, "Prozessingenieur")
        )
        self.asked: list[dict] = []
        self.attempts = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def paced(self, delay, *, attempts=None):
        self.attempts = attempts
        return self

    async def get(self, url, *, params=None, headers=None):
        self.asked.append(params)
        return _Response(url, self.cards if params["start"] == 0 else linkedin.END_PAGE)


async def test_each_job_is_judged_at_first_sight_and_counted_in_the_report(seeded, monkeypatch):
    from trouveur.coverage import report

    async with connect() as conn:
        ours = (
            await conn.execute(
                sa.select(job.c.id, job.c.company, job.c.title)
                .where(job.c.source == "greenhouse", job.c.company.is_not(None))
                .order_by(job.c.id)
                .limit(1)
            )
        ).one()
    site = _Site(ours.company, ours.title)
    monkeypatch.setattr(linkedin, "PoliteClient", lambda: site)
    settings = linkedin.get_settings().model_copy(update={"linkedin_max_requests": 20})

    found = await linkedin.run(settings)
    assert found is not None and found.blocked is None and found.error is None
    assert site.attempts == 1, "a 429 retried at LinkedIn is how a slowdown becomes a block"

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
        recall = (await report(conn, horizon_days=7)).recall
    assert [(row.linkedin_id, row.held, row.held_job_id) for row in leads] == [
        ("1", True, ours.id),
        ("2", False, None),
        ("3", None, None),
    ]
    assert {row.result for row in leads} == {"no_url"}
    assert [(row.origin, row.seen, row.held) for row in recall] == [("linkedin", 2, 1)]

    assert await linkedin.run(settings) is None, "a second pass inside the day asks nothing"
    again = await linkedin.run(settings, force=True)
    assert again is not None and again.leads == [], "a job seen before is not judged again"
    async with connect() as conn:
        assert await conn.scalar(sa.select(sa.func.count()).select_from(discovery_lead)) == 3
