"""What Postgres actually built, versus what schema.py says it built.

The other half of enum parity lives in tests/unit/test_enum_parity.py and needs no database. This
half closes the chain: Python matches schema.py, schema.py matches the server. Neither alone is
enough, because the migration is separate DDL that SQLAlchemy never sees.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ENUM

from tests.unit.test_enum_parity import BINDINGS
from trouveur.db import schema
from trouveur.db.engine import connect

_LABELS = """
SELECT t.typname AS name, e.enumlabel AS label
FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid
JOIN pg_namespace n ON n.oid = t.typnamespace
WHERE n.nspname = 'public'
"""


async def _server_enums() -> dict[str, set[str]]:
    async with connect() as conn:
        rows = await conn.execute(sa.text(_LABELS))
    found: dict[str, set[str]] = {}
    for row in rows:
        found.setdefault(row.name, set()).add(row.label)
    return found


async def test_schema_and_postgres_declare_the_same_enum_members(clean_db):
    """Catches an enum member added to schema.py without an ALTER TYPE in the migration."""
    on_server = await _server_enums()
    declared = {
        value.name: set(value.enums)
        for value in vars(schema).values()
        if isinstance(value, ENUM)
    }
    assert set(declared) <= set(on_server), (
        f"enums in schema.py that Postgres does not have: {sorted(set(declared) - set(on_server))}"
    )
    for name, members in declared.items():
        assert members == on_server[name], (
            f"{name}: only in schema.py {sorted(members - on_server[name])}; "
            f"only in Postgres {sorted(on_server[name] - members)}"
        )


async def test_every_bound_enum_exists_on_the_server(clean_db):
    on_server = await _server_enums()
    missing = set(BINDINGS) - set(on_server)
    assert not missing, f"enums bound in Python but absent from the database: {sorted(missing)}"


async def test_every_table_column_in_schema_exists_on_the_server(clean_db):
    """schema.py builds queries; the migration builds the database. A drift breaks one silently.

    The table-level check lives in tests/unit/test_architecture.py and is static. This is the
    column-level one, which needs the server: a column renamed in the migration but not in
    schema.py produces a query referencing a column that does not exist, at runtime only.
    """
    async with connect() as conn:
        rows = await conn.execute(
            sa.text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'public'"
            )
        )
    on_server: dict[str, set[str]] = {}
    for row in rows:
        on_server.setdefault(row.table_name, set()).add(row.column_name)

    problems = []
    for name, table in schema.metadata.tables.items():
        for column in table.columns:
            if column.name not in on_server.get(name, set()):
                problems.append(f"{name}.{column.name}")
    assert not problems, f"columns in schema.py missing from the database: {problems}"
