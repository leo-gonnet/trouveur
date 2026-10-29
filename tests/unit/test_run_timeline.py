"""Step durations and bar placement for the Runs page chart."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from trouveur.web import timeline

# 29 Oct 2026: Vienna is UTC+1, so 03:30 UTC is 04:30 on the wall.
NOW = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def vienna(monkeypatch):
    monkeypatch.setenv("TIMEZONE", "Europe/Vienna")


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 29, hour, minute, tzinfo=UTC)


def _run(run_id=1, started=None, finished=..., stages=None, match_user_id=None):
    return SimpleNamespace(
        id=run_id,
        started_at=started or _at(3, 30),
        finished_at=_at(5, 21) if finished is ... else finished,
        match_user_id=match_user_id,
        stage_started={k: v.isoformat() for k, v in (stages or {}).items()},
    )


NIGHTLY = {"fetch": _at(3, 31), "prepare": _at(4, 0), "match": _at(4, 20), "email": _at(5, 20)}
WITH_DISCOVERY = {**NIGHTLY, "discover": _at(5, 21)}


def test_each_step_lasts_until_the_next_one_begins():
    took = timeline.step_seconds(_run(stages=NIGHTLY), NOW)
    # Fetch is counted from the run's start, not from the stamp a minute later.
    assert took == {"fetch": 30 * 60, "prepare": 20 * 60, "match": 60 * 60, "email": 60}


def test_a_running_step_lasts_until_now():
    run = _run(finished=None, stages={"fetch": _at(3, 30), "prepare": _at(8, 0)})
    assert timeline.step_seconds(run, NOW)["prepare"] == 60 * 60


def test_a_run_from_before_steps_were_recorded_is_one_unsplit_bar():
    run = _run(stages=None)
    assert timeline.step_seconds(run, NOW) == {}
    chart = timeline.build([run], NOW)
    assert [seg.stage for seg in chart.bars[0].segments] == [None]
    assert chart.has_unsplit


def test_the_axis_hugs_the_bars_and_marks_whole_hours():
    chart = timeline.build([_run(stages=NIGHTLY)], NOW)
    # 04:30 to 06:21 local: the axis runs 04:30 to 06:30, with marks at 05:00 and 06:00.
    assert [label for label, _ in chart.ticks] == ["05:00", "06:00"]
    first = chart.bars[0].segments[0]
    assert first.stage == "fetch" and first.left == 0
    assert (first.start.hour, first.start.minute) == (4, 30)
    assert (first.end.hour, first.end.minute) == (5, 0)
    assert not chart.has_unsplit


def test_a_bar_is_named_today_or_yesterday_when_it_is():
    def on(day):
        return _run(started=_at(3, 30).replace(day=day), finished=_at(5, 0).replace(day=day))

    chart = timeline.build([on(29), on(28), on(27)], NOW)
    assert [bar.day for bar in chart.bars] == ["today", "yesterday", "Tue 27"]


def test_only_the_step_still_going_reads_as_running():
    run = _run(finished=None, stages={"fetch": _at(3, 30), "prepare": _at(8, 0)})
    segments = timeline.build([run], NOW).bars[0].segments
    assert [seg.running for seg in segments] == [False, True]
    assert segments[-1].seconds == 60 * 60


def test_discovery_is_a_step_of_its_own():
    run = _run(finished=_at(5, 50), stages=WITH_DISCOVERY)
    assert timeline.step_seconds(run, NOW)["discover"] == 29 * 60


def test_steps_stamped_at_the_same_instant_keep_their_order():
    same = _at(5, 20)
    run = _run(stages={**NIGHTLY, "email": same, "discover": same})
    assert [stage for stage, *_ in timeline.step_spans(run, NOW)][-2:] == ["email", "discover"]


def test_a_scoring_only_run_does_not_stretch_the_axis():
    afternoon = _run(
        run_id=2, started=_at(14, 0), finished=_at(14, 5), match_user_id=3,
        stages={"match": _at(14, 0)},
    )
    chart = timeline.build([_run(stages=NIGHTLY), afternoon], NOW)
    assert [bar.run_id for bar in chart.bars] == [1]
    assert chart.ticks[-1][0] == "06:00"
