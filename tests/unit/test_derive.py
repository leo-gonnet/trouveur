"""Guards for deterministic derivation.

Most of these encode a wrong answer that derivation used to give confidently. A bad facet never
raises: it silently poisons every filter and ranking that reads it, so each of these is a bug
that would otherwise be found by a user wondering why a job never appeared.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from trouveur.ingest.derive import derive
from trouveur.models import (
    CanonicalJob,
    EmploymentType,
    Location,
    SalaryPeriod,
    SalaryQuote,
    Seniority,
    WorkMode,
)
from trouveur.sources.arbeitsagentur import normalize as aa_normalize
from trouveur.sources.greenhouse import normalize as gh_normalize


def _job(**kwargs) -> CanonicalJob:
    return CanonicalJob(
        source="test", external_id="1", url="https://example.test", **kwargs
    )


def test_structured_address_is_used_without_reparsing(aa_listing):
    facets = derive(aa_normalize(aa_listing))
    assert facets.countries == ["DE"]
    assert facets.regions == ["Bayern"]
    # The disambiguating suffix Arbeitsagentur appends is dropped, the city is not.
    assert facets.cities == ["München"]


def test_free_text_location_is_parsed_only_where_structure_is_absent():
    facets = derive(_job(title="Engineer", locations=[Location(raw="Bangalore, India")]))
    assert facets.countries == ["IN"]
    assert facets.cities == ["Bangalore"]


def test_every_named_country_survives_a_multi_location_posting(gh_board):
    job = gh_normalize(gh_board["jobs"][0], external_id="beispiel:4001")
    facets = derive(job)
    # Collapsing these to one value would silently drop half of what a country filter matches on.
    assert set(facets.countries) == {"DE", "AT"}
    assert facets.work_mode is WorkMode.REMOTE


@pytest.mark.parametrize(
    "spelling", ["Wien", "Vienna", "Vienne", "Wien 10., Favoriten", "1100 Wien"]
)
def test_every_spelling_of_a_town_resolves_to_one_place(spelling):
    """A city filter compares ids, never names: `Wien` and `Vienna` stored as written were two
    different values, and a filter on one silently dropped the other."""
    facets = derive(_job(title="Engineer", locations=[Location(raw=spelling, city=spelling,
                                                               country="Österreich")]))
    assert facets.place_ids == [2761369]
    assert facets.unplaced_countries == []


def test_a_transliterated_town_resolves_like_its_umlauted_form():
    facets = derive(_job(title="Engineer", locations=[Location(raw="Muenchen, Deutschland")]))
    assert facets.place_ids == derive(
        _job(title="Engineer", locations=[Location(raw="München, Deutschland")])
    ).place_ids != []


def test_an_ambiguous_town_falls_back_to_its_country_rather_than_a_guess():
    """A dozen German towns are called Neustadt. Guessing one would put the posting in the
    wrong circle; its country still lets the filter keep it for anyone looking in Germany."""
    facets = derive(_job(title="Engineer", locations=[Location(raw="Neustadt, Germany")]))
    assert facets.place_ids == []
    assert facets.unplaced_countries == ["DE"]


@pytest.mark.parametrize(
    ("raw", "country", "place_id"),
    [("London", "GB", 2643743), ("Bielefeld", "DE", 2949186), ("Wien", "AT", 2761369),
     ("Vienna", "AT", 2761369)],
)
def test_a_bare_town_resolves_worldwide_when_one_place_dominates(raw, country, place_id):
    """A posting that names only a town used to derive to no country, and a posting with no
    country passes every location filter: a user who picked Vienna was shown London."""
    facets = derive(_job(title="Engineer", locations=[Location(raw=raw)]))
    assert facets.countries == [country]
    assert facets.place_ids == [place_id]


@pytest.mark.parametrize(
    ("raw", "could_be"), [("Cambridge", {"GB", "US"}), ("Geneva", {"CH", "US"})]
)
def test_a_bare_town_is_judged_by_every_country_it_could_be_in(raw, could_be):
    """Cambridge is two cities of similar size. It may not land in somebody's circle -- but left
    with no country at all it passed every filter, and "Geneva" was recommended in Vienna."""
    facets = derive(_job(title="Engineer", locations=[Location(raw=raw)]))
    assert facets.place_ids == []
    assert facets.countries == []
    assert could_be <= set(facets.unplaced_countries)


@pytest.mark.parametrize(
    ("raw", "place_id"),
    [("Berlin Office", 2950159), ("Munich HQ", 2867714), ("Zentrale (Wien)", 2761369),
     ("DE-Berlin", 2950159), ("Brunswick (Germany)", 2945024), ("Munich, Bavaria", 2867714),
     ("Cybay Hannover", 2910831)],
)
def test_a_town_among_words_that_name_no_place_still_resolves(raw, place_id):
    """Each of these derived to no place and no country, so each passed every location filter:
    47 of the 50 postings in one Vienna reader's edition were Berlin, Munich and Paris."""
    facets = derive(_job(title="Engineer", locations=[Location(raw=raw)]))
    assert place_id in facets.place_ids


