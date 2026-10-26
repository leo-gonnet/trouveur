"""Source-layer exceptions.

Never raise a bare Exception and never swallow one. The pipeline isolates failures per source, and
that isolation is only useful if what it catches carries enough detail to diagnose from a log line.
"""

from __future__ import annotations

from datetime import timedelta

# Consecutive failures from one source that read as the source failing rather than its boards or
# postings, and how long it is then left alone.
SOURCE_BURST = 5
SOURCE_PAUSE = timedelta(hours=1)


class SourceError(Exception):
    """A source could not be swept. Carries a complete sentence, not a code."""


class FetchError(SourceError):
    """An HTTP request failed after exhausting retries."""


class PartitionOverflow(SourceError):
    """A partition holds more results than the source will let us page through.

    Raised rather than logged because the alternative is silently keeping the first N and calling
    the sweep a success, which is indistinguishable from complete coverage in every metric we
    record. The caller must either split the partition or mark the sweep incomplete.
    """


class SourceBlocked(FetchError):
    """The provider refused us: a 429 after every retry, a challenge page, or an empty 200.

    Never "no results". The source is paused and asked again later, more slowly.
    """


class SourceRefused(FetchError):
    """One HTTP 403. A private board says this too, so only a run of them is a block."""
