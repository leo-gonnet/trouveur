"""Reading a board list nobody wrote for us: which column is the link, and what each row says."""

from __future__ import annotations

import json

from trouveur.discovery.lists import leads_from, records
from trouveur.discovery.resolve import resolve
from trouveur.models import LeadOrigin


def _leads(text: str, suffix: str = ".csv"):
    return leads_from(records(text, suffix), "test-list")


def test_a_name_slug_url_list_files_the_board_links_with_their_company():
    leads = _leads(
        "name,slug,url\n"
        "Booking.com,booking,https://job-boards.greenhouse.io/booking\n"
        "Nahe GmbH,nahegmbh,https://nahegmbh.jobs.personio.de\n"
    )
    assert [(lead.url, lead.company) for lead in leads] == [
        ("https://job-boards.greenhouse.io/booking", "Booking.com"),
        ("https://nahegmbh.jobs.personio.de", "Nahe GmbH"),
    ]
    assert {lead.origin for lead in leads} == {LeadOrigin.LIST}
    assert {lead.list_name for lead in leads} == {"test-list"}
    assert resolve(leads[0].url).scope == "booking"


def test_the_column_holding_most_links_wins_over_a_company_name_that_looks_like_a_host():
    leads = _leads(
        "company;careers\n"
        "booking.com;https://jobs.lever.co/booking\n"
        "Nahe GmbH;https://jobs.lever.co/nahe\n"
    )
    assert [lead.url for lead in leads] == [
        "https://jobs.lever.co/booking",
        "https://jobs.lever.co/nahe",
    ]


def test_a_careers_link_beats_a_homepage_when_both_columns_are_full():
    leads = _leads(
        "company\twebsite\tcareers_page\n"
        "Nahe\thttps://nahe.example\thttps://jobs.lever.co/nahe\n"
    )
    assert [lead.url for lead in leads] == ["https://jobs.lever.co/nahe"]


def test_a_file_of_bare_links_has_no_header_to_lose():
    leads = _leads("https://jobs.lever.co/eins\njobs.lever.co/zwei\n", ".txt")
    assert [lead.url for lead in leads] == [
        "https://jobs.lever.co/eins",
        "https://jobs.lever.co/zwei",
    ]
    assert resolve(leads[1].url).scope == "zwei"


def test_an_api_page_is_read_through_its_envelope():
    page = {
        "data": [
            {"title": "Prozessingenieur", "company": "Nahe GmbH", "location": "Wien, at",
             "url": "https://jobs.lever.co/nahe/1?utm_source=example"},
            {"title": "Kein Link", "company": "Ohne GmbH", "url": None},
        ],
        "meta": {"total": 2, "limit": 100, "offset": 0},
    }
    leads = _leads(json.dumps(page), ".json")
    assert [(lead.url, lead.title, lead.location_text) for lead in leads] == [
        ("https://jobs.lever.co/nahe/1?utm_source=example", "Prozessingenieur", "Wien, at"),
    ]


def test_a_row_without_a_link_is_skipped_not_filed_empty():
    leads = _leads("name,url\nEins,https://jobs.lever.co/eins\nZwei,\nDrei,n/a\n")
    assert [lead.company for lead in leads] == ["Eins"]
