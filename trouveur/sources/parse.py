"""Pure decoding shared by normalisers. Nothing here interprets; that is ingest/derive.py."""

from __future__ import annotations

import html as html_module
from datetime import UTC, datetime
from typing import Any

from selectolax.parser import HTMLParser

from trouveur.models import collapse_whitespace

# Below this a value is seconds, above it milliseconds. No real posting date is ambiguous.
_MILLIS_THRESHOLD = 1_000_000_000_000


def iso_datetime(value: Any) -> datetime | None:
    """An ISO 8601 timestamp, normalised to UTC. Naive is read as UTC, never local: a
    normaliser that read the machine's timezone would not replay reproducibly."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def epoch_datetime(value: Any) -> datetime | None:
    """An epoch timestamp in either seconds or milliseconds. Arbeitnow states seconds and Lever
    milliseconds; reading one as the other lands in 1970 or the year 57000, without erroring."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    seconds = float(value)
    if abs(seconds) >= _MILLIS_THRESHOLD:
        seconds /= 1000.0
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def html_to_text(content: Any, *, unescape: bool = False) -> str | None:
    """Readable text from a source's markup.

    `unescape` is only for a source that HTML-escapes its own HTML (`&lt;div&gt;`). Unescape
    first, then strip. Either order applied to the wrong source corrupts the description.
    """
    if not content:
        return None
    markup = str(content)
    if unescape:
        markup = html_module.unescape(markup)
    return collapse_whitespace(HTMLParser(markup).text(separator=" "))
