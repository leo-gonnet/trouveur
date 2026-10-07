"""Discovery: one job seen somewhere, and what its link turned out to be.

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
