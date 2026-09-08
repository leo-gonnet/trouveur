"""Guards for the Greenhouse adapter, including the completeness rule that gates job closing."""

from __future__ import annotations

import httpx
import pytest

from trouveur.sources.greenhouse import GreenhouseSource, external_id, normalize


class StubClient:
    """Minimal stand-in for PoliteClient. No network anywhere in the suite."""

    def __init__(self, responses: dict[str, tuple[int, dict]]) -> None:
        self.responses = responses
        self.requested: list[str] = []

    async def get(self, url, *, params=None, headers=None):
        self.requested.append(url)
        status, payload = self.responses.get(url, (404, {"error": "not found"}))
        return httpx.Response(status, json=payload, request=httpx.Request("GET", url))


def _url(slug: str) -> str:
    return f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"


def test_the_board_registry_is_valid_and_non_empty():
    """A malformed slug in boards.txt must fail loudly, not 404 every day in silence."""
    from trouveur.sources.greenhouse.client import BOARDS_FILE
    from trouveur.sources.scopes import load_scopes

    boards = load_scopes(BOARDS_FILE)
    assert boards, "boards.txt is empty, so Greenhouse contributes nothing"
    assert len(boards) == len(set(boards)), "duplicate slugs double the requests to a tenant"


def test_a_source_reads_its_own_registry():
    """Nothing upstream should need to know that this source is tenant-scoped."""
    from trouveur.sources import build_sources

    greenhouse_source = next(s for s in build_sources() if s.name == "greenhouse")
    assert greenhouse_source.boards


def test_malformed_slugs_are_rejected_at_load(tmp_path):
    from trouveur.sources.errors import SourceError
    from trouveur.sources.scopes import load_scopes

    path = tmp_path / "boards.txt"
    path.write_text("good-slug\n# a comment\n\nhttps://example.com/oops\n")
    with pytest.raises(SourceError, match="not valid slugs"):
        load_scopes(path)


def test_comments_and_blank_lines_are_ignored(tmp_path):
    from trouveur.sources.scopes import load_scopes

    path = tmp_path / "boards.txt"
    path.write_text("# header\n\nalpha  # trailing\nbeta\nalpha\n")
    assert load_scopes(path) == ["alpha", "beta"]


def test_external_id_is_scoped_by_board():
    # Nothing documents Greenhouse ids as globally unique, and a collision across two tenants
    # would silently merge two unrelated postings onto one row.
    assert external_id("beispiel", 4001) == "beispiel:4001"
    assert external_id("other", 4001) != external_id("beispiel", 4001)


def test_description_is_unescaped_before_tags_are_stripped(gh_board):
    job = normalize(gh_board["jobs"][0], external_id="beispiel:4001")
    # content arrives as HTML that has itself been HTML-escaped. Stripping tags without
    # unescaping first leaves entity text in the description and strips nothing.
    assert "&lt;" not in job.description
    assert "<p>" not in job.description
    assert "process improvement" in job.description


def test_multiple_locations_are_split_and_kept_verbatim(gh_board):
    job = normalize(gh_board["jobs"][0], external_id="beispiel:4001")
    assert [location.raw for location in job.locations] == ["Remote, Germany", "Remote, Austria"]
    # Free text, so normalisation states no structure it was not given.
    assert all(location.country is None for location in job.locations)


def test_normalize_requires_the_scoped_external_id(gh_board):
    assert normalize(gh_board["jobs"][0]) is None


async def test_sweep_marks_each_fetched_board_closable(gh_board):
    client = StubClient({_url("beispiel"): (200, gh_board)})
    source = GreenhouseSource(boards=["beispiel"])
    batches = []

    outcome = await source.sweep(client, lambda docs: _collect(batches, docs))

    assert outcome.documents == 2
    assert outcome.expected == 2
    # A board dump is the complete live set, so its stale postings may be retired.
    assert outcome.closable_scopes == ["beispiel"]
    assert outcome.complete is True
    assert all(doc.scope == "beispiel" for batch in batches for doc in batch)
    # Recorded per tenant so the dashboard can show which board answered.
    assert [(r.scope, r.ok, r.documents) for r in outcome.scope_results] == [("beispiel", True, 2)]


async def test_a_failing_board_is_never_closable(gh_board):
    """One tenant's board failing must not retire another tenant's postings.

    This is the difference between a per-source completeness flag and a per-scope one: with a
    single flag, a 404 on one board would either block closing everywhere or, worse, close the
    postings of every board that did not answer.
    """
    client = StubClient({_url("good"): (200, gh_board)})
    source = GreenhouseSource(boards=["good", "gone"])

    outcome = await source.sweep(client, lambda docs: _collect([], docs))

    assert outcome.closable_scopes == ["good"]
    assert "gone" not in outcome.closable_scopes
    assert outcome.errors and "gone" in outcome.errors[0]
    assert outcome.partitions_done == 1
    assert outcome.partitions_total == 2
    failed = {r.scope: r for r in outcome.scope_results}
    assert failed["gone"].ok is False
    assert failed["good"].ok is True


async def _collect(sink: list, documents) -> None:
    sink.append(list(documents))
