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

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "alembic" / "versions"


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


def _tables_after_migrations() -> set[str]:
    """Every table the chain leaves behind, applying each upgrade() in revision order.

    The whole chain, not just the baseline: a table added by a later migration is as real as one
    created in 0001, and reading only the baseline would report it as drift.
    """
    tables: set[str] = set()
    for path in sorted(MIGRATIONS.glob("[0-9]*.py")):
        upgrade = path.read_text().split("def downgrade")[0]
        tables |= set(re.findall(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+)", upgrade))
        tables -= set(re.findall(r"DROP TABLE (?:IF EXISTS )?(\w+)", upgrade))
    return tables


def test_schema_and_migration_declare_the_same_tables():
    """schema.py builds queries, the migration builds the database; a drift breaks one silently."""
    in_migration = _tables_after_migrations()
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


def _evalx_modules() -> list[str]:
    return sorted(
        f"evalx.{path.stem}"
        for path in (ROOT / "evalx").glob("*.py")
        if path.stem != "__init__"
    )


def test_every_evalx_module_still_matches_the_code_it_reaches_into():
    """The lab imports production's retrieval and scoring directly, and nothing else checks it.

    It is not part of the package and no test ever ran it, so a rename in `trouveur/match/`
    broke it silently and stayed broken: `from trouveur.match.retrieve import RETRIEVAL_LIMIT`
    raised ImportError on the next eval run, long after the change that caused it.

    A module whose optional third-party dependency is not installed is skipped -- `uv run pytest`
    is meant to work on a plain install -- but a missing `trouveur` symbol is a failure.
    """
    import importlib

    broken = []
    for name in _evalx_modules():
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if (exc.name or "").split(".")[0] in {"trouveur", "evalx"}:
                broken.append(f"{name}: {exc}")
        except ImportError as exc:
            broken.append(f"{name}: {exc}")
    assert not broken, "evalx has drifted from the code it imports: " + "; ".join(broken)


def test_evalx_calls_production_functions_with_arguments_they_accept():
    """An import check is not enough: a signature can change without a name changing.

    `dense_candidates` gained a required `seen_since` and every call in the lab kept compiling,
    kept importing, and raised TypeError only when somebody next paid to run an evaluation.
    Bound against the real signature here, where it costs nothing.
    """
    import importlib
    import inspect

    offenders = []
    for path in sorted((ROOT / "evalx").glob("*.py")):
        tree = ast.parse(path.read_text())
        # alias -> module, for `from trouveur.x import y as z` and `from trouveur.x import y`
        aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("trouveur"):
                for imported in node.names:
                    aliases[imported.asname or imported.name] = (
                        f"{node.module}.{imported.name}"
                    )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            owner = node.func.value
            if not isinstance(owner, ast.Name) or owner.id not in aliases:
                continue
            try:
                module = importlib.import_module(aliases[owner.id])
            except ImportError:
                continue
            target = getattr(module, node.func.attr, None)
            if not callable(target) or inspect.isclass(target):
                continue
            positional = [a for a in node.args if not isinstance(a, ast.Starred)]
            if len(positional) != len(node.args):
                continue  # *args: the count is not knowable here
            keywords = {k.arg: None for k in node.keywords if k.arg is not None}
            try:
                inspect.signature(target).bind(*positional, **keywords)
            except TypeError as exc:
                offenders.append(
                    f"{path.name}:{node.lineno} {owner.id}.{node.func.attr}(): {exc}"
                )
    assert not offenders, "evalx calls a production function wrongly: " + "; ".join(offenders)
