"""Placing a kept job back into tonight's arms, as search would have ranked it."""

from __future__ import annotations

from datetime import UTC, datetime

from trouveur.match import recall


def _day(day: int) -> datetime:
    return datetime(2026, 10, day, tzinfo=UTC)


def test_a_job_lands_after_every_closer_one():
    assert recall.dense_position([0.1, 0.2, 0.5], 0.3) == 2
    assert recall.dense_position([0.1, 0.2, 0.5], 0.05) == 0


def test_text_rank_ties_go_newest_first_and_undated_last():
    pool = [(0.9, _day(1)), (0.5, _day(20)), (0.5, _day(5)), (0.5, None)]
    assert recall.lexical_position(pool, 0.5, _day(10)) == 2, "after the 0.9 and the newer 0.5"
    assert recall.lexical_position(pool, 0.5, None) == 4, "an undated job sorts after all"


def test_inserting_keeps_the_arm_at_its_depth_and_never_twice():
    assert recall.insert([1, 2, 3], 9, 1, depth=3) == [1, 9, 2]
    assert recall.insert([1, 2, 3], 9, 3, depth=3) == [1, 2, 3], "past the depth: not in the arm"
    assert recall.insert([1, 9, 3], 9, 0, depth=3) == [9, 1, 3], "already there: moved, not doubled"
    assert recall.insert([1, 2, 3], 9, None, depth=3) == [1, 2, 3]
