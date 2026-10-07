"""The URL rules, against freehire's own test cases.

Ported with the rules from freehire (https://github.com/strelov1/freehire, MIT):
`internal/ingest/atsboard/board_test.go` and `internal/ingest/atsdetect/fromurl_test.go`. They are
here verbatim because each one is a real URL shape somebody had to go and look at, and a rule that
passes a URL invented by whoever wrote the rule proves nothing.

Only the cases that can fail for a reason worth hearing about are kept: the boards we sweep, and
the NEGATIVE cases on hosts we hold a rule for -- an embed path, a CDN asset, a per-job shortlink,
a bare apex. Their ~190 cases for platforms we hold no rule for are left out: every one of them
asserts that a host our table never mentions resolves to nothing, which is the default branch
tested once over and over.

Two expectations differ from freehire's on purpose, and both are stated in `resolve.py`:
a Workday board is `tenant:wdN:site` because that is what our adapter addresses it with, and the
Greenhouse embed script resolves here (they split that shape into a second function).
"""

from __future__ import annotations

import pytest

from trouveur.discovery import resolve
from trouveur.discovery.resolve import API_RULES, RULES, Mode
from trouveur.models import LeadResult
from trouveur.sources import registry
from trouveur.sources.errors import SourceError

# (url, expected source, expected scope) -- (None, None) for a URL that names no board.
PORTED: tuple[tuple[str, str | None, str | None], ...] = (
    ('https://job-boards.greenhouse.io/alpaca/jobs/5745893004?utm=x#top', 'greenhouse', 'alpaca'),
    ('https://job-boards.greenhouse.io/alpaca', 'greenhouse', 'alpaca'),
    ('https://jobs.lever.co/offchainlabs/52c01c91/apply', 'lever', 'offchainlabs'),
    ('https://jobs.eu.lever.co/coinspaid/244123b5-ffbb/apply?x=1', 'lever', 'coinspaid'),
    ('https://jobs.ashbyhq.com/blitzy/a741b4e8-8799', 'ashby', 'blitzy'),
    ('https://job-boards.greenhouse.io/embed/job_app?token=1', None, None),
    ('https://boards.greenhouse.io/embed/job_board/js?for=acme', 'greenhouse', 'acme'),
    ('https://job-boards.cdn.greenhouse.io/assets/entry-ZTzpC0b7.css', None, None),
    ('https://job-boards.eu.greenhouse.io/lionhires/jobs/4941013101', 'greenhouse', 'lionhires'),
    (
        'https://api.ashbyhq.com/posting-api/job-board/phantom?includeCompensation=false',
        'ashby',
        'phantom',
    ),
    ('https://boards-api.greenhouse.io/v1/boards/anthropic/jobs', 'greenhouse', 'anthropic'),
    ('https://api.lever.co/v0/postings/matchgroup?mode=json', 'lever', 'matchgroup'),
    ('https://api.ashbyhq.com/posting-api/job-board', None, None),
    ('https://api.lever.co/v1/something/else', None, None),
    ('https://ats.rippling.com/en-GB/satomic/jobs/48384892-1b6b?utm=x', 'rippling', 'satomic'),
    ('https://ats.rippling.com/satomic/jobs/34aaf2aa', 'rippling', 'satomic'),
    ('https://ats.rippling.com/satomic', 'rippling', 'satomic'),
    ('https://ats.rippling.com/en-GB', None, None),
    ('https://acme.jobs.personio.com/job/9', 'personio', 'acme'),
    (
        'https://reflex-aerospace-gmbh.jobs.personio.de/job/2679152?display=en#apply',
        'personio',
        'reflex-aerospace-gmbh',
    ),
    (
        'https://generalmotors.wd5.myworkdayjobs.com/Careers_GM/job/Austin/Senior-Software-Engineer_JR-202614238',
        'workday',
        'generalmotors:wd5:Careers_GM',
    ),
    (
        'https://generalmotors.wd5.myworkdayjobs.com/Careers_GM',
        'workday',
        'generalmotors:wd5:Careers_GM',
    ),
    (
        'https://goodyear.wd1.myworkdayjobs.com/goodyearcareers/job/x',
        'workday',
        'goodyear:wd1:goodyearcareers',
    ),
    (
        'https://gm.wd5.myworkdayjobs.com/en-US/Careers_GM/job/x/Eng_JR-1',
        'workday',
        'gm:wd5:Careers_GM',
    ),
    ('https://generalmotors.wd5.myworkdayjobs.com', None, None),
    ('https://salesforce.wd12.myworkdayjobs.com/en-US', None, None),
    (
        'https://trumpf.wd3.myworkdayjobs.com/en-us/trumpf_students/job/apodaca/ar_r1',
        'workday',
        'trumpf:wd3:trumpf_students',
    ),
    (
        'https://wmg.wd1.myworkdayjobs.com/de-De/wmgglobal/job/berlin/artist_jr1',
        'workday',
        'wmg:wd1:wmgglobal',
    ),
    ('https://acme.wd1.myworkdayjobs.com/job/Berlin/Engineer_R-1', None, None),
    ('https://acme.wd1.myworkdayjobs.com/details/Engineer_R-1', None, None),
    ('https://acme.wd1.myworkdayjobs.com/en-US/job/Berlin/Engineer_R-1', None, None),
    ('https://apply.workable.com/j/EF5014296F/apply', None, None),
    ('https://apply.workable.com/acme/j/EF5014296F/', 'workable', 'acme'),
    ('https://jobs.ashbyhq.com', None, None),
    ('https://jobs.personio.com', None, None),
    ('http://app4.greenhouse.io/ai_opt_out_request/job_post/6178374004/ai_opt_out', None, None),
    ('https://my.greenhouse.io/ai_opt_out_request', None, None),
    (
        'https://trumpf.wd3.myworkdayjobs.com/en-us/trumpf_students/job/apodaca-mexico/ar-student_r00040838',
        'workday',
        'trumpf:wd3:trumpf_students',
    ),
    (
        'https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/US/role_JR123',
        'workday',
        'nvidia:wd5:NVIDIAExternalCareerSite',
    ),
    (
        'https://wmg.wd1.myworkdayjobs.com/de-DE/wmgglobal/job/berlin/artist_jr1',
        'workday',
        'wmg:wd1:wmgglobal',
    ),
    ('https://job-boards.greenhouse.io/avepoint/jobs/6899160', 'greenhouse', 'avepoint'),
    (
        'https://job-boards.eu.greenhouse.io/amoriabond/jobs/4878751101',
        'greenhouse',
        'amoriabond',
    ),
    ('https://jobs.lever.co/findhelp/abc-123', 'lever', 'findhelp'),
    (
        'https://jobs.ashbyhq.com/llamaindex/156d7573-82dd-4973-87ab-7d5a056650d5',
        'ashby',
        'llamaindex',
    ),
    # Shapes freehire's own tests do not reach, written from the board URL our adapter
    # addresses. Breezy is their one rule for a tenant-scoped source of ours with no case.
    ('https://acme.breezy.hr/p/1a2b3c4d-senior-engineer', 'breezy', 'acme'),
    ('https://acme.breezy.hr', 'breezy', 'acme'),
    ('https://app.breezy.hr/dashboard', None, None),
    ('https://breezy.hr/pricing', None, None),
)


