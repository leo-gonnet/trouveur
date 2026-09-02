from __future__ import annotations

from trouveur.models import Job, fold


def _job(**kw) -> Job:
    base = dict(source="s", source_native_id="1", url="u", title="t")
    return Job(**{**base, **kw})


def test_fold_strips_german_diacritics():
    assert fold("München") == "munchen"
    assert fold("Zürich") == "zurich"
    assert fold("Straße") == "strasse"
    assert fold("Führungskraft") == "fuhrungskraft"


def test_content_hash_matches_across_sources_when_same_vacancy():
    a = _job(source="arbeitsagentur", source_native_id="1",
             title="Wirtschaftsingenieur (m/w/d)", company="ACME GmbH", location_city="Wien")
    b = _job(source="karriere_at", source_native_id="99",
             title="wirtschaftsingenieur  (m/w/d)!", company="ACME  GmbH", location_city="wien")
    assert a.content_hash == b.content_hash


def test_content_hash_differs_when_company_differs():
    a = _job(title="Wirtschaftsingenieur", company="ACME GmbH", location_city="Wien")
    b = _job(title="Wirtschaftsingenieur", company="Other GmbH", location_city="Wien")
    assert a.content_hash != b.content_hash


def test_content_hash_ignores_url_and_salary():
    """Aggregators differ on these for one underlying vacancy, so they must not split it."""
    a = _job(title="X", company="Y", location_city="Wien", url="https://a", salary_min=1)
    b = _job(title="X", company="Y", location_city="Wien", url="https://b", salary_min=2)
    assert a.content_hash == b.content_hash
