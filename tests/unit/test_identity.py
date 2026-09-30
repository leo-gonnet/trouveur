"""Guards for the two identity questions V1 conflated into one UNIQUE constraint.

V1 had UNIQUE (content_hash) over hash(title|company|city) sitting next to
UNIQUE (source, source_native_id). The same vacancy arriving from a second source hit the
content-hash conflict, upsert returned (-1, False), and the row was dropped with no error and no
record that a second source had ever seen it.
"""

from __future__ import annotations

from trouveur.models import CanonicalJob, Location, dedup_key
from trouveur.versions import CONTENT_HASH_VERSION


def _job(**kwargs) -> CanonicalJob:
    base = {
        "source": "a",
        "external_id": "1",
        "url": "https://example.test",
        "title": "Wirtschaftsingenieur",
        "company": "Beispiel GmbH",
        "locations": [Location(raw="München, DEUTSCHLAND", city="München")],
        "description": "Prozessoptimierung in der Fertigung.",
    }
    return CanonicalJob(**{**base, **kwargs})


def test_content_hash_tracks_what_a_reranker_reads():
    assert _job().content_hash == _job().content_hash
    assert _job().content_hash != _job(description="Something else entirely.").content_hash
    assert _job().content_hash != _job(title="Prozessingenieur").content_hash


def test_content_hash_ignores_provenance():
    # The same posting from two boards must produce one cache entry, not two: it is the same text
    # and scoring it twice is paying twice for one answer.
    assert _job(source="a", external_id="1").content_hash == _job(
        source="b", external_id="99"
    ).content_hash


def test_content_hash_is_versioned():
    """Bumping the version must change every hash.

    Otherwise adding a field to the hashed set leaves pre-existing rows holding a stale hash and
    silently skipping the work that new field was supposed to trigger.
    """
    import trouveur.models.job as job_module

    before = _job().content_hash
    original = job_module.CONTENT_HASH_VERSION
    try:
        job_module.CONTENT_HASH_VERSION = original + 1
        assert _job().content_hash != before
    finally:
        job_module.CONTENT_HASH_VERSION = original
    assert CONTENT_HASH_VERSION == original


def test_dedup_key_answers_a_different_question_from_content_hash():
    """Semantic identity is not content identity, and must never share a constraint with it.

    Two boards carrying one vacancy have the same dedup key but need to stay two rows, each with
    its own provenance; the marker records the relationship instead of destroying it.
    """
    same_role = dedup_key("Wirtschaftsingenieur", "Beispiel GmbH")
    reworded = dedup_key("Wirtschaftsingenieur", "Beispiel GmbH")
    assert same_role == reworded

    # Content hash is sensitive to description; the dedup key deliberately is not.
    assert _job().content_hash != _job(description="Reworded advert, same job.").content_hash


def test_dedup_key_separates_different_companies():
    assert dedup_key("Engineer", "A GmbH") != dedup_key("Engineer", "B GmbH")


def test_an_aggregator_copy_matches_its_board_by_the_board_id_not_the_name():
    """Arbeitnow names the employer by its board id; the board's own postings use the display
    name. Matched by name, "Ddome" and "DataDome" were never twins."""
    assert dedup_key("Senior Product Manager", "Ddome") == dedup_key(
        "Senior Product Manager", "DataDome", board="ddome"
    )
    assert dedup_key("Senior Product Manager", "Ddome") != dedup_key(
        "Senior Product Manager", "Ddome GmbH"
    )


def test_a_copy_that_lost_its_location_shares_the_key_of_the_board_it_came_from():
    """Arbeitnow re-publishes company boards with the location blanked and the board's slug for a
    name. Keyed on the town and the spelled-out name, the two could never be matched."""
    assert dedup_key("Software Engineer II", "wppmedia") == dedup_key(
        "Software Engineer II", "WPP Media"
    )


def test_folding_matches_what_postgres_indexes():
    from trouveur.models import fold

    # The trigram column is lower(f_unaccent(...)) in Postgres. If these diverge, substring search
    # silently stops matching the rows it indexed.
    assert fold("München") == "munchen"
    assert fold("Straße") == "strasse"
    assert fold("Wirtschaftsingenieur") == "wirtschaftsingenieur"
