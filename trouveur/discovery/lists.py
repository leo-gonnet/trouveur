"""Board lists somebody else collected, read from local files and filed as leads.

No network: a list is a file a person downloaded, and the resolver decides what each link is. The
shape is guessed per file -- CSV with any delimiter, JSON, JSON Lines, or one link per line -- and
the link column is the one holding the most links, so a new list needs no code of its own.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from trouveur.discovery.work import record_leads
from trouveur.models import Lead, LeadOrigin

SUFFIXES = frozenset({".csv", ".tsv", ".txt", ".json", ".jsonl", ".ndjson"})

_SAMPLE = 200
_SCHEME = re.compile(r"^https?://", re.IGNORECASE)
_BARE_HOST = re.compile(r"^(?:[a-z0-9-]+\.)+[a-z]{2,}(?:[/?#]\S*)?$", re.IGNORECASE)

# Tie-breakers between two columns holding as many links: a careers link beats a homepage.
_URL_HINTS = ("career", "job", "board", "apply", "url", "link")
_COMPANY = ("company", "company_name", "employer", "organization", "organisation", "name")
_TITLE = ("title", "job_title", "position")
_LOCATION = ("location", "location_text", "city")


@dataclass(frozen=True)
class FileImport:
    path: Path
    rows: int
    without_link: int
    new_leads: int


def as_link(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if _SCHEME.match(text):
        return text
    # Many lists spell a site as a bare host, and the resolver reads http(s) links only.
    if _BARE_HOST.match(text):
        return f"https://{text}"
    return None


def records(text: str, suffix: str) -> list[dict]:
    if suffix == ".json":
        return _json_records(json.loads(text))
    if suffix in {".jsonl", ".ndjson"}:
        return _json_records([json.loads(line) for line in text.splitlines() if line.strip()])
    if suffix == ".txt":
        return [{"value": line} for line in text.splitlines() if line.strip()]
    return _table_records(text)


def _json_records(data: object) -> list[dict]:
    # An API page wraps its rows (`{"data": [...], "meta": ...}`): the first list is the rows.
    if isinstance(data, dict):
        data = next((value for value in data.values() if isinstance(value, list)), [data])
    if not isinstance(data, list):
        return []
    return [
        item if isinstance(item, dict) else {"value": item}
        for item in data
        if isinstance(item, (dict, str))
    ]


def _table_records(text: str) -> list[dict]:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    try:
        dialect = csv.Sniffer().sniff("\n".join(lines[:20]), delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO("\n".join(lines)), dialect))
    # A header names columns, so a first row holding a link is data.
    if any(as_link(cell) for cell in rows[0]):
        header = [f"column_{i}" for i in range(max(len(row) for row in rows))]
    else:
        header, rows = [cell.strip() for cell in rows[0]], rows[1:]
    return [dict(zip(header, row, strict=False)) for row in rows]


def _key(name: str) -> str:
    return re.sub(r"[\s-]+", "_", str(name).strip().lower())


def link_field(rows: list[dict]) -> str | None:
    counts: dict[str, int] = {}
    for row in rows[:_SAMPLE]:
        for name, value in row.items():
            if as_link(value):
                counts[name] = counts.get(name, 0) + 1
    if not counts:
        return None

    def rank(name: str) -> tuple[int, int]:
        hint = next(
            (i for i, word in enumerate(_URL_HINTS) if word in _key(name)), len(_URL_HINTS)
        )
        return -counts[name], hint

    return min(counts, key=rank)


def compact(row: dict, limit: int = 300) -> dict:
    """The small fields of a row: what a later resolver may want, without a description."""

    def small(value: object) -> bool:
        if isinstance(value, str):
            return len(value) <= limit
        if isinstance(value, list):
            return len(value) <= 20 and all(small(item) for item in value)
        return value is None or isinstance(value, (int, float, bool))

    return {
        str(name): value
        for name, value in row.items()
        if value not in ("", None) and small(value)
    }


def _field(row: dict, names: tuple[str, ...]) -> str | None:
    keyed = {_key(name): value for name, value in row.items()}
    for name in names:
        value = keyed.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def leads_from(rows: list[dict], list_name: str, url_field: str | None = None) -> list[Lead]:
    """One lead per row whose link column holds a link."""
    field = url_field or link_field(rows)
    if field is None:
        return []
    return [
        Lead(
            origin=LeadOrigin.LIST,
            list_name=list_name,
            url=url,
            company=_field(row, _COMPANY),
            title=_field(row, _TITLE),
            location_text=_field(row, _LOCATION),
            payload=compact(row),
        )
        for row in rows
        if (url := as_link(row.get(field)))
    ]


def files_in(paths: Iterable[Path]) -> list[Path]:
    found: list[Path] = []
    for path in paths:
        if path.is_dir():
            found += sorted(
                p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in SUFFIXES
            )
        else:
            found.append(path)
    return found


async def import_files(
    paths: Iterable[Path], list_name: str, url_field: str | None = None
) -> list[FileImport]:
    """File every row of every list as a lead; a link this list already filed is skipped.

    One write per file, so the boards of the first file are proposed, and so tried, before the
    next file's.
    """
    results = []
    for path in files_in(paths):
        rows = records(path.read_text(encoding="utf-8-sig"), path.suffix.lower())
        leads = leads_from(rows, list_name, url_field)
        results.append(
            FileImport(
                path=path,
                rows=len(rows),
                without_link=len(rows) - len(leads),
                new_leads=await record_leads(leads),
            )
        )
    return results