@pytest.mark.parametrize(("url", "source", "scope"), PORTED, ids=[case[0] for case in PORTED])
def test_a_ported_url_resolves_to_the_board_freehire_resolves_it_to(url, source, scope):
    got = resolve(url)
    if source is None:
        assert got.source is None and got.scope is None, (
            f"{url} named no board for freehire and named {got.source}/{got.scope} here; a board "
            "we invent is swept nightly for ever and 404s every time"
        )
        return
    assert (got.source, got.scope) == (source, scope)


def test_every_board_we_can_sweep_is_reported_as_resolved():
    """A board behind a platform we have an adapter for must not read as one we cannot sweep."""
    for url, source, _ in PORTED:
        if source in registry.SOURCES:
            assert resolve(url).result is LeadResult.RESOLVED, url


def test_every_tenant_scoped_source_has_a_rule_and_a_case():
    """A source whose boards no link can resolve to can only ever be filled by hand.

    This is the one that catches a source added later: the registry gains it, the sweep works, and
    discovery silently never proposes a board for it.
    """
    with_rules = {rule.source for rule in RULES} | {source for _, source, _ in API_RULES}
    covered = {source for _, source, _ in PORTED if source}
    missing = [
        name
        for name, spec in registry.SOURCES.items()
        if spec.tenant_scoped and (name not in with_rules or name not in covered)
    ]
    assert not missing, f"tenant-scoped sources no URL rule or no test case reaches: {missing}"


