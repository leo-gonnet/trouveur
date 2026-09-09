"""Guards for the Arbeitsagentur constraints. Every one of these fails silently in production.

Each test here corresponds to a behaviour verified against the live API on 2026-09-08 that returns
a wrong answer with an HTTP 200 rather than an error. They exist to make a future "cleanup" fail
loudly instead of quietly halving the corpus.
"""

from __future__ import annotations

import pytest

from trouveur.sources.arbeitsagentur import client, normalize
from trouveur.sources.errors import FetchError


def test_delta_window_rejects_values_that_return_the_whole_corpus():
    # veroeffentlichtseit is not a day count. 2, 3, 4, 5, 30 and 100 are all accepted by the API
    # and all return the entire ~1M corpus instead of a window, with no error. A "2-day catch-up"
    # would silently fetch a million rows.
    assert client.DELTA_WINDOWS == {0, 1, 7, 14}
    for silently_unfiltered in (2, 3, 4, 5, 30, 100):
        assert silently_unfiltered not in client.DELTA_WINDOWS


def test_sweep_windows_are_whitelisted_values():
    assert client._DAILY_WINDOW in client.DELTA_WINDOWS
    assert client._BACKFILL_WINDOW in client.DELTA_WINDOWS


def test_page_size_stays_within_the_limit_that_returns_rows():
    # size=501 returns HTTP 200 with an empty list, so raising this "for throughput" collects
    # nothing at all.
    assert client.MAX_PAGE_SIZE <= 500


def test_paging_never_exceeds_the_result_window():
    # size * page > 10000 is HTTP 400. The loop must stop before it, not discover it.
    max_page = client.RESULT_WINDOW // client.MAX_PAGE_SIZE
    assert max_page * client.MAX_PAGE_SIZE <= client.RESULT_WINDOW


def test_search_uses_v6_and_detail_uses_v4():
    # Not a typo and not harmonisable: each endpoint 403s on the version that looks consistent.
    assert "/v6/jobs" in client._SEARCH_URL
    assert "/v4/jobdetails" in client._DETAIL_URL
    assert "/v4/jobs" not in client._SEARCH_URL
    for wrong in ("/v5/jobdetails", "/v6/jobdetails"):
        assert wrong not in client._DETAIL_URL


def test_normalize_reads_the_search_result_list_key(aa_listing):
    # The list key is `ergebnisliste`, not `stellenangebote`; older documentation disagrees.
    payload = {"ergebnisliste": [aa_listing], "maxErgebnisse": 1}
    assert payload["ergebnisliste"][0]["referenznummer"] == aa_listing["referenznummer"]


def test_normalize_keeps_structured_address_when_source_gives_one(aa_listing):
    job = normalize(aa_listing)
    assert len(job.locations) == 1
    location = job.locations[0]
    assert location.city == "München, Oberbayern"
    assert location.country == "DEUTSCHLAND"
    # Never transliterated: the API returns and expects literal umlauts.
    assert "Muenchen" not in location.raw


def test_agency_flag_is_unknown_without_a_detail_payload(aa_listing, aa_detail):
    # Only the detail endpoint carries the agency flags. Absent one the answer is genuinely
    # unknown, and returning False would read downstream as "confirmed not an agency".
    assert normalize(aa_listing).agency_hint is None
    assert normalize(aa_listing, aa_detail).agency_hint is False


def test_agency_flag_is_true_for_arbeitnehmeruberlassung(aa_agency_detail):
    job = normalize(aa_agency_detail, aa_agency_detail)
    assert job.agency_hint is True


def test_description_comes_only_from_the_detail_payload(aa_listing, aa_detail):
    assert normalize(aa_listing).description is None
    assert "Prozessoptimierung" in normalize(aa_listing, aa_detail).description


def test_content_hash_changes_when_the_description_arrives(aa_listing, aa_detail):
    # This is what re-queues derivation, embedding and scoring once a detail lands.
    assert normalize(aa_listing).content_hash != normalize(aa_listing, aa_detail).content_hash


def test_normalize_returns_none_when_identity_is_missing(aa_listing):
    assert normalize({**aa_listing, "referenznummer": None}) is None
    assert normalize({**aa_listing, "stellenangebotsTitel": None, "hauptberuf": None}) is None


def test_url_prefers_the_employer_advert_then_falls_back(aa_listing):
    assert normalize(aa_listing).url == "https://beispiel.example/karriere/wing-42"
    without = {**aa_listing}
    del without["externeURL"]
    assert "arbeitsagentur.de" in normalize(without).url


async def test_sweep_refuses_a_window_that_would_return_everything(monkeypatch):
    source = client.ArbeitsagenturSource()
    monkeypatch.setattr(client, "_BACKFILL_WINDOW", 2)
    with pytest.raises(FetchError, match="veroeffentlichtseit"):
        await source.sweep(None, None, backfill=True)