@pytest.mark.parametrize("raw", ["Hamburg or Berlin", "Berlin; Hamburg", "Hamburg | Berlin"])
def test_one_string_naming_several_towns_places_each(raw):
    facets = derive(_job(title="Engineer", locations=[Location(raw=raw)]))
    assert {2911298, 2950159} <= set(facets.place_ids)


@pytest.mark.parametrize("raw", ["Hybrid or Fully Remote", "Erasmus Kindergarten Berliner Straße"])
def test_a_word_that_is_also_a_small_town_never_places_a_posting(raw):
    """Fully is a village in Valais and Erasmus one in South Africa. Read as places, these moved
    postings out of every circle on the strength of an ordinary word."""
    facets = derive(_job(title="Engineer", locations=[Location(raw=raw)]))
    assert facets.countries == []
    assert facets.unplaced_countries == []


@pytest.mark.parametrize(
    ("raw", "country"),
    [("USA-NY-Remote Location", "US"), ("US: South San Francisco", "US"), ("Aschach (AT)", "AT")],
)
def test_a_capital_country_code_is_the_country_even_where_a_town_shares_it(raw, country):
    """"Usa" is a town in Japan and "At" one elsewhere; read as towns, these postings named no
    country and passed every filter."""
    facets = derive(_job(title="Engineer", locations=[Location(raw=raw)]))
    assert country in facets.countries


def test_a_small_town_beside_a_misleading_word_keeps_its_country():
    """"Media" is a city in Algeria. If only big names counted, this Fulda shop would be judged as
    Algerian and never reach anyone in Germany."""
    facets = derive(_job(title="Engineer", locations=[
        Location(raw="Media Markt, Fulda (am Emaillierwerk)")
    ]))
    assert "DE" in facets.unplaced_countries


def test_a_town_beside_a_stated_country_we_cannot_read_is_not_looked_up_worldwide():
    facets = derive(_job(title="Engineer", locations=[
        Location(raw="Atlantis, ATLANTIS", city="Atlantis", country="ATLANTIS")
    ]))
    assert facets.place_ids == []
    assert facets.unplaced_countries == []


def test_a_town_with_a_state_is_never_looked_up_worldwide():
    """Worldwide, "Vienna" is Wien. "Vienna, VA" is Virginia, and must never reach a circle
    drawn around Wien."""
    facets = derive(_job(title="Engineer", locations=[Location(raw="Vienna, VA")]))
    assert facets.countries == ["US"]
    assert 2761369 not in facets.place_ids


def test_a_country_name_is_a_country_never_a_town():
    facets = derive(_job(title="Engineer", locations=[Location(raw="China")]))
    assert facets.countries == ["CN"]
    assert facets.cities == []
    assert facets.unplaced_countries == ["CN"]


def test_a_us_state_named_like_a_country_is_not_read_as_that_country():
    """"Georgia" is left out of the vocabulary on purpose; read as GE, every Atlanta posting
    would move to the Caucasus."""
    facets = derive(_job(title="Engineer", locations=[Location(raw="Atlanta, Georgia")]))
    assert "GE" not in facets.countries


def test_a_stated_country_we_cannot_read_falls_back_to_the_raw_text():
    """Workday states "United States of America" beside a raw "Las Vegas, NV, USA". The stated
    name was unknown, the raw one was never read, and every such posting had no country."""
    facets = derive(_job(title="Engineer", locations=[
        Location(raw="Las Vegas, NV, USA", country="Republic of Nowhere")
    ]))
    assert facets.countries == ["US"]


@pytest.mark.parametrize(
    ("raw", "country"),
    [
        ("Durham, North Carolina, United States of America", "US"),
        ("UK - London", "GB"),
        ("US - Austin, TX", "US"),
        ("Princeton - NJ - US", "US"),
        ("Berlin, Berlin", "DE"),
        ("Chicago, IL", "US"),
        ("Columbus, Ohio", "US"),
    ],
)
def test_the_ways_boards_write_a_place_resolve_its_country(raw, country):
    """Each of these derived to no country, and a posting with no country passes every location
    filter: 14% of open postings, shown to a reader who picked only Vienna."""
    facets = derive(_job(title="Engineer", locations=[Location(raw=raw)]))
    assert facets.countries == [country]


def test_a_state_code_is_not_a_country_unless_the_town_is_in_the_us():
    """"CA" is California after Redlands and Canada after Toronto."""
    facets = derive(_job(title="Engineer", locations=[Location(raw="Toronto, CA")]))
    assert facets.countries == ["CA"]


def test_a_bare_hyphen_is_part_of_a_town_name_not_a_separator():
    facets = derive(_job(title="Engineer", locations=[Location(raw="Castrop-Rauxel")]))
    assert facets.countries == ["DE"]
    assert facets.place_ids != []


def test_unknown_country_derives_to_nothing_rather_than_a_guess():
    facets = derive(_job(title="Engineer", locations=[Location(raw="Mordor")]))
    assert facets.countries == []


