"""All SQL lives in this package. A route, an adapter or a worker containing SQL text is a bug.

Split by concern rather than by table, because the useful grouping is "who calls this": ingest
writes the corpus, jobs serves the derived stages, match is per user, admin is operational, users
is authentication and settings.
"""

from trouveur.db.queries import admin, ingest, jobs, match, users

__all__ = ["admin", "ingest", "jobs", "match", "users"]
