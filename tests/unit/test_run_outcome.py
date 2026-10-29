"""The Runs table's result line, read from the report a run stored."""

from __future__ import annotations

from decimal import Decimal

from trouveur.web import outcome

NIGHTLY = {
    "summary": "greenhouse[error] docs=13156 ...",
    "sources": {
        "greenhouse": {"documents": 13156, "changed": 320, "closed": 299, "error": "HTTP 500"},
        "workday": {"documents": 79903, "changed": 3406, "closed": 3752, "error": None},
        "himalayas": {"documents": 0, "changed": 0, "closed": 0, "error": "429",
                      "blocked": "429 since 03:41"},
    },
    "matches": [
        {"user_id": 1, "scored": 40, "published": 5, "cost_usd": "0.1100"},
        {"user_id": 2, "scored": 2, "published": 0, "cost_usd": "0.0100"},
    ],
    "digests_sent": 1,
}


def test_a_nightly_report_adds_up_across_sources_and_readers():
    got = outcome.of(NIGHTLY)
    assert (got.changed, got.closed) == (3726, 4051)
    assert (got.scored, got.recommended, got.emails) == (42, 5, 1)
    assert got.cost_usd == Decimal("0.12")


def test_a_blocked_source_is_not_also_listed_as_failed():
    got = outcome.of(NIGHTLY)
    assert got.failed == ["greenhouse"]
    assert got.blocked == ["himalayas"]


def test_a_part_the_report_lacks_reads_as_unknown_not_zero():
    """A scoring-only run fetched nothing, and old reports have no `published`: a 0 there would
    claim the run looked and found nothing."""
    got = outcome.of({"matches": [{"user_id": 1, "scored": 3, "cost_usd": "0"}]})
    assert got.changed is None and got.emails is None
    assert got.scored == 3 and got.recommended is None
    assert outcome.of(None) == outcome.Outcome()
