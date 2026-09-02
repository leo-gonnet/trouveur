"""Runner scheduling decisions.

Only the pure logic is tested here -- claiming and executing need Postgres and a live
pipeline. What matters and is easy to get wrong is *when* a scheduled run is queued: exactly
once per day, only after its slot, and never while disabled.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from trouveur.runner.service import MAX_ATTEMPTS, is_scheduled_run_due


def _schedule(enabled=True, hour=7, minute=0):
    return SimpleNamespace(enabled=enabled, run_hour=hour, run_minute=minute)


def _at(h, m=0):
    return datetime(2026, 9, 1, h, m, tzinfo=UTC)


def test_due_when_slot_passed_and_nothing_queued_today():
    assert is_scheduled_run_due(_schedule(hour=7), _at(7, 30), last_queued_at=None) is True


def test_not_due_before_the_slot():
    assert is_scheduled_run_due(_schedule(hour=7), _at(6, 59), last_queued_at=None) is False


def test_not_due_when_a_run_was_already_queued_after_todays_slot():
    last = _at(7, 1)
    assert is_scheduled_run_due(_schedule(hour=7), _at(9, 0), last_queued_at=last) is False


def test_due_again_when_last_run_was_yesterday():
    yesterday = _at(7, 5) - timedelta(days=1)
    assert is_scheduled_run_due(_schedule(hour=7), _at(7, 5), last_queued_at=yesterday) is True


def test_never_due_when_disabled():
    assert is_scheduled_run_due(_schedule(enabled=False), _at(23, 0), last_queued_at=None) is False


def test_retry_budget_is_one():
    """One initial attempt plus exactly one retry, so the run stops requeueing at MAX_ATTEMPTS."""
    assert MAX_ATTEMPTS == 2
