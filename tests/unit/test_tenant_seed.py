"""Guards for the local tenant seed file.

The seed is a hand-edited stopgap, so its failure mode is a typo. Every one of these turns a typo
into a loud error at import rather than a board that 404s on every sweep until somebody reads the
failure count.
"""

from __future__ import annotations

import pytest

from trouveur.sources.errors import SourceError
from trouveur.sources.seed import DEFAULT_FILENAME, load_seed

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]


def write(tmp_path, body: str):
    path = tmp_path / DEFAULT_FILENAME
    path.write_text(body, encoding="utf-8")
    return path


def test_a_seed_file_is_cleaned_by_each_source_own_grammar(tmp_path):
    """A Greenhouse board is a slug; a Workday board is three facts with load-bearing capitals."""
    path = write(
        tmp_path,
        """
        greenhouse = ["  GitLab ", "https://job-boards.greenhouse.io/doctolib/"]
        workday = ["https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"]
        """,
    )

    seed = load_seed(path)

    assert seed["greenhouse"] == ["gitlab", "doctolib"]
    assert seed["workday"] == ["nvidia:wd5:NVIDIAExternalCareerSite"]


def test_a_repeated_board_is_registered_once(tmp_path):
    """Duplicates are harmless in a hand-edited file and must not become duplicate work."""
    path = write(tmp_path, 'greenhouse = ["gitlab", "GitLab", "gitlab"]')

    assert load_seed(path)["greenhouse"] == ["gitlab"]


def test_a_malformed_scope_is_rejected_at_import(tmp_path):
    """Otherwise it reaches source_tenant and 404s on every sweep, silently, for ever."""
    path = write(tmp_path, 'greenhouse = ["not a slug!"]')

    with pytest.raises(SourceError):
        load_seed(path)


def test_a_global_source_cannot_be_given_tenants(tmp_path):
    """Workable sweeps one corpus. A board listed under it would never be swept and never error."""
    path = write(tmp_path, 'workable = ["acme"]')

    with pytest.raises(SourceError, match="no tenants"):
        load_seed(path)


def test_an_unknown_source_names_the_ones_that_take_tenants(tmp_path):
    path = write(tmp_path, 'smartrecruiters = ["acme"]')

    with pytest.raises(SourceError, match="unknown source"):
        load_seed(path)


def test_a_source_given_something_other_than_a_list_is_rejected(tmp_path):
    path = write(tmp_path, 'greenhouse = "gitlab"')

    with pytest.raises(SourceError, match="must be a list"):
        load_seed(path)


def test_a_missing_file_says_how_to_make_one(tmp_path):
    with pytest.raises(SourceError, match=r"\.example"):
        load_seed(tmp_path / "absent.toml")


def test_the_committed_example_parses_and_lists_only_tenant_scoped_sources():
    """The example is the template every installation starts from; a broken one blocks setup.

    It also pins that no global source has crept into it -- boards listed under one would be
    silently ignored by every sweep.
    """
    from trouveur.sources.registry import SOURCES

    seed = load_seed(REPO_ROOT / f"{DEFAULT_FILENAME}.example")

    assert seed, "the example lists no boards; it is not a useful starting point"
    for source, scopes in seed.items():
        assert SOURCES[source].tenant_scoped, f"{source} takes no tenants"
        assert scopes, f"{source} is listed with no boards"
