"""Mining a posting we already hold for links to boards we do not sweep.

The traps here are all silent ones: a link lost to an `href` that was stripped, a self-link
filling the table with leads naming boards we already have, and a filter so wide that the real
leads sit among the cookie policies.
"""

from __future__ import annotations

from trouveur.discovery import leads_from_posting, urls_in
from trouveur.discovery.resolve import resolve
from trouveur.models import LeadOrigin, LeadResult


def _mine(payload: str, *, source="arbeitsagentur", scope=None, url=None):
    return leads_from_posting(
        job_id=7, source=source, scope=scope, url=url, company="Beispiel GmbH",
        title="Prozessingenieur", location_text="Wien", payload=payload,
    )


def test_a_link_only_an_href_carries_is_still_found():
    """The reason mining reads the RAW payload and not `job.description`.

    `html_to_text` keeps "Apply here" and drops the URL, so a posting whose only board link is an
    anchor would yield nothing at all from the stored text -- and nothing would ever say so.
    """
    payload = '{"description": "<p>Interesse? <a href=\\"https://jobs.lever.co/acme/1\\">Apply '
    payload += 'here</a></p>"}'
    assert [lead.url for lead in _mine(payload)] == ["https://jobs.lever.co/acme/1"]


def test_an_entity_escaped_parameter_survives():
    """An href inside a payload arrives as `?a=1&amp;b=2`, and Greenhouse's embed script puts the
    board in exactly such a parameter. Without unescaping the board is unreadable."""
    payload = '<script src="https://boards.greenhouse.io/embed/job_board/js?for=acme&amp;x=1">'
    leads = _mine(payload)
    assert resolve(leads[0].url).scope == "acme"


def test_a_url_at_the_end_of_a_sentence_loses_its_full_stop():
    """A plain-text description is not markup, so nothing but the punctuation ends the URL."""
    assert urls_in("Bewerbung unter https://acme.breezy.hr/p/1a2b.") == [
        "https://acme.breezy.hr/p/1a2b"
    ]


def test_the_same_link_twice_is_one_lead():
    payload = "https://jobs.lever.co/acme/1 and again https://jobs.lever.co/acme/1"
    assert len(_mine(payload)) == 1


def test_a_posting_does_not_file_a_lead_for_its_own_board():
    """Otherwise every posting on a board we already sweep files a lead saying so.

    One row per posting, for ever, every one of them naming a board the crawl set already holds --
    and the handful of leads that point somewhere new sit among them.
    """
    leads = _mine(
        '{"absolute_url": "https://job-boards.greenhouse.io/beispiel/jobs/1"}',
        source="greenhouse", scope="beispiel",
        url="https://job-boards.greenhouse.io/beispiel/jobs/1",
    )
    assert leads == []


def test_another_companys_board_on_the_same_platform_is_a_lead():
    """The self-link rule is per BOARD, not per platform: an advert naming another company's
    Greenhouse board is exactly the lead we are mining for."""
    leads = _mine(
        '{"description": "see also https://job-boards.greenhouse.io/anderefirma/jobs/9"}',
        source="greenhouse", scope="beispiel",
        url="https://job-boards.greenhouse.io/beispiel/jobs/1",
    )
    assert [resolve(lead.url).scope for lead in leads] == ["anderefirma"]


def test_a_links_page_furniture_is_left_out():
    """Every posting carries its company's social accounts, its fonts and its cookie policy. Kept,
    they would outnumber the leads by an order of magnitude and make the table unreadable."""
    payload = (
        '{"description": "https://fonts.googleapis.com/css?family=Inter '
        "https://www.linkedin.com/company/beispiel https://beispiel.de/datenschutz "
        'https://twitter.com/beispiel"}'
    )
    assert _mine(payload) == []


def test_an_unreadable_link_that_is_plainly_a_job_is_kept():
    """These are the whole point of counting unreadable hosts: a company running its own careers
    page is a job we can see and cannot collect, and the count is what says how many."""
    leads = _mine('{"externeURL": "https://karriere.beispiel-gmbh.de/stellen/42"}')
    assert len(leads) == 1
    got = resolve(leads[0].url)
    assert got.result is LeadResult.UNKNOWN_HOST
    assert got.host == "karriere.beispiel-gmbh.de"


def test_a_board_link_is_kept_however_it_is_worded():
    """A URL a rule already recognises must never be dropped by the wording filter: Ashby's host
    carries no word in the list, and losing it would lose the platform."""
    leads = _mine('{"apply": "https://jobs.ashbyhq.com/anderefirma/a741b4e8"}')
    assert [resolve(lead.url).source for lead in leads] == ["ashby"]


def test_an_employers_own_advert_on_an_aggregator_is_the_lead():
    """What makes archive mining worth doing. An Arbeitsagentur advert states the employer's own
    application URL, and that URL is a board with the company's other postings on it."""
    leads = _mine(
        "{}", source="arbeitsagentur", scope=None,
        url="https://anderefirma.jobs.personio.de/job/12345",
    )
    assert len(leads) == 1
    assert leads[0].origin is LeadOrigin.ARCHIVE
    assert leads[0].company == "Beispiel GmbH"
    got = resolve(leads[0].url)
    assert (got.source, got.scope) == ("personio", "anderefirma")


def test_a_payload_that_is_not_text_yields_nothing_rather_than_raising():
    assert _mine(None) == []
    assert urls_in(None) == []


def test_an_absurdly_long_run_of_characters_is_not_a_url():
    """A tracking redirect chain or a base64 blob containing "http" is not a board link, and
    storing one would push a megabyte into a column nothing can read."""
    assert urls_in("https://x.de/" + "a" * 900) == []
