"""The Quality page: score dots, and recall per night with the needle changes behind it."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from trouveur.web import quality

# 29 Oct 2026, 09:00 UTC: 10:00 in Vienna.
NOW = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def vienna(monkeypatch):
    monkeypatch.setenv("TIMEZONE", "Europe/Vienna")


def _night(day: int) -> datetime:
    return datetime(2026, 10, day, 3, 30, tzinfo=UTC)


def _check(check_id: int, day: int, ranks: dict[int, int | None]):
    if not ranks:
        return [SimpleNamespace(check_id=check_id, checked_at=_night(day), job_id=None, rank=None)]
    return [
        SimpleNamespace(check_id=check_id, checked_at=_night(day), job_id=job, rank=rank)
        for job, rank in ranks.items()
    ]


def _job(state="saved"):
    return SimpleNamespace(state=state)


def test_recall_is_found_over_needles_and_a_miss_is_listed():
    nights = quality.recall_nights(_check(1, 29, {10: 4, 11: None, 12: 90}), {}, NOW)
    tonight = nights[-1]
    assert (tonight.needles, tonight.found, tonight.missing) == (3, 2, [11])
    assert tonight.recall == pytest.approx(2 / 3)


def test_only_the_reader_takes_a_job_out_of_the_kept_set():
    """Age and closing no longer remove a needle, so every change is the reader's own action."""
    rows = _check(1, 28, {10: 1, 11: 2, 12: 3}) + _check(2, 29, {10: 1, 14: 5})
    jobs = {11: _job(state="dismissed"), 12: _job(state="new")}
    tonight = quality.recall_nights(rows, jobs, NOW)[-1]
    assert tonight.added == [14]
    assert tonight.removed == [(11, "dismissed"), (12, "unmarked")]


def test_a_night_without_a_check_is_a_gap_and_changes_count_from_the_last_check():
    rows = _check(1, 26, {10: 1}) + _check(2, 29, {10: 1, 11: 2})
    nights = quality.recall_nights(rows, {}, NOW)
    assert [night.needles for night in nights[-4:]] == [1, None, None, 2]
    assert nights[-1].added == [11]


def test_a_check_with_nothing_to_look_for_is_zero_not_a_gap():
    tonight = quality.recall_nights(_check(1, 29, {}), {}, NOW)[-1]
    assert tonight.needles == 0 and tonight.recall is None


def _score(job_id, score, day, state="new"):
    return SimpleNamespace(job_id=job_id, llm_score=score, scored_at=_night(day), state=state)


def test_a_night_counts_its_good_jobs_and_their_share():
    rows = [_score(1, 90, 29), _score(2, 86, 29), _score(3, 40, 29), _score(4, 70, 29)]
    tonight = quality.score_nights(rows, NOW)[-1]
    assert (tonight.scored, tonight.good, tonight.share_good) == (4, 2, 0.5)


def test_a_night_with_nothing_scored_is_a_gap_not_zero_good_jobs():
    nights = quality.score_nights([_score(1, 90, 29)], NOW)
    assert nights[-2].good is None and nights[-2].share_good is None


def test_dismissed_good_reads_the_last_week_of_good_jobs_the_reader_acted_on():
    """One night holds a handful of good jobs and dismissals come days later, so a single night
    would swing between 0 and 100%. Untouched jobs say nothing either way."""
    rows = [
        _score(1, 90, 20, "dismissed"),  # nine nights ago: outside the week
        _score(2, 90, 25, "dismissed"),
        _score(3, 90, 27, "saved"),
        _score(4, 95, 29, "applied"),
        _score(5, 99, 29, "new"),
        _score(6, 50, 29, "dismissed"),  # not good: says nothing about the score's "good"
    ]
    tonight = quality.score_nights(rows, NOW)[-1]
    assert tonight.dismissed_good == pytest.approx(1 / 3)


def test_all_readers_is_the_median_reader_night_by_night():
    alice = quality.score_nights([_score(1, 90, 29), _score(2, 90, 29)], NOW)
    bob = quality.score_nights([_score(3, 90, 29)], NOW)
    carol = quality.score_nights([_score(4, 10, 29)], NOW)
    tonight = quality.median_nights([alice, bob, carol])[-1]
    assert tonight.good == 1 and tonight.share_good == 1


def test_one_dismissal_out_of_one_is_read_as_unsure_not_as_100_percent():
    """The reason the share is drawn with its range: a reader who dismisses the only good job of
    the week has not shown the score is wrong, and anything alerting on this reads the low end."""
    low, high = quality.wilson(1, 1)
    assert high == 1 and low < 0.3
    low, high = quality.wilson(20, 40)
    assert 0.35 < low < 0.5 < high < 0.65


def test_all_readers_pool_their_dismissals_instead_of_a_median():
    alice = quality.score_nights([_score(1, 90, 29, "dismissed")], NOW)
    bob = quality.score_nights([_score(2, 90, 29, "saved"), _score(3, 90, 29, "saved")], NOW)
    tonight = quality.median_nights([alice, bob])[-1]
    assert (tonight.good_dismissed, tonight.good_acted) == (1, 3)


def test_recall_pools_the_readers_needles():
    """A share of jobs: one reader with a single needle must not weigh like one with twenty."""
    alice = quality.recall_nights(_check(1, 29, {i: 1 for i in range(1, 20)}), {}, NOW)
    bob = quality.recall_nights(_check(2, 29, {50: None}), {}, NOW)
    tonight = quality.pooled([alice, bob])[-1]
    assert (tonight.found, tonight.needles) == (19, 20)
    assert quality.last_week(quality.pooled([alice, bob])) == (19, 20)
