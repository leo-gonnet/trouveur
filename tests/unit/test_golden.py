"""Golden files: the full output of normalisation and derivation, pinned per source.

The core of this system is two pure functions, and a change to either alters every posting in the
corpus. Example-based tests assert the handful of fields somebody thought to check; these assert
*everything*, so a derivation change that silently reclassifies a field nobody is watching shows up
as a diff in the pull request rather than as poor recall three weeks later.

Each case records the normaliser version it was generated at, and `derive_version.json` records
DERIVE_VERSION once for all of them, that being one number rather than one per source. Regenerating
after changing a derivation but without bumping the version produces a diff with changed facets and
an unchanged version number -- which is the signal a reviewer needs, because it means the corpus in
production still holds the old readings and no backfill has been scheduled.

    uv run pytest tests/unit/test_golden.py --update-goldens

Then read the diff. Never regenerate to make a red test green without reading what moved.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trouveur import versions
from trouveur.ingest.derive import derive
from trouveur.sources.registry import normalizer_for

CASES = Path(__file__).parent / "golden"
PINNED_DERIVE_VERSION = CASES / "derive_version.json"


def _cases() -> list[Path]:
    return sorted(CASES.glob("*/*.input.json"))


def _render(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    source = path.parent.name
    normalize, normalize_version = normalizer_for(source)

    job = normalize(
        payload["listing"], payload.get("detail"), external_id=payload.get("external_id")
    )
    if job is None:
        return {"normalize_version": normalize_version, "job": None, "facets": None}
    return {
        "normalize_version": normalize_version,
        "job": job.model_dump(mode="json"),
        "facets": derive(job).model_dump(mode="json"),
    }


def _dump(value: dict) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


@pytest.mark.parametrize("case", _cases(), ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_golden(case: Path, request: pytest.FixtureRequest):
    expected_path = case.with_name(case.name.replace(".input.json", ".expected.json"))
    actual = _render(case)

    if request.config.getoption("--update-goldens"):
        expected_path.write_text(_dump(actual), encoding="utf-8")
        pytest.skip("golden regenerated")

    assert expected_path.exists(), (
        f"{expected_path.name} is missing; run pytest --update-goldens and review the result."
    )
    expected = json.loads(expected_path.read_text(encoding="utf-8"))

    # Reported before the full comparison, because "the version moved" and "a field moved" are
    # different problems and the second is unreadable when the first is the cause.
    assert actual["normalize_version"] == expected["normalize_version"], (
        f"{case.parent.name} normalizer version changed "
        f"{expected['normalize_version']} -> {actual['normalize_version']}; regenerate goldens."
    )
    assert actual == expected, (
        f"{case.parent.name}/{case.stem} changed.\n"
        f"expected: {_dump(expected)}\nactual:   {_dump(actual)}\n"
        "If this is intended, regenerate with --update-goldens and bump the relevant version in "
        "trouveur/versions.py so the corpus is re-derived."
    )


def test_the_pinned_derive_version(request: pytest.FixtureRequest):
    if request.config.getoption("--update-goldens"):
        PINNED_DERIVE_VERSION.write_text(
            _dump({"derive_version": versions.DERIVE_VERSION}), encoding="utf-8"
        )
        pytest.skip("golden regenerated")
    recorded = json.loads(PINNED_DERIVE_VERSION.read_text(encoding="utf-8"))["derive_version"]
    assert recorded == versions.DERIVE_VERSION, (
        f"DERIVE_VERSION changed {recorded} -> {versions.DERIVE_VERSION}; regenerate goldens, "
        "and make sure a re-derive is queued for the existing corpus."
    )


def test_every_source_has_at_least_one_golden():
    """A source with no golden case is a normaliser nothing pins."""
    from trouveur.sources.registry import NORMALIZERS

    covered = {path.parent.name for path in _cases()}
    assert covered == set(NORMALIZERS), (
        f"sources without golden cases: {sorted(set(NORMALIZERS) - covered)}"
    )
