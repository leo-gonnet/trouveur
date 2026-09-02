from __future__ import annotations

from types import SimpleNamespace

from trouveur.notify.email import render


def row(**kw):
    base = dict(id=1, title="Wirtschaftsingenieur (m/w/d)", company="ACME GmbH",
                location_city="Wien", location_country="AT", remote=True,
                salary_min=65000, salary_max=80000, salary_period="YEAR",
                llm_score=88, llm_reason="Strong match.", url="https://example.invalid/1")
    return SimpleNamespace(**{**base, **kw})


def test_digest_includes_score_title_and_link():
    subject, text, html = render([row()], threshold=70)
    assert "1 new match" in subject
    assert "Wirtschaftsingenieur (m/w/d)" in text
    assert "https://example.invalid/1" in text
    assert "88" in html


def test_digest_pluralises_correctly():
    assert "2 new matches" in render([row(id=1), row(id=2)], 70)[0]


def test_digest_handles_missing_salary_and_company():
    _, text, _ = render([row(company=None, salary_min=None, salary_max=None)], 70)
    assert "unknown company" in text


def test_html_escapes_job_titles():
    """Titles come from third parties and land in an HTML email."""
    _, _, html = render([row(title='Ingenieur <script>alert("x")</script>')], 70)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
