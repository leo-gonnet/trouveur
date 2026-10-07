"""Discovery: one job seen somewhere, what its link turned out to be, and what we could say about
it when a reader asked.

A lead is the unit every lead source writes -- archive mining, a reader's report, an aggregator
search -- and the resolver reads. It is deliberately not a posting: we may never fetch it, and
what we want from it is the *board* behind the link, which brings that company's other postings
with it.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class LeadOrigin(StrEnum):
    """Where we saw the job. Each value is one lead source, and more arrive with later tasks."""

    ARCHIVE = "archive"
    USER_REPORT = "user_report"


class LeadResult(StrEnum):
    """What the resolver made of the link.

    A pure function of the URL and RESOLVE_VERSION, which is what lets a version bump recompute
    every stored lead. Nothing here depends on what else we happen to hold: whether a resolved
    board is one we already sweep is a join against `source_tenant`, not a verdict frozen into
    the row -- it changes the day somebody promotes the board, and a stale verdict reads as fact.
    """

    # A board one of our adapters can sweep. The scope is in the source's own grammar.
    RESOLVED = "resolved"
    # No rule matched, or the rule matched and the board it read was not usable. The host is kept
    # either way: a host that keeps appearing is how a platform worth a rule announces itself --
    # it may be a whole ATS we have no adapter for, or one company's own careers page.
    UNKNOWN_HOST = "unknown_host"
    # The lead names no link at all, so there is nothing to resolve now or after a version bump.
    NO_URL = "no_url"


class Lead(BaseModel):
    """One job seen somewhere. `url` is what we actually saw, never a tidied version of it.

    Every field but the origin is optional because a lead source states what it knows: archive
    mining has the company and title of the posting the link sat in, a reader's report may have
    only a link.
    """

    origin: LeadOrigin
    url: str | None = None
    # The posting the link was found in, for an archive lead. Its raw documents are the input the
    # lead was derived from, and they are kept for ever, so a re-mine needs no new request.
    job_id: int | None = None
    company: str | None = None
    title: str | None = None
    location_text: str | None = None


class ReportOutcome(StrEnum):
    """What we could tell a reader about the job they found somewhere else.

    Recorded once, when the runner answers the report, and never recomputed: it is what the
    reader was TOLD, the same reason an edition is stored rather than derived. The corpus moves
    -- the board gets promoted, the posting arrives a week later -- and an answer silently
    rewritten under the reader says we had something we did not have on the day they asked.
    """

    # We held the posting and it reached one of their editions.
    HAD_AND_RECOMMENDED = "had_and_recommended"
    # We held it and never showed it. `ReportReason` says what stopped it, which is the whole
    # value of this box: it is the only place a reader tells us about a job they wanted.
    HAD_NOT_RECOMMENDED = "had_not_recommended"
    # We sweep the board (or the whole platform) and do not hold this posting. A gap in a sweep
    # we already pay for: a page cap, a closed posting, or a filter in the adapter.
    MISSING_JOB = "missing_job"
    # The link named a board we can sweep and do not. The lead is filed and the board proposed,
    # so this one answers itself once somebody promotes it.
    MISSING_BOARD = "missing_board"
    # No URL rule read the link. The lead keeps it, so the day a rule for that platform lands,
    # every report like it resolves without asking anybody for anything.
    UNKNOWN_PLATFORM = "unknown_platform"


class ReportReason(StrEnum):
    """Why a posting we held never reached the reader. Set only for HAD_NOT_RECOMMENDED.

    One reason, in the order the pipeline would have stopped it, so it names the FIRST thing that
    did: a posting outside the area is never retrieved either, and reporting the later stage
    would send somebody to fix the wrong one.
    """

    # The location filter -- the only hard filter -- rejected it for this reader's area.
    LOCATION = "location"
    # It passed the filter and retrieval never found it: the recall bug worth hearing about.
    NOT_RETRIEVED = "not_retrieved"
    # Retrieved, and no score ever reached an edition: scoring off, no credit, or the daily
    # ceiling. Costs nothing to fix and is invisible from the reader's side.
    NOT_SCORED = "not_scored"
