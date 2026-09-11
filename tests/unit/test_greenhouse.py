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


def test_a_tenant_scoped_source_is_dropped_when_it_has_no_tenants():
    """Sweeping it would make no requests, find nothing, and report a healthy empty sweep.

    That is indistinguishable from a source that is working and finding nothing, which is exactly
    the silent failure this project exists to avoid.
    """
    from trouveur.sources import build_sources
    from trouveur.sources.registry import SOURCES

    # Asserted against the registry rather than a hardcoded list, so adding a source cannot make
    # this pass for the wrong reason -- a new tenant-scoped source that forgot to declare itself
    # one would appear here rather than in a sweep that quietly does nothing.
    tenant_scoped = {name for name, spec in SOURCES.items() if spec.tenant_scoped}
    assert tenant_scoped, "no tenant-scoped sources; this test is not checking anything"
    assert not tenant_scoped & {s.name for s in build_sources()}
    assert "greenhouse" in [s.name for s in build_sources(tenants={"greenhouse": ["gitlab"]})]


def test_tenants_reach_the_source_without_the_caller_naming_it():
    """build_sources takes a mapping, so no caller branches on which sources are tenant-scoped."""
    from trouveur.sources import build_sources

    source = next(
        s for s in build_sources(tenants={"greenhouse": ["a", "b"]}) if s.name == "greenhouse"
    )
    assert source.boards == ["a", "b"]


def test_slugs_are_validated_and_urls_are_accepted():
    """Validation happens at the write, because a bad slug otherwise 404s silently forever."""
    from trouveur.sources.errors import SourceError
    from trouveur.sources.registry import clean_scope

    assert clean_scope("greenhouse", "  GitLab ") == "gitlab"
    assert clean_scope("greenhouse", "https://job-boards.greenhouse.io/doctolib/") == "doctolib"
    for bad in ("not a slug!", "", "https://example.com/"):
        with pytest.raises(SourceError):
            clean_scope("greenhouse", bad)


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
