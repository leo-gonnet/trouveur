"""Pure parsing helpers shared by normalisers.

Every source states a timestamp or a description in its own dialect, but there are only a handful
of dialects: ISO 8601, epoch seconds, epoch milliseconds, and HTML that may or may not have been
escaped on the way in. Each normaliser having its own copy is how two of them come to disagree
about what a naive datetime means -- silently, and only for the rows that carry one.

Nothing here interprets: turning "Vienna, Austria" into a country is derivation and lives in
ingest/derive.py. These functions only decode what a source already stated.
"""

from __future__ import annotations

import html as html_module
from datetime import UTC, datetime
from typing import Any

from selectolax.parser import HTMLParser

from trouveur.models import collapse_whitespace

# Below this, a value is a timestamp in seconds; above it, milliseconds. The boundary sits at
# 2001-09-09 in seconds and 1970-01-12 in milliseconds, so no real posting date is ambiguous.
_MILLIS_THRESHOLD = 1_000_000_000_000


def iso_datetime(value: Any) -> datetime | None:
    """An ISO 8601 timestamp, normalised to UTC.

    A naive timestamp is read as UTC rather than local time. The alternative makes normalisation
    depend on the machine's timezone, which would break the guarantee that replaying the archive
    reproduces the original result.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def epoch_datetime(value: Any) -> datetime | None:
    """An epoch timestamp in either seconds or milliseconds.

    Both are in use across sources -- Arbeitnow states seconds, Lever states milliseconds -- and
    reading one as the other puts the posting in 1970 or in the year 57000. Neither errors.
    """
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

    `unescape` is for sources that HTML-escape their own HTML, so the JSON holds `&lt;div&gt;`.
    Stripping tags without unescaping those first leaves the entity text in the description and
    strips nothing -- which reads as a parser that works, while feeding markup to the embedder and
    the reranker. Applying it to a source that did NOT double-escape is equally wrong: it would
    turn a literal `&lt;` the posting meant to show into a tag and delete the text after it.
    """
    if not content:
        return None
    markup = str(content)
    if unescape:
        markup = html_module.unescape(markup)
    return collapse_whitespace(HTMLParser(markup).text(separator=" "))
