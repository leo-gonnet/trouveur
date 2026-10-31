"""Build trouveur/sources/eures/nuts.tsv, the region names the EURES normaliser writes.

EURES places a posting by NUTS code alone ("FR101"), and derivation reads names, not codes. The
names come from the portal's own reference endpoint, so they are the labels its search filters
on (the NUTS classification is Eurostat's). One request:

    uv run python tools/build_nuts.py

The output is committed. Changing it changes what the EURES normaliser produces, so a rebuild
must come with a bump of its `version`.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

URL = "https://europa.eu/eures/api/shared-data-rest-api/public/reference/hierarchy"
OUTPUT = Path(__file__).resolve().parents[1] / "trouveur" / "sources" / "eures" / "nuts.tsv"


def _walk(node: dict, rows: dict[str, str]) -> None:
    code, label = str(node.get("nutsCode") or ""), str(node.get("label") or "").strip()
    if code and label and "\t" not in label:
        rows[code.upper()] = label
    for child in node.get("regions") or []:
        _walk(child, rows)


def main() -> None:
    with urllib.request.urlopen(URL, timeout=60) as response:  # noqa: S310 - fixed https URL
        tree = json.load(response)
    rows: dict[str, str] = {}
    for country in tree:
        _walk(country, rows)
    OUTPUT.write_text(
        "".join(f"{code}\t{label}\n" for code, label in sorted(rows.items())), encoding="utf-8"
    )
    print(f"wrote {len(rows)} NUTS labels to {OUTPUT}")


if __name__ == "__main__":
    main()