def test_a_scope_we_resolve_is_one_the_sweep_would_accept():
    """The scope is copied verbatim into the crawl set, so it has to pass the source's grammar.

    Not a restatement of `resolve`: a rule whose mode extracts the wrong part of the URL produces
    a plausible-looking string, and the only thing that knows it is wrong is `clean_scope`.
    """
    for url, source, scope in PORTED:
        spec = registry.SOURCES.get(source) if source else None
        if spec is None or not spec.tenant_scoped:
            continue
        try:
            cleaned = registry.clean_scope(source, scope)
        except SourceError as exc:  # pragma: no cover - the assertion below is the report
            pytest.fail(f"{url} resolved to a scope {source} rejects: {exc}")
        assert cleaned == scope, f"{url} resolved to {scope!r}, which cleans to {cleaned!r}"


def test_an_unreadable_link_keeps_its_host():
    """The host is the whole signal: a host that keeps appearing is a platform asking for a rule.

    Dropping it is what makes a missing platform invisible -- the count is the only thing that
    says Trouveur is losing postings to a platform nobody has looked at.
    """
    got = resolve("https://careers.example-gmbh.de/stellenangebote/12")
    assert got.result is LeadResult.UNKNOWN_HOST
    assert got.host == "careers.example-gmbh.de"


@pytest.mark.parametrize("url", ["", None, "not a url", "mailto:jobs@example.com", "/careers"])
def test_a_link_we_cannot_parse_is_no_link(url):
    assert resolve(url).result is LeadResult.NO_URL


def test_a_platform_we_hold_no_rule_for_is_counted_by_host():
    """We carry a rule only for a platform we can sweep, so a whole ATS reads as an unread host.

    That is the intended answer, not a gap: the count is what says the platform is worth a rule,
    and the lead keeps its raw URL, so porting the rule later re-reads every one of these with no
    new request. The host is a platform's, not one company's, which is what the panel shows.
    """
    got = resolve("https://jobs.smartrecruiters.com/BHFT/744000139104759-senior-officer")
    assert got.result is LeadResult.UNKNOWN_HOST
    assert got.host == "jobs.smartrecruiters.com"
    assert (got.source, got.scope) == (None, None)


def test_every_rule_names_a_source_the_registry_knows():
    """We hold a rule only for a source we can sweep, and `_for_source` indexes the registry
    with it rather than guarding -- so a rule for anything else is a KeyError in the runner.

    It is also what the whole design rests on: a board filed under a name no adapter answers to
    is a board nothing ever sweeps, and nothing anywhere would say so.
    """
    sources = {rule.source for rule in RULES} | {source for _, source, _ in API_RULES}
    unknown = sorted(sources - set(registry.SOURCES))
    assert not unknown, (
        f"rules name {unknown}, which no adapter answers to. Port a rule when the adapter for it "
        "is written, not before."
    )


def test_a_subdomain_rule_declines_the_platforms_own_hosts():
    """In subdomain mode the label IS the board, so the vendor's own app becomes a company.

    Every tenant's career site links to it, which is how it gets into a payload in the first
    place, and the board it names would 404 on every sweep for ever.
    """
    assert resolve("https://breezy.hr/pricing").source is None, "the bare apex is no tenant"
    assert resolve("https://app.breezy.hr/dashboard").source is None
    assert resolve("https://acme.breezy.hr/p/1a2b").scope == "acme"


def test_the_table_holds_no_rule_for_a_platform_we_cannot_sweep():
    """The decision this file turns on, pinned so it cannot drift back by accident.

    Rules for platforms with no adapter cost a row each and buy nothing: the link is counted by
    host either way, and a lead keeps its raw URL, so porting the rule the day the adapter lands
    re-reads every one of them for free. freehire has about ninety more if one is needed.
    """
    assert {rule.source for rule in RULES} == {
        name for name, spec in registry.SOURCES.items() if spec.tenant_scoped
    } | {"workable"}, (
        "the rules must be exactly the tenant-scoped sources, plus Workable, whose whole corpus "
        "we sweep globally and whose links would otherwise read as a platform to go and build"
    )


def test_every_rule_uses_a_mode_that_has_a_branch():
    """A mode with no branch of its own falls through and reads whatever the URL leads with.

    freehire shipped exactly that: the first-path-segment rule was an implicit tail after the
    switch, so every mode added later silently inherited it.
    """
    for rule in RULES:
        assert isinstance(rule.mode, Mode)
        url = f"https://board.{rule.hosts[0]}.com/acme/jobs/1"
        resolve(url)  # must not raise, whatever the mode
