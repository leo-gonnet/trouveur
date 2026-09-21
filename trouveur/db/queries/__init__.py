"""All SQL lives in this package. A route, an adapter or a worker containing SQL text is a bug.

Split by concern rather than by table, because the useful grouping is "who calls this": ingest
writes the corpus, jobs serves the derived stages, match is per user, admin is operational, users
is authentication and settings, archive is the offsite export.
"""

from trouveur.db.queries import admin, archive, ingest, jobs, match, users

__all__ = ["admin", "archive", "ingest", "jobs", "match", "users"]
