"""The one hard filter, defined once. Read by retrieval, by the re-score estimate and by the
coverage report; the three disagreeing would mean a page reporting a corpus no reader can see.

Written for a query whose job is aliased `j` and whose facets are aliased `f`, which is what
every caller already writes.
"""

from __future__ import annotations

from typing import Any

from trouveur.ingest import places


def _remote_allowed(facets: str) -> str:
    return f"(CAST(:remote_anywhere AS boolean) OR {facets}.work_mode <> 'remote')"


def _admitted(facets: str) -> str:
    return f"""(
        {_remote_allowed(facets)}
        AND (
            {facets}.countries && CAST(:countries AS text[])
            OR {facets}.unplaced_countries && CAST(:countries AS text[])
            OR {facets}.place_ids && CAST(:area_ids AS integer[])
            OR {facets}.unplaced_countries && CAST(:area_countries AS text[])
            OR (CAST(:remote_anywhere AS boolean) AND {facets}.work_mode = 'remote')
        )
    )"""


_PLACED_TWINS = """
    FROM job s
    JOIN job_facet sf ON sf.job_id = s.id
    WHERE s.dedup_group = j.dedup_group
      AND s.id <> j.id
      AND s.closed_at IS NULL
      AND (cardinality(sf.countries) > 0 OR cardinality(sf.unplaced_countries) > 0)
"""

# The ONE filter applied before ranking, and deliberately the only one. Every other preference a
# user states -- salary, languages -- reaches the reranker as text instead, because a rule
# that rejected a posting here hid it from the reader with no way to find out it existed. The four
# enum filters this replaced (work mode, seniority, employment type, salary) each also dropped
# every posting whose facet was unstated, which is why the form had to offer a "not stated" tick
# box to undo the filter the user had just set.
#
# Unstated location passes: a posting nobody parsed a country out of is not a posting somewhere
# else. A posting whose town could not be resolved is judged by the countries it may be in, against
# the user's countries and every country their circles reach into -- it might be inside one. So is
# "Geneva": it has no country of its own, but it is Switzerland or the US and nowhere else, which
# is enough to keep it from a reader in Vienna.
#
# A fully remote role is shown only to a reader who asked for them (`remote_anywhere`), and then
# wherever it was posted: for a role with no office the place named says nothing about whether the
# reader can take it. A reader who did not ask sees none, whatever country it lists -- "remote in
# Albania, Andorra, Austria, ..." was 21 of 50 postings in one Vienna reader's edition. Only a role
# DERIVED remote is kept out; hybrid, on site and unstated all pass.
#
# Cities are never compared by name here: `place_ids` are GeoNames ids resolved by derivation, and
# the circles are resolved from the same list, so `Wien` and `Vienna` are one id on both sides.
#
# A posting whose source named no place at all is judged by its open twins that name one: an
# aggregator had blanked the location of a role its origin board states, and passing it as
# unstated showed a Vienna reader Elastic's London role. Only when the source said NOTHING --
# "Sobernheim" beside a twin in Coburg is the same role in another town, and must keep its own.
SQL = f"""
    (
        {_remote_allowed("f")}
        AND (
            CAST(:anywhere AS boolean)
            OR {_admitted("f")}
            OR (
                cardinality(f.countries) = 0
                AND cardinality(f.unplaced_countries) = 0
                AND CASE
                    WHEN j.location_text = '' AND EXISTS (SELECT 1 {_PLACED_TWINS})
                    THEN EXISTS (SELECT 1 {_PLACED_TWINS} AND {_admitted("sf")})
                    ELSE TRUE
                END
            )
        )
    )
"""


def params(area: Any) -> dict[str, Any]:
    """The parameters `SQL` reads. Takes anything carrying the four location fields -- a
    UserProfile, or the area a coverage report groups several profiles into."""
    countries = list(area.countries or [])
    city_ids = list(area.city_ids or [])
    area_ids, area_countries = places.area(city_ids, area.radius_km)
    return {
        # Nothing picked at all accepts everywhere. Cities alone must not.
        "anywhere": not countries and not city_ids,
        "countries": countries,
        "area_ids": area_ids,
        "area_countries": area_countries,
        "remote_anywhere": bool(area.remote_anywhere),
    }
