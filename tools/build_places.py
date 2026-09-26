"""Build trouveur/ingest/data/places.tsv.gz, the city list derivation resolves places against.

GeoNames (https://www.geonames.org, CC BY 4.0) is the source. download.geonames.org is not
reachable from every build environment, so the data is read from the `geonamescache` wheel on
PyPI, which ships GeoNames' cities1000 dump unchanged. Pin the version so a rebuild is
reproducible:

    uv run --with geonamescache==3.0.2 python tools/build_places.py

The output is committed. Changing it changes derived output for every stored posting, so a rebuild
must come with a DERIVE_VERSION bump -- exactly like an edit to ingest/vocab.py.
"""

from __future__ import annotations

import gzip
import io
import json
from importlib.resources import files
from pathlib import Path

from trouveur.ingest.places import COVERED_COUNTRIES

OUTPUT = Path(__file__).resolve().parents[1] / "trouveur" / "ingest" / "data" / "places.tsv.gz"


def _keep_name(name: str) -> bool:
    # Non-Latin scripts can never match a posting on a board we sweep, and three capital letters
    # are airport codes ("FRA", "VIE"), which would read an office code as a city.
    if not name or "\t" in name or "|" in name:
        return False
    if len(name) == 3 and name.isupper():
        return False
    return all(ord(ch) < 0x250 for ch in name)


def main() -> None:
    raw = json.loads(
        files("geonamescache").joinpath("data/cities1000.json").read_text(encoding="utf-8")
    )
    lines = []
    for city in sorted(raw.values(), key=lambda c: int(c["geonameid"])):
        if city["countrycode"] not in COVERED_COUNTRIES:
            continue
        names = sorted({n for n in [city["name"], *city["alternatenames"]] if _keep_name(n)})
        lines.append(
            "\t".join(
                [
                    str(city["geonameid"]),
                    city["name"],
                    city["countrycode"],
                    f"{float(city['latitude']):.5f}",
                    f"{float(city['longitude']):.5f}",
                    str(int(city["population"] or 0)),
                    "|".join(names),
                ]
            )
        )
    buffer = io.BytesIO()
    # mtime=0 so an unchanged input rebuilds byte for byte.
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as out:
        out.write(("\n".join(lines) + "\n").encode("utf-8"))
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(buffer.getvalue())
    print(f"{len(lines)} places written to {OUTPUT} ({len(buffer.getvalue())} bytes)")


if __name__ == "__main__":
    main()
