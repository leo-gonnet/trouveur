"""A link -> the board behind it, from the URL alone. Pure: no network, no clock, no database.

Ported from freehire (https://github.com/strelov1/freehire, MIT): `internal/ingest/atsboard`
supplies the host table and the extraction modes, `internal/ingest/atsdetect` the Greenhouse embed
shape. Their test cases are ported too, in tests/unit/test_resolve.py. Porting rather than writing
these from scratch matters because every one of them is a trap somebody already fell into: the
notes on each row are theirs.

Two rules shape everything here:

- **Fail safe.** A missing or wrong row makes a link *unrecognised*, never a wrong board. A board
  we invent is swept nightly for ever, 404s every time, and shows up only as a rising failure
  count; a link we failed to read is counted by host and asks for a rule.
- **The scope must be the one the adapter addresses the board with**, not the one the URL reads
  most naturally. It is copied verbatim into `source_tenant`, so a truncated one is a board the
  crawl rejects. Which is why every scope goes through `registry.clean_scope` -- the source's own
  grammar -- rather than being trusted as extracted.

**There is one rule per source we sweep, and no others.** freehire's table covers about ninety more
platforms (SmartRecruiters, Teamtailor, Recruitee, BambooHR, Softgarden, UKG, ADP, iCIMS and so
on), several with an extraction mode of their own; port the one you need when the adapter for it is
written, and not before. A rule for a platform we cannot sweep buys nothing: the link is counted
by host either way, every lead keeps its raw URL, and bumping RESOLVE_VERSION the day the rule
lands re-reads all of them with no new request to anybody.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import parse_qs, urlsplit

from trouveur.models import LeadResult
from trouveur.sources import registry
from trouveur.sources.errors import SourceError


class Mode(StrEnum):
    """Where in the URL the board sits. Named as freehire names them, so the two can be diffed."""

    # The first path segment that is not platform machinery: jobs.lever.co/<board>/...
    PATH = "path"
    # Same, after dropping a leading xx-XX locale the board API does not use.
    PATH_LOCALE = "pathlocale"
    # The leftmost DNS label under the apex: <board>.breezy.hr
    SUBDOMAIN = "subdomain"
    # Host plus the first path segment: a Workday tenant is the host and the site is the segment.
    HOST_PATH = "hostpath"


@dataclass(frozen=True)
class Rule:
    hosts: tuple[str, ...]
    source: str
    mode: Mode
    # Leading path segments that are the platform's own machinery. Skipped to reach the board
    # behind them; a path of nothing else is declined rather than turned into a false board.
    reserved: tuple[str, ...] = ()
    # Leading segments that mean the link carries NO board. Unlike `reserved` these are not
    # skipped: behind Workable's "/j/" sits the JOB's id, and reading it as a company is worse
    # than reading nothing.
    no_board: tuple[str, ...] = ()
    # Where the board sits when the path holds only machinery: Greenhouse's embed script names it
    # in `?for=`, and that script is how most company careers pages mention their board at all.
    param: str | None = None


# An ATS's own API host, where the board sits right after a fixed prefix. These are not links a
# person pastes; they are the request a careers page on the company's OWN domain makes to load
# its listing, and they often name the board when nothing else on the page does.
#
# Matched before the host table, which also settles a shadowing bug of freehire's:
# boards-api.greenhouse.io is a subdomain of greenhouse.io, so the path rule read the API version
# as the board and returned greenhouse/"v1".
API_RULES: tuple[tuple[str, str, str], ...] = (
    ("api.ashbyhq.com", "ashby", "posting-api/job-board"),
    ("boards-api.greenhouse.io", "greenhouse", "v1/boards"),
    ("api.lever.co", "lever", "v0/postings"),
)

# Hosts under a supported platform's domain that serve the platform's own infrastructure, so no
# path on them names a board. `_PLATFORM_LABELS` cannot reach these: it reads the leftmost label,
# and job-boards.cdn.greenhouse.io leads with the same "job-boards" the real board hosts do.
NO_BOARD_HOSTS = frozenset({"job-boards.cdn.greenhouse.io"})

RULES: tuple[Rule, ...] = (
    Rule(
        ("greenhouse.io",), "greenhouse", Mode.PATH,
        reserved=("embed", "job_app", "job_board", "js"),
        # One shared AI-screening opt-out endpoint, served identically to every customer. It
        # belongs here and not in `reserved` because the segment behind it is the platform's own
        # "job_post": skipping would move the false board one segment along, not decline it.
        no_board=("ai_opt_out_request",),
        param="for",
    ),
    Rule(("jobs.lever.co", "jobs.eu.lever.co"), "lever", Mode.PATH),
    Rule(("jobs.ashbyhq.com",), "ashby", Mode.PATH),
    # The public site prefixes the board with a locale its board API omits, so both shapes have
    # to land on one board.
    Rule(("ats.rippling.com",), "rippling", Mode.PATH_LOCALE),
    Rule(("breezy.hr",), "breezy", Mode.SUBDOMAIN),
    Rule(("jobs.personio.com", "jobs.personio.de"), "personio", Mode.SUBDOMAIN),
    Rule(("myworkdayjobs.com",), "workday", Mode.HOST_PATH),
    # We sweep Workable's whole corpus from its public search, so a Workable link adds no board.
    # It is listed anyway: counted as an unread host it would read as a platform to go and build.
    Rule(("apply.workable.com",), "workable", Mode.PATH, no_board=("j",)),
)

# Leftmost DNS labels a multi-tenant platform uses for its own product hosts rather than for a
# tenant. In subdomain mode the label IS the board, so without this the platform's own app --
# which every tenant's career site links to -- reads as a company called "app".
_PLATFORM_LABELS = frozenset(
    {"app", "dashboard", "admin", "api", "support", "help", "blog", "docs"}
)

# An xx-XX language-COUNTRY locale, the optional leading segment Rippling's public site and
# Workday's public URLs insert before the board. The country half is matched case-insensitively:
# the canonical spelling is xx-XX, but real Workday links use the lowercase form, and reading
# "en-us" as a site names a board that does not exist.
_LOCALE = re.compile(r"^[a-z]{2}-[A-Za-z]{2}$")

# Workday segments that are the POSTING rather than a career site. Taking one as the site yields
# "<host>/job" -- a board that does not exist but looks new.
_WORKDAY_NOT_A_SITE = frozenset({"job", "details"})


@dataclass(frozen=True)
class Resolution:
    """What a link turned out to be. `source` and `scope` are set only once a board was read."""

    result: LeadResult
    host: str | None = None
    source: str | None = None
    scope: str | None = None

    @property
    def board(self) -> tuple[str, str] | None:
        if self.result is LeadResult.RESOLVED and self.source and self.scope:
            return self.source, self.scope
        return None


def resolve(url: str | None) -> Resolution:
    """The board a link addresses, or why it names none."""
    parts = urlsplit((url or "").strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return Resolution(LeadResult.NO_URL)

    host = parts.hostname.removeprefix("www.")
    if host in NO_BOARD_HOSTS:
        return Resolution(LeadResult.UNKNOWN_HOST, host=host)

    found = _api_board(host, parts.path) or _rule_board(host, parts)
    if found is None:
        return Resolution(LeadResult.UNKNOWN_HOST, host=host)
    source, scope = found
    return _for_source(host, source, scope)


def _for_source(host: str, source: str, scope: str) -> Resolution:
    """Hand the extracted scope to the source that will sweep it.

    `clean_scope` is the source's own grammar and the same function an operator's `tenants add`
    goes through, so a lead can never register a board shape the sweep would reject. A scope it
    refuses means the link was not a board URL after all, however much its host looked like one.

    Indexed, not `.get()`: a rule naming a source the registry does not have is a mistake to fix,
    not a state to carry, and a test pins every rule's source to the registry.
    """
    if not registry.SOURCES[source].tenant_scoped:
        # The source sweeps one global corpus, so there is no board to register. Still resolved:
        # the platform is covered, which is a different answer from "we cannot read this link".
        return Resolution(LeadResult.RESOLVED, host=host, source=source, scope=scope)
    try:
        cleaned = registry.clean_scope(source, scope)
    except SourceError:
        return Resolution(LeadResult.UNKNOWN_HOST, host=host)
    return Resolution(LeadResult.RESOLVED, host=host, source=source, scope=cleaned)


def _api_board(host: str, path: str) -> tuple[str, str] | None:
    for api_host, source, prefix in API_RULES:
        if host != api_host:
            continue
        rest = path.strip("/")
        if not rest.startswith(f"{prefix}/"):
            return None
        board = rest[len(prefix) + 1 :].split("/", 1)[0]
        return (source, board) if board else None
    return None


def _match_host(host: str) -> tuple[Rule, str] | None:
    """The rule for a host, with the apex it matched on: the host itself or a subdomain of it."""
    for rule in RULES:
        for entry in rule.hosts:
            if host == entry or host.endswith(f".{entry}"):
                return rule, entry
    return None


def _rule_board(host: str, parts) -> tuple[str, str] | None:
    matched = _match_host(host)
    if matched is None:
        return None
    rule, apex = matched
    segments = [segment for segment in parts.path.split("/") if segment]

    if rule.mode is Mode.SUBDOMAIN:
        if _is_platform_host(host):
            return None
        label = _subdomain_label(host, apex)
        return (rule.source, label) if label else None

    if rule.mode is Mode.HOST_PATH:
        site = _after_locale(segments)
        if not site or site[0] in _WORKDAY_NOT_A_SITE:
            return None
        # The host carries the tenant and the instance, the segment the site, and the scope is all
        # three -- which is exactly what `clean_scope` reads out of a careers URL, so hand it one.
        return rule.source, f"https://{host}/{site[0]}"

    if rule.mode is Mode.PATH_LOCALE:
        after = _after_locale(segments)
        return (rule.source, after[0]) if after else None

    if segments and segments[0] in rule.no_board:
        return None
    for segment in segments:
        if segment not in rule.reserved:
            return rule.source, segment
    # Nothing but the platform's own path words, so the board is wherever the platform puts it
    # instead -- Greenhouse's embed script carries it in a query parameter.
    if rule.param:
        values = parse_qs(parts.query).get(rule.param) or []
        if values and values[0]:
            return rule.source, values[0]
    return None


def _is_platform_host(host: str) -> bool:
    label, _, rest = host.partition(".")
    return bool(rest) and label in _PLATFORM_LABELS


def _subdomain_label(host: str, apex: str) -> str:
    """The leftmost label under the apex, and nothing when more than one label remains.

    These adapters fetch "<board>.<apex>", so a nested host like uk-ext.eu.breezy.hr has no such
    form: taking "uk-ext" names a host that does not exist.
    """
    if not host.endswith(f".{apex}"):
        return ""
    label = host.removesuffix(f".{apex}")
    return "" if "." in label else label


def _after_locale(segments: list[str]) -> list[str]:
    if segments and _LOCALE.match(segments[0]):
        return segments[1:]
    return segments
