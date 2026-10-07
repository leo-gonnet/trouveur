"""Reading a reported link, and the one answer it earns.

Two things can fail here for a reason worth hearing about. The lookup is loose on purpose -- a
reader's link is almost never spelled the way we stored it -- and every spelling it does not try
is a job we tell them we never collected when we did. And the answer names one stage, so the
order it checks them in is the difference between "fix retrieval" and "widen your area".
"""

from __future__ import annotations

import pytest

from trouveur.discovery.reports import Facts, decide, id_tokens, url_variants
from trouveur.models import ReportOutcome, ReportReason


def test_the_spellings_tried_cover_how_a_reader_pastes_a_link():
    """Each of these is the same posting as the one we stored, written differently."""
    pasted = "http://WWW.Example.com/jobs/7/?utm_source=linkedin&utm_campaign=x"
    got = url_variants(pasted)
    # The tracking parameters are gone from these: where the reader found the job is not part
    # of which job it is, and the URL we stored carries neither.
    assert "https://example.com/jobs/7" in got, "no scheme, host or slash difference survives"
    assert "https://www.example.com/jobs/7/" in got
    assert pasted in got, "the link as pasted is tried too, whatever the rebuilding above makes"


def test_a_parameter_that_is_the_job_itself_is_kept():
    """Greenhouse's embed script names the posting in `gh_jid`. Dropped, the link would match
    every other job on the same board instead of this one."""
    got = url_variants("https://boards.greenhouse.io/embed/job_app?for=acme&gh_jid=42")
    assert all("gh_jid=42" in url for url in got)


# `https://[::1` is the one that used to RAISE rather than return nothing: splitting a netloc
# holding a bracket is a ValueError, and both of these run per report inside one transaction, so
# one such row stored before the route rejected them wedged the queue for every reader.
@pytest.mark.parametrize(
    "url",
    ["", "not a link", "mailto:jobs@example.com", "/careers", "https://[::1", "https://acme.de]"],
)
def test_a_link_we_cannot_parse_has_no_spellings(url):
    assert url_variants(url) == []
    # `id_tokens` is loose by design and reads whatever the path held, so what is asserted of it
    # here is only that it ANSWERS: a raise is what wedged the queue.
    assert isinstance(id_tokens(url, "acme"), list)


def test_an_encoded_parameter_is_not_rewritten_by_rebuilding():
    """The rebuilt spellings must stay comparable to the URL we stored.

    Rebuilding from `parse_qs` DECODED the value and no re-encoding puts it back: `%20` and `+`
    are both a space, so whichever is written the other stops matching. A reported job whose
    stored URL differed only by `www.` was then answered "we never had it" -- the mistake the
    module docstring calls expensive.
    """
    got = url_variants("https://www.acme.com/jobs?title=Senior%20Engineer&utm_source=x")
    assert all("title=Senior%20Engineer" in url for url in got)
    assert not any("utm_source" in url for url in got[1:])


def test_the_id_in_a_link_is_offered_both_bare_and_behind_its_board():
    """A board source scopes its ids by tenant, so the stored id is `acme:123` -- and a global
    source stores the bare one. The lookup cannot know which, so it offers both."""
    got = id_tokens("https://job-boards.greenhouse.io/acme/jobs/5745893004?gh_src=x", "acme")
    assert "acme:5745893004" in got
    assert "5745893004" in got
    assert id_tokens("https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-1-S", None) == [
        "10000-1-S", "jobdetail", "jobsuche"
    ]


def test_a_job_we_showed_is_told_as_shown():
    facts = Facts(source="greenhouse", scope="acme", swept=True, job_id=1, shown=True)
    assert decide(facts) == (ReportOutcome.HAD_AND_RECOMMENDED, None)


def test_the_answer_names_the_first_stage_that_stopped_it():
    """A posting outside the area was not retrieved either, and not scored either. Answering
    with a later stage sends somebody to look at a part that was working."""
    outside = Facts(source="greenhouse", scope="acme", swept=True, job_id=1, in_area=False)
    assert decide(outside) == (ReportOutcome.HAD_NOT_RECOMMENDED, ReportReason.LOCATION)
    unfound = Facts(source="greenhouse", scope="acme", swept=True, job_id=1)
    assert decide(unfound) == (ReportOutcome.HAD_NOT_RECOMMENDED, ReportReason.NOT_RETRIEVED)
    unscored = Facts(
        source="greenhouse", scope="acme", swept=True, job_id=1, retrieved=True
    )
    assert decide(unscored) == (ReportOutcome.HAD_NOT_RECOMMENDED, ReportReason.NOT_SCORED)


def test_a_board_we_sweep_that_lacks_the_job_is_not_the_same_as_a_board_we_do_not_have():
    """The sharp end of the whole feature. One says a sweep we already pay for lost a posting;
    the other says we never asked. Telling a reader the second about the first hides the bug."""
    swept = Facts(source="greenhouse", scope="acme", swept=True, job_id=None)
    assert decide(swept) == (ReportOutcome.MISSING_JOB, None)
    unswept = Facts(source="greenhouse", scope="neu", swept=False, job_id=None)
    assert decide(unswept) == (ReportOutcome.MISSING_BOARD, None)


def test_a_link_no_rule_reads_is_an_unknown_platform():
    assert decide(Facts(source=None, scope=None, swept=False)) == (
        ReportOutcome.UNKNOWN_PLATFORM, None
    )
