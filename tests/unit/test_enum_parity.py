"""Every vocabulary is declared twice; the two declarations must be proven equal.

A `StrEnum` in Python and an `ENUM` in Postgres describe the same set of values, and nothing makes
them agree. Adding `Seniority.STAFF` without touching the migration type-checks, lints, passes
every other test, and then fails at insert time in production on the first posting that derives to
it -- long after the commit that caused it.

Split deliberately: this half needs no database and so runs on every push. The other half, in
tests/integration/test_schema.py, checks that schema.py matches what Postgres actually created.
Together they chain Python -> schema.py -> Postgres.
"""

from __future__ import annotations

from enum import StrEnum

import pytest
from sqlalchemy.dialects.postgresql import ENUM

from trouveur.db import schema
from trouveur.models import (
    DocumentKind,
    EmploymentType,
    RuleVerdict,
    RunStatus,
    RunTrigger,
    SalaryPeriod,
    Seniority,
    TenantOrigin,
    UserState,
    WorkMode,
)
from trouveur.work import WorkKind

# Postgres enum name -> the Python enum that must mirror it.
BINDINGS: dict[str, type[StrEnum]] = {
    "document_kind": DocumentKind,
    "salary_period": SalaryPeriod,
    "work_mode": WorkMode,
    "seniority": Seniority,
    "employment_type": EmploymentType,
    "work_kind": WorkKind,
    "tenant_origin": TenantOrigin,
    "rule_verdict": RuleVerdict,
    "user_state": UserState,
    "run_status": RunStatus,
    "run_trigger": RunTrigger,
}


def _declared() -> dict[str, tuple[str, ...]]:
    return {
        value.name: tuple(value.enums)
        for value in vars(schema).values()
        if isinstance(value, ENUM)
    }


def test_every_postgres_enum_has_a_python_counterpart():
    """An enum with only a SQL declaration is written as bare strings at every call site.

    That is how `status="runing"` reaches the database: it type-checks, it lints, and it fails on
    whichever run happens to be executing rather than on the commit that introduced it.
    """
    unbound = set(_declared()) - set(BINDINGS)
    assert not unbound, f"Postgres enums with no Python enum: {sorted(unbound)}"


def test_no_binding_names_an_enum_that_does_not_exist():
    stale = set(BINDINGS) - set(_declared())
    assert not stale, f"bindings for enums that no longer exist: {sorted(stale)}"


@pytest.mark.parametrize("name", sorted(BINDINGS), ids=str)
def test_python_and_schema_declare_the_same_members(name: str):
    declared = set(_declared()[name])
    in_python = {member.value for member in BINDINGS[name]}
    assert in_python == declared, (
        f"{name}: only in Python {sorted(in_python - declared)}; "
        f"only in schema.py {sorted(declared - in_python)}. "
        "Adding a member needs a migration (ALTER TYPE ... ADD VALUE), not just an enum edit."
    )
