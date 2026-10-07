"""Links inside a posting we already hold. Pure: no network, no clock, no database.

The first lead source, and the only free one: every posting's raw payload has been kept for ever,
so this is recall we have already paid for. Aggregators are what make it worth doing -- an
Arbeitsagentur advert states the employer's own application URL, and that URL is a board with the
company's other forty postings on it.

Mined from the RAW payload rather than from `job.description`, which is the one thing here that is
not obvious: `html_to_text` keeps the words and drops the `href`, so the link in "Apply here" is
gone from the stored description and present in the archive. Mining the stored text instead would
silently find only the handful of links a source happens to write as bare text.
"""

from __future__ import annotations

import html
import re

from trouveur.discovery.resolve import resolve
from trouveur.models import Lead, LeadOrigin, LeadResult

# Absolute http(s) URLs in arbitrary text -- an href, a src, or a bare URL in a JSON string.
# Stops at the first quote, bracket, backslash or whitespace, freehire's AbsURLRe.
_ABSOLUTE_URL = re.compile(r"https?://[^\s\"'<>)\\]+")

# A URL at the end of a sentence in plain text takes the punctuation with it. Trimmed here
# because the payload is JSON, so the markup-stopping characters above never see it.
_TRAILING = ".,;:!?"

# A link worth keeping. Without this every posting contributes its company's LinkedIn page, its
# cookie policy and its font host, and the table stops being readable long before it stops being
# correct. It is a filter on a discovery INPUT, not a facet -- the cost of it being too narrow is
# a lead we did not notice, and the raw archive is still there, so widening it is a MINE_VERSION
# bump and a re-mine with no new request. A link a URL rule already recognises is kept whatever
# its wording.
#
# It is LOAD-BEARING for finding platforms we have no rule for, which is the one thing here worth
# being careful about. `resolve` holds a rule only for a source we can sweep, so every link to an
# ATS we do not yet support arrives as an unread host and reaches the discovery panel only if one
# of these words is in it. In practice an ATS URL always carries one -- recruitee, bamboohr,
# smartrecruiters and softgarden links all say "job", "career" or "recruit" somewhere -- but a
# platform that names itself with none of them would be invisible rather than merely unresolved.
_JOB_WORDS = (
    "job", "jobs", "career", "careers", "vacanc", "apply", "application", "recruit", "hiring",
    "hire", "ats", "stelle", "stellen", "bewerb", "karriere", "emploi", "empleo", "lavoro",
    "vacature", "praca",
)

# Enough to carry any real board URL; past it a payload is quoting something else (a tracking
# pixel's redirect chain, a base64 blob that happens to contain "http").
_MAX_URL_CHARS = 500


def urls_in(text: str | None) -> list[str]:
    """Every absolute URL in `text`, in order, each one once.

    Entities are unescaped first: an href inside a JSON payload arrives as `?a=1&amp;b=2`, and
    Greenhouse's embed script carries the board in exactly such a parameter.
    """
    if not text:
        return []
    found: dict[str, None] = {}
    for match in _ABSOLUTE_URL.finditer(html.unescape(text)):
        url = match.group(0).rstrip(_TRAILING)
        if url and len(url) <= _MAX_URL_CHARS:
            found.setdefault(url, None)
    return list(found)


def leads_from_posting(
    *,
    job_id: int,
    source: str,
    scope: str | None,
    url: str | None,
    company: str | None,
    title: str | None,
    location_text: str | None,
    payload: str | None,
) -> list[Lead]:
    """The leads one posting yields: its own link, plus every link in its raw payload.

    A link resolving to where the posting ITSELF came from is dropped -- its own board, or, for a
    source that sweeps a whole platform, that platform. Without it every posting on a board we
    already sweep files a lead saying so -- one row per posting, for ever, all of them naming
    boards the crawl set already holds -- and the real leads sit among them. What is left is only
    the links that point somewhere else, which is the whole of what discovery is for.
    """
    leads = []
    for candidate in urls_in(url) + urls_in(payload):
        resolution = resolve(candidate)
        if (resolution.source, resolution.scope) == (source, scope):
            continue
        if resolution.result is LeadResult.UNKNOWN_HOST and not _looks_like_work(candidate):
            continue
        leads.append(
            Lead(
                origin=LeadOrigin.ARCHIVE,
                url=candidate,
                job_id=job_id,
                company=company,
                title=title,
                location_text=location_text,
            )
        )
    return leads


def _looks_like_work(url: str) -> bool:
    folded = url.lower()
    return any(word in folded for word in _JOB_WORDS)
