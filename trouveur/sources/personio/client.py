"""Personio XML job feed — network only, no field interpretation.

robots.txt (jobs.personio.de, checked 2026-09-09): empty, so no restriction is expressed. The XML
feed is Personio's own documented syndication endpoint, published so job boards can consume a
customer's postings. Boards live on per-tenant subdomains, so the politeness budget is owed to
personio.de as a whole -- see http.throttle_key.

Shape verified live on 2026-09-09 against a real board:

  - one request returns the tenant's COMPLETE live board, so the response is itself the seen-set;
  - **an unknown tenant answers HTTP 429 with a Vercel "Security Checkpoint" HTML page, not 404.**
    A valid board returns 200 even when polled fast, so 429 here is not rate limiting -- it is how
    a bad slug looks. PoliteClient retries 429 by design, so without the check below a typo'd slug
    burns four attempts and a backoff on every sweep and reports itself as throttling forever;
  - **the feed states no posting URL.** There is no href, link or url element anywhere in it, so
    the canonical URL has to be built from the tenant and the id.

Decoding XML into a dict happens here rather than in the normaliser, for the same reason the
Greenhouse client calls response.json(): that is transport decoding, not interpretation. The
archive then holds one decoded record per posting exactly as it does for every JSON source, and
reading its *fields* stays in the versioned pure normaliser where a fix is a re-derive.
"""

from __future__ import annotations

from typing import Any
from xml.etree import ElementTree

from trouveur.models import DocumentKind, RawDocument
from trouveur.sources.base import DocumentSink, ScopeResult, SweepOutcome
from trouveur.sources.board import BATCH, scoped_id
from trouveur.sources.errors import FetchError
from trouveur.sources.http import PoliteClient

SOURCE = "personio"

_BOARD_URL = "https://{scope}.jobs.personio.de/xml"

# The feed carries no URL, so this is the only way to address a posting. Verified against a live
# board on 2026-09-09.
_JOB_URL = "https://{scope}.jobs.personio.de/job/{job_id}"


def job_url(scope: str, job_id: str) -> str:
    return _JOB_URL.format(scope=scope, job_id=job_id)


class PersonioSource:
    name = SOURCE
    requires_detail = False
    tenant_scoped = True

    def __init__(self, boards: list[str] | None = None) -> None:
        self.boards = boards or []

    async def sweep(
        self, client: PoliteClient, sink: DocumentSink, *, backfill: bool = False
    ) -> SweepOutcome:
        # A board dump is always the complete live set, so a backfill and a daily run are the same
        # request. The flag is accepted for protocol conformance and deliberately unused.
        outcome = SweepOutcome(partitions_total=len(self.boards), expected=0)
        for scope in self.boards:
            try:
                seen = await self._sweep_board(client, sink, scope)
            except FetchError as exc:
                outcome.errors.append(f"scope={scope}: {exc}")
                outcome.scope_results.append(ScopeResult(scope=scope, ok=False, error=str(exc)))
                continue
            outcome.documents += seen
            outcome.partitions_done += 1
            outcome.expected = (outcome.expected or 0) + seen
            outcome.scope_results.append(ScopeResult(scope=scope, ok=True, documents=seen))
            outcome.closable_scopes.append(scope)
        return outcome

    async def _sweep_board(
        self, client: PoliteClient, sink: DocumentSink, scope: str
    ) -> int:
        response = await client.get(_BOARD_URL.format(scope=scope))
        content_type = response.headers.get("content-type", "")
        # An unknown tenant answers 429 with an HTML challenge page. Treating it as rate limiting
        # would retry a slug that will never exist; the body, not the status, is what tells them
        # apart.
        if "xml" not in content_type.lower():
            raise FetchError(
                f"Personio board {scope!r} returned HTTP {response.status_code} with "
                f"content-type {content_type!r} instead of XML; the tenant most likely does not "
                "exist. A 429 here is a bad slug, not throttling."
            )
        if response.status_code != 200:
            raise FetchError(
                f"Personio board {scope!r} returned HTTP {response.status_code}."
            )

        positions = _positions(response.text, scope)
        seen = 0
        for start in range(0, len(positions), BATCH):
            documents = [
                RawDocument(
                    source=SOURCE,
                    external_id=scoped_id(scope, position["id"]),
                    kind=DocumentKind.LISTING,
                    scope=scope,
                    payload=position,
                )
                for position in positions[start : start + BATCH]
            ]
            if documents:
                await sink(documents)
                seen += len(documents)
        return seen


def _positions(text: str, scope: str) -> list[dict]:
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise FetchError(
            f"Personio board {scope!r} returned XML that does not parse: {exc}."
        ) from exc
    records = []
    for node in root.iter("position"):
        record = element_to_dict(node)
        # A position with no id cannot be addressed or de-duplicated; it is dropped rather than
        # given a synthetic key that would change on every sweep.
        if isinstance(record, dict) and str(record.get("id") or "").strip():
            records.append(record)
    return records


def element_to_dict(node: ElementTree.Element) -> Any:
    """One XML element as JSON-compatible data, faithfully.

    Repeated sibling tags become a list so that a board with one job description and a board with
    several decode to the same shape -- otherwise the normaliser would need to handle both, and
    would get one of them wrong.
    """
    children = list(node)
    if not children:
        return (node.text or "").strip()

    record: dict[str, Any] = {}
    for child in children:
        value = element_to_dict(child)
        if child.tag in record:
            existing = record[child.tag]
            record[child.tag] = existing if isinstance(existing, list) else [existing]
            record[child.tag].append(value)
        else:
            record[child.tag] = value
    return record
