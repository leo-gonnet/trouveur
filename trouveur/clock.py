"""The installation's wall clock: the one place `settings.timezone` is read.

Storage is UTC and stays UTC -- every column is `timestamptz`, the server runs on `Etc/UTC`, and
asyncpg hands back aware UTC datetimes. This module is only the edge conversion, in the two
directions that matter:

- **Display.** A time rendered without conversion reads two hours early for anyone in Vienna,
  and nothing on the page says it is UTC.
- **A day, and an hour, that a person chose.** `run_hour` is a wall-clock hour somebody typed,
  so it has to stay at that hour across a DST change instead of sliding by one. An edition's day
  is the day its reader would call it.

The day an edition is published into is STORED (`user_edition_item.day`), never recomputed when
the page is rendered: it is part of that table's key and of the constraint that stops a posting
being recommended twice, so a day that changed with the viewer's clock would make an immutable
record viewer-dependent. This module decides that day once, at publish time.

Deliberately NOT used by the archive export, whose partitions are keyed on `fetched_at::date` in
the database's own UTC. Those are storage partitions, not something a reader calls a day.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from trouveur.config import get_settings


class TimezoneError(Exception):
    """The configured timezone is not a name the system's zone database knows."""


def zone() -> ZoneInfo:
    """The configured zone. Not cached: `ZoneInfo` keeps its own cache, and a cache here would
    hold a stale zone for the rest of the process after the setting changed."""
    name = get_settings().timezone.strip()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise TimezoneError(
            f"TIMEZONE is set to {name!r}, which is not an IANA timezone name "
            f"(for example 'Europe/Vienna' or 'UTC')."
        ) from exc


def to_local(value: datetime) -> datetime:
    """A stored instant, as a reader's wall clock shows it."""
    return value.astimezone(zone())


def now() -> datetime:
    return datetime.now(zone())


def today() -> date:
    return now().date()