def test_benefits_prose_does_not_decide_work_mode(gh_board):
    """A benefits list is not a work-mode statement.

    "flexible paid time off" previously matched a "flexible" hybrid term and classified an
    on-site Austrian role as hybrid.
    """
    job = gh_normalize(gh_board["jobs"][1], external_id="beispiel:4002")
    assert "flexible paid time off" in job.description
    facets = derive(job)
    assert facets.work_mode is not WorkMode.HYBRID


def test_company_boilerplate_does_not_decide_work_mode():
    """An employer describing itself is not this vacancy describing itself.

    A description saying "we are an all-remote company" previously marked an explicitly
    Bangalore-based on-site role as remote. Unknown is the honest answer.
    """
    facets = derive(
        _job(
            title="Process Engineer",
            description="We are an all-remote company with a remote-first culture.",
            locations=[Location(raw="Bangalore, India")],
        )
    )
    assert facets.work_mode is WorkMode.UNKNOWN


def test_structured_remote_hint_outranks_everything():
    assert derive(_job(title="Engineer", remote_hint=True)).work_mode is WorkMode.REMOTE
    assert derive(_job(title="Engineer", remote_hint=False)).work_mode is WorkMode.ONSITE


def test_seniority_reads_the_title_not_the_description():
    # Descriptions mention seniority constantly in requirements, which classifies the wrong thing.
    assert derive(_job(title="Senior Engineer")).seniority is Seniority.SENIOR
    assert (
        derive(
            _job(title="Engineer", description="You will mentor our senior engineers.")
        ).seniority
        is Seniority.UNKNOWN
    )


def test_leadership_titles_outrank_plain_seniority():
    assert derive(_job(title="Head of Operations")).seniority is Seniority.LEAD
    assert derive(_job(title="Director of Engineering")).seniority is Seniority.EXECUTIVE


def test_austrian_monthly_salary_annualises_over_fourteen_months():
    """Austrian contracts commonly pay 14 monthly salaries; German ones do not.

    Annualising generously matters because this only decides whether a posting clears a floor:
    over-estimating lets a borderline job be judged on its merits, under-estimating hides it.
    """
    salary = SalaryQuote(amount_min=Decimal(4000), currency="EUR", period=SalaryPeriod.MONTH)
    def _at(raw: str):
        return derive(_job(title="Engineer", salary=salary, locations=[Location(raw=raw)]))

    austrian = _at("Wien, Austria")
    german = _at("Berlin, Germany")
    assert austrian.salary_min_eur_year == Decimal(56000)
    assert german.salary_min_eur_year == Decimal(48000)
    assert austrian.salary_annualised is True


def test_annual_salary_is_not_marked_annualised(aa_listing):
    facets = derive(aa_normalize(aa_listing))
    assert facets.salary_min_eur_year == Decimal(62000)
    assert facets.salary_annualised is False


def test_non_euro_salary_is_not_converted():
    # Converting would need an exchange rate, and a stale rate is a wrong number that looks right.
    facets = derive(
        _job(
            title="Engineer",
            salary=SalaryQuote(amount_min=Decimal(90000), currency="USD", period=SalaryPeriod.YEAR),
        )
    )
    assert facets.salary_min_eur_year is None


def test_missing_salary_is_none_not_zero():
    # Zero would read as "unpaid" to a salary filter rather than "unstated".
    assert derive(_job(title="Engineer")).salary_min_eur_year is None


def test_skills_come_from_the_dictionary_only(aa_listing, aa_detail):
    facets = derive(aa_normalize(aa_listing, aa_detail))
    assert "lean management" in facets.skills
    assert "six sigma" in facets.skills
    assert "sap" in facets.skills


def test_employment_hint_outranks_title_matching():
    assert (
        derive(_job(title="Engineer", employment_type_hint="teilzeit")).employment_type
        is EmploymentType.PART_TIME
    )
    assert (
        derive(_job(title="Praktikum Logistik")).employment_type is EmploymentType.INTERNSHIP
    )


# Spellings observed in live Arbeitsagentur payloads on 2026-09-09. The API transliterates
# umlauts, so a vocabulary keyed only on the umlauted form matches none of them.
LIVE_COUNTRY_SPELLINGS = {
    "DEUTSCHLAND": "DE",
    "OESTERREICH": "AT",
    "SCHWEIZ": "CH",
    "NIEDERLANDE": "NL",
    "POLEN": "PL",
    "SUEDAFRIKA": "ZA",
    "DAENEMARK": "DK",
    "RUMAENIEN": "RO",
}


@pytest.mark.parametrize(("land", "expected"), sorted(LIVE_COUNTRY_SPELLINGS.items()))
def test_transliterated_country_names_resolve(land: str, expected: str):
    """Arbeitsagentur writes OESTERREICH, not Österreich.

    The retrieval evaluation found this: every Austrian posting derived to no country at all and
    was therefore invisible to every country filter, in a product built for DACH. 4.3% of a live
    sample. It raised nothing, because "never guess" correctly declines to invent a country -- the
    vocabulary simply had the wrong key.
    """
    facets = derive(
        _job(title="Ingenieur", locations=[Location(raw=f"Wien, {land}", country=land)])
    )
    assert facets.countries == [expected]
