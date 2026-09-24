"""All SQL lives in this package. A route, an adapter or a worker containing SQL text is a bug.

Split by who calls it, not by table.
"""

from trouveur.db.queries import admin, archive, ingest, jobs, match, users

__all__ = ["admin", "archive", "ingest", "jobs", "match", "users"]
