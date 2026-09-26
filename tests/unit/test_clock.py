"""The installation's wall clock.

Storage is UTC and stays UTC. This is the edge, and every failure it guards is silent: a time
rendered two hours early looks like a time, a scan hour that slid by one still runs, and an
edition cut on the wrong day is still a perfectly ordinary edition.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from trouveur import clock
from trouveur.web.app import _localtime, _posted_age


@pytest.fixture
def vienna(monkeypatch):
    monkeypatch.setenv("TIMEZONE", "Europe/Vienna")


def test_a_stored_instant_is_displayed_on_the_readers_clock(vienna):
    """Rendered straight from the database a 09:00 scan reads 07:00, and the page says nothing
    about why -- every column is timestamptz and asyncpg returns them in UTC."""
    assert _localtime(datetime(2026, 9, 25, 7, 0, tzinfo=UTC), "%H:%M") == "09:00"
    # Winter is a different offset, which is the whole reason this is not a fixed +2.
    assert _localtime(datetime(2026, 1, 15, 7, 0, tzinfo=UTC), "%H:%M") == "08:00"


def test_a_missing_time_renders_as_a_dash_rather_than_an_error(vienna):
    """Several columns are nullable -- a board that has never succeeded has no last success."""
    assert _localtime(None) == "—"


def test_an_advert_posted_late_last_night_is_yesterday_not_today(vienna, monkeypatch):
    """Calendar days, not elapsed hours.

    Measured as elapsed time, something posted at 23:00 last night read "posted today" all
    through this morning, because fewer than 24 hours had passed. The card is the only place a
    reader is told how stale a posting is.
    """
    monkeypatch.setattr(clock, "today", lambda: date(2026, 9, 25))

    late_yesterday = datetime(2026, 9, 24, 21, 0, tzinfo=UTC)  # 23:00 in Vienna
    assert _posted_age(late_yesterday) == "posted yesterday"

    this_morning = datetime(2026, 9, 25, 6, 0, tzinfo=UTC)
    assert _posted_age(this_morning) == "posted today"

    assert _posted_age(datetime(2026, 9, 22, 12, 0, tzinfo=UTC)) == "posted 3 days ago"


def test_a_posting_with_no_date_says_so(vienna):
    assert _posted_age(None) == "date not stated"


def test_an_unknown_timezone_is_refused_rather_than_quietly_read_as_utc(monkeypatch):
    """A silent fallback would cut every edition on the wrong day and label every time wrongly,
    and nothing downstream could tell that apart from a correct installation."""
    monkeypatch.setenv("TIMEZONE", "Europe/Viena")
    with pytest.raises(clock.TimezoneError, match="IANA timezone name"):
        clock.zone()


def test_utc_remains_a_valid_choice(monkeypatch):
    """The setting narrows behaviour; it must not make the previous behaviour unreachable."""
    monkeypatch.setenv("TIMEZONE", "UTC")
    assert _localtime(datetime(2026, 9, 25, 7, 0, tzinfo=UTC), "%H:%M") == "07:00"
