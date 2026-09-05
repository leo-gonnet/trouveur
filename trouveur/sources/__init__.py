"""The catalogue of sources a user can activate.

Plain data, deliberately importing no adapter: the web UI renders this list, and importing the
adapters would drag optional dependencies (JobSpy pulls pandas) into the web process. Adding an
adapter means adding it here too, or it can never be switched on --
`test_every_source_the_pipeline_can_build_is_in_the_registry` guards that.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SourceInfo:
    name: str
    label: str
    description: str


AVAILABLE_SOURCES: tuple[SourceInfo, ...] = (
    SourceInfo(
        "arbeitsagentur",
        "Arbeitsagentur",
        "The German federal employment agency. The widest German coverage; searches your "
        "profile keywords in your profile cities.",
    ),
    SourceInfo(
        "karriere_at",
        "karriere.at",
        "The Austrian backbone. Recall scales with the number of profile keywords, so add "
        "keywords rather than expecting more pages.",
    ),
    SourceInfo(
        "personio",
        "Personio",
        "Career feeds of individual employers. Contributes nothing until you add companies on "
        "the Companies page -- Personio publishes no directory of its customers.",
    ),
    SourceInfo(
        "jobspy",
        "JobSpy",
        "Indeed, LinkedIn and Glassdoor through the JobSpy library. Optional dependency "
        "(uv sync --extra jobspy); yields nothing when it is not installed.",
    ),
)

SOURCE_NAMES: frozenset[str] = frozenset(info.name for info in AVAILABLE_SOURCES)
