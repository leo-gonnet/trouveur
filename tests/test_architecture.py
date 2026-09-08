"""Structural guards: one concept, one implementation.

These are cheap insurance against the failure mode that is hardest to see in review -- a second
copy of a vocabulary or a second normaliser that disagrees with the first. Every such
disagreement is silent: no error, just postings that quietly fail to group or filter.
"""

from __future__ import annotations

import ast
import pkgutil
import re
from pathlib import Path

import trouveur.sources as sources_pkg
from trouveur.db.schema import metadata
from trouveur.sources.registry import NORMALIZERS

ROOT = Path(__file__).resolve().parent.parent
MIGRATION = ROOT / "alembic" / "versions" / "0001_pipeline_v2.py"


def test_every_source_normalizer_is_registered():
    """A normaliser the registry does not know about is dead code or a silent second path."""
    packages = {
        module.name
        for module in pkgutil.iter_modules(sources_pkg.__path__)
        if module.ispkg
    }
    assert packages, "no source packages found; the discovery below is not actually checking"
    assert packages == set(NORMALIZERS), (
        f"source packages {sorted(packages)} do not match registered normalisers "
        f"{sorted(NORMALIZERS)}"
    )


def test_schema_and_migration_declare_the_same_tables():
    """schema.py builds queries, the migration builds the database; a drift breaks one silently."""
    in_migration = set(re.findall(r"CREATE TABLE (\w+)", MIGRATION.read_text()))
    assert in_migration == set(metadata.tables), (
        f"only in migration: {sorted(in_migration - set(metadata.tables))}; "
        f"only in schema.py: {sorted(set(metadata.tables) - in_migration)}"
    )


def test_sql_lives_only_in_the_db_package():
    """A route, adapter or worker containing SQL text is a bug."""
    offenders = []
    pattern = re.compile(r"\b(SELECT|INSERT INTO|UPDATE\s+\w+\s+SET|DELETE FROM)\b")
    for path in (ROOT / "trouveur").rglob("*.py"):
        if "db/queries" in path.as_posix() or path.name == "schema.py":
            continue
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if pattern.search(line) and not line.lstrip().startswith("#"):
                offenders.append(f"{path.relative_to(ROOT)}:{number}")
    assert not offenders, f"SQL found outside trouveur/db/queries: {offenders}"


def _docstrings(tree: ast.AST) -> set[int]:
    """Ids of string nodes that are docstrings, so prose can cite a source without failing."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


def test_nothing_downstream_of_normalisation_names_a_source():
    """Facets, matching and the web layer must not branch on which board a posting came from.

    If they can, every new source becomes work in several places instead of one, and the registry
    stops being the single point of dispatch. Checked over the AST rather than the text, so a
    comment or docstring explaining where a rule came from is allowed and a literal the code
    actually compares against is not.
    """
    names = set(NORMALIZERS)
    downstream = ["ingest/derive.py", "ingest/vocab.py", "match", "web", "notify"]
    offenders = []
    for relative in downstream:
        target = ROOT / "trouveur" / relative
        files = [target] if target.is_file() else list(target.rglob("*.py"))
        for path in files:
            tree = ast.parse(path.read_text())
            skip = _docstrings(tree)
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and id(node) not in skip
                    and node.value.lower() in names
                ):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}: {node.value!r}")
    assert not offenders, f"source names used as values downstream: {offenders}"
