"""Command line entry points.

Commands are split along the same seams as the code: sweeping, matching and draining are separate
because they have separate costs and separate failure modes, and because being able to run one
without the others is what makes the system debuggable.
"""

from __future__ import annotations

import asyncio
import getpass
import logging
import sys
from pathlib import Path

import click

from trouveur import versions
from trouveur.config import get_settings


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        stream=sys.stderr,
    )


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Debug logging.")
def main(verbose: bool) -> None:
    """Trouveur — self-hosted job recommendation."""
    _setup_logging(verbose)


@main.command()
@click.option("--source", "only_source", default=None, help="Sweep a single source.")
@click.option("--backfill", is_flag=True, help="Use the source's wider window.")
def sweep(only_source: str | None, backfill: bool) -> None:
    """Fetch from sources into the corpus. Writes; does not score and does not email."""
    from trouveur.ingest import run

    report = asyncio.run(run(only_source=only_source, backfill=backfill))
    click.echo(report.summary())
    for name, item in report.per_source.items():
        if item.coverage_shortfall:
            click.echo(
                f"  {name}: {item.coverage_shortfall} postings the source reported were never "
                f"seen (partitions overflowed: {item.partitions_overflowed})",
                err=True,
            )


@main.command()
@click.option("--user", "user_id", type=int, default=None, help="Match one user.")
def match(user_id: int | None) -> None:
    """Retrieve, apply rules and rerank. Reranking spends the user's own LLM credit."""
    from trouveur.match import run_all, run_for_user

    settings = get_settings()
    reports = (
        [asyncio.run(run_for_user(user_id, settings))]
        if user_id
        else asyncio.run(run_all(settings))
    )
    for report in reports:
        click.echo(report.summary())
        for error in report.errors:
            click.echo(f"  ! {error}", err=True)


@main.command()
@click.option("--rounds", default=1, show_default=True, help="How many drain passes to make.")
def drain(rounds: int) -> None:
    """Work the deferred queues once: detail fetches, derivation, dedupe markers, embeddings."""
    from trouveur.runner import drain_queues

    settings = get_settings()

    async def _run() -> None:
        # All rounds inside one event loop. The database engine is a singleton whose pool binds to
        # the loop that created it, so a second asyncio.run() would reuse a pool attached to a
        # loop that has already closed.
        for _ in range(max(rounds, 1)):
            click.echo(str(await drain_queues(settings)))

    asyncio.run(_run())


@main.command()
@click.option(
    "--kind",
    type=click.Choice(["derive", "embed", "dedup"]),
    required=True,
    help="Which derived stage to bring up to date.",
)
@click.option("--chunk", default=5000, show_default=True)
def refill(kind: str, chunk: int) -> None:
    """Queue every row whose derived output is below the current version.

    This is both the backfill and the upgrade path: after bumping a version in
    trouveur/versions.py, this refills the queue and the ordinary worker does the rest. It is
    chunked and resumable, so interrupting it costs nothing and re-running it is free.
    """
    from trouveur.db.engine import connect
    from trouveur.ingest.embed import embedding_version
    from trouveur.work import WorkKind
    from trouveur.work import refill as refill_queue

    target = {
        "derive": str(versions.DERIVE_VERSION),
        "dedup": str(versions.DEDUP_VERSION),
        "embed": embedding_version(),
    }[kind]

    async def _run() -> int:
        cursor, total = 0, 0
        while True:
            async with connect() as conn:
                queued, cursor = await refill_queue(
                    conn, WorkKind(kind), target, chunk_size=chunk, after_job_id=cursor
                )
            total += queued
            if cursor is None:
                return total
            click.echo(f"  queued {total} so far (cursor {cursor})", err=True)

    click.echo(f"queued {asyncio.run(_run())} item(s) for {kind} at version {target}")


@main.command("eval")
@click.option("--limit", "k", default=200, show_default=True, help="Retrieval depth to score at.")
@click.option(
    "--rerank",
    is_flag=True,
    help=(
        "Also score the planted needles with the real reranker. Spends real credit on the key "
        "in TROUVEUR_EVAL_LLM_KEY, so it is opt-in."
    ),
)
@click.option("--save-baseline", is_flag=True, help="Record this run as the new baseline.")
@click.option("--sweep", is_flag=True, help="Fetch a fresh haystack from live sources first.")
def evaluate_retrieval(k: int, save_baseline: bool, sweep: bool, rerank: bool) -> None:
    """Score retrieval against planted needles. Measures recall; never gates anything.

    Requires TROUVEUR_EVAL_DATABASE_URL pointing at a scratch database. Refusing to run against
    DATABASE_URL is deliberate: the harness plants fake postings and rewrites personas' profiles,
    and doing that to a real corpus would be unrecoverable.

    Populate the scratch corpus first with a real sweep, so the needles have to compete with real
    postings. Against an empty haystack every number is 100% and means nothing.
    """
    import os

    eval_url = os.environ.get("TROUVEUR_EVAL_DATABASE_URL")
    if not eval_url:
        raise SystemExit(
            "Set TROUVEUR_EVAL_DATABASE_URL to a scratch database. The harness writes fake "
            "postings and overwrites profiles; it must never point at a real corpus."
        )
    os.environ["DATABASE_URL"] = eval_url

    from trouveur.eval import harness

    if get_settings().embedding_provider == "deterministic":
        click.echo(
            "warning: EMBEDDING_PROVIDER=deterministic produces meaningless vectors, so every "
            "dense number below is noise.",
            err=True,
        )

    if sweep:
        click.echo("fetching a haystack from live sources...", err=True)
        click.echo(f"collected {asyncio.run(harness.snapshot_haystack())} postings", err=True)

    if rerank and not os.environ.get(harness.EVAL_LLM_KEY_VAR):
        raise SystemExit(
            f"--rerank needs {harness.EVAL_LLM_KEY_VAR} set to an OpenRouter key. It is read "
            "from the environment and never stored: users' keys live in the database, entered "
            "through the web UI, and this must not become a second way in."
        )

    card = asyncio.run(harness.run_eval(limit=k, rerank=rerank))
    baseline = harness.read_baseline()
    click.echo(harness.render(card, baseline))

    if lost := harness.regressions(card, baseline):
        click.echo("\nrecall regressed:", err=True)
        for line in lost:
            click.echo(f"  {line}", err=True)

    if save_baseline:
        harness.write_baseline(card)
        click.echo(f"\nbaseline written to {harness.BASELINE}")


@main.group()
def tenants() -> None:
    """Manage the crawl set: which companies a per-tenant source sweeps.

    Deliberately not in the web UI. The crawl set is shared by every user, so enlarging it is an
    operator decision -- one user adding five hundred boards would make everyone pay for it.
    """


@tenants.command("list")
@click.option("--source", default=None, help="Only this source.")
def tenants_list(source: str | None) -> None:
    """Show every tenant with its health, including candidates awaiting review."""
    from trouveur.db.engine import connect
    from trouveur.db.queries import admin

    async def _run() -> list:
        async with connect() as conn:
            return await admin.list_tenants(conn, source)

    rows = asyncio.run(_run())
    if not rows:
        click.echo("No tenants registered.")
        return
    click.echo(f"{'source':<14}{'scope':<24}{'on':<4}{'origin':<11}{'docs':>7}  {'fails':>5}")
    for row in rows:
        click.echo(
            f"{row.source:<14}{row.scope:<24}{'yes' if row.enabled else 'no':<4}"
            f"{row.origin:<11}{row.last_documents or 0:>7}  {row.consecutive_failures or 0:>5}"
            + (f"  {row.last_error[:48]}" if row.last_error else "")
        )


@tenants.command("add")
@click.argument("source")
@click.argument("scopes", nargs=-1, required=True)
@click.option("--disabled", is_flag=True, help="Register without sweeping it yet.")
@click.option("--note", default=None, help="Why this tenant is here.")
def tenants_add(source: str, scopes: tuple[str, ...], disabled: bool, note: str | None) -> None:
    """Register tenants. Accepts a slug or a full careers URL.

    Verify a slug before adding it; an unknown one fails on every sweep thereafter and shows up
    only as a growing failure count.
    """
    from trouveur.db.engine import connect
    from trouveur.db.queries import admin
    from trouveur.sources.errors import SourceError
    from trouveur.sources.registry import clean_scope

    # Each source spells a tenant its own way -- a Greenhouse board is a slug, a Workday board is
    # tenant:instance:site -- so the grammar comes from the registry rather than from here.
    try:
        cleaned = [clean_scope(source, scope) for scope in scopes]
    except SourceError as exc:
        raise SystemExit(str(exc)) from None

    async def _run() -> int:
        async with connect() as conn:
            return await admin.add_tenants(
                conn, source, cleaned, enabled=not disabled, note=note
            )

    added = asyncio.run(_run())
    click.echo(f"added {added} of {len(cleaned)} ({len(cleaned) - added} already registered)")


@tenants.command("import")
@click.argument("path", required=False, type=click.Path(dir_okay=False, path_type=Path))
@click.option("--disabled", is_flag=True, help="Register without sweeping them yet.")
@click.option("--dry-run", is_flag=True, help="Show what would be registered and write nothing.")
def tenants_import(path: Path | None, disabled: bool, dry_run: bool) -> None:
    """Preload boards from a local, unversioned tenant list.

    A stopgap until discovery exists. The file only ever inserts: it is not the crawl set, so
    deleting a line does not remove a board -- use `tenants remove` for that.
    """
    from trouveur.db.engine import connect
    from trouveur.db.queries import admin
    from trouveur.sources.errors import SourceError
    from trouveur.sources.seed import DEFAULT_FILENAME, load_seed

    target = path or Path(DEFAULT_FILENAME)
    try:
        seed = load_seed(target)
    except SourceError as exc:
        raise SystemExit(str(exc)) from None

    if not seed:
        click.echo(f"{target} lists no boards.")
        return
    if dry_run:
        for source, scopes in seed.items():
            click.echo(f"{source}: {len(scopes)} -> {', '.join(scopes)}")
        return

    async def _run() -> list[tuple[str, int, int]]:
        results = []
        async with connect() as conn:
            for source, scopes in seed.items():
                added = await admin.add_tenants(
                    conn, source, scopes, enabled=not disabled, note=f"seeded from {target.name}"
                )
                results.append((source, added, len(scopes)))
        return results

    for source, added, total in asyncio.run(_run()):
        click.echo(f"{source}: {added} added, {total - added} already registered")


@tenants.command("enable")
@click.argument("source")
@click.argument("scope")
def tenants_enable(source: str, scope: str) -> None:
    """Start sweeping a registered tenant."""
    _set_enabled(source, scope, True)


@tenants.command("disable")
@click.argument("source")
@click.argument("scope")
def tenants_disable(source: str, scope: str) -> None:
    """Stop sweeping a tenant without forgetting it."""
    _set_enabled(source, scope, False)


def _set_enabled(source: str, scope: str, enabled: bool) -> None:
    from trouveur.db.engine import connect
    from trouveur.db.queries import admin

    async def _run() -> None:
        async with connect() as conn:
            await admin.set_tenant_enabled(conn, source, scope, enabled)

    asyncio.run(_run())
    click.echo(f"{source}/{scope} {'enabled' if enabled else 'disabled'}")


@tenants.command("remove")
@click.argument("source")
@click.argument("scope")
def tenants_remove(source: str, scope: str) -> None:
    """Forget a tenant entirely, along with its health history.

    Postings already collected from it are kept: they are corpus, not configuration.
    """
    from trouveur.db.engine import connect
    from trouveur.db.queries import admin

    async def _run() -> None:
        async with connect() as conn:
            await admin.remove_tenant(conn, source, scope)

    asyncio.run(_run())
    click.echo(f"removed {source}/{scope}")


@main.command("create-user")
@click.option("--username", prompt=True)
@click.option("--email", default="", help="Optional; without one this user gets no digest.")
def create_user(username: str, email: str) -> None:
    """Create a login. This is the only way to create one."""
    from trouveur.db.engine import connect
    from trouveur.db.queries import users as users_q
    from trouveur.web.auth import hash_password

    password = getpass.getpass("Password: ")
    if len(password) < 12:
        raise SystemExit("Use at least 12 characters; this login faces the internet.")
    if password != getpass.getpass("Repeat: "):
        raise SystemExit("The passwords do not match.")

    async def _run() -> int:
        async with connect() as conn:
            if await users_q.get_user_by_username(conn, username):
                raise SystemExit(f"A user named {username!r} already exists.")
            return await users_q.create_user(
                conn, username, hash_password(password), email.strip() or None
            )

    click.echo(f"created user {username} (id {asyncio.run(_run())})")


@main.command("test-notify")
@click.option("--user", "user_id", type=int, required=True)
def test_notify(user_id: int) -> None:
    """Send this user's pending digest now. Reaches a real inbox."""
    from trouveur.db.engine import connect
    from trouveur.db.queries import match as match_q
    from trouveur.db.queries import users as users_q
    from trouveur.notify import email as mailer

    settings = get_settings()

    async def _run() -> str:
        async with connect() as conn:
            user = await users_q.get_user(conn, user_id)
            if user is None or not user.email:
                return "That user does not exist or has no email address."
            rows = await match_q.pending_digest(conn, user_id)
            if not rows:
                return "Nothing pending to send."
            subject, text, body = mailer.render(rows)
            mailer.send(settings, user.email, subject, text, body)
            await match_q.mark_notified(conn, user_id, [row.id for row in rows])
            return f"sent {len(rows)} posting(s) to {user.email}"

    click.echo(asyncio.run(_run()))


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8080, show_default=True)
@click.option("--reload", is_flag=True)
def serve(host: str, port: int, reload: bool) -> None:
    """Run the web UI."""
    import uvicorn

    uvicorn.run("trouveur.web.app:app", host=host, port=port, reload=reload)


@main.command()
def runner() -> None:
    """Run the scheduler and queue workers. Long-running; starts real scans."""
    from trouveur.runner import main as runner_main

    asyncio.run(runner_main())


if __name__ == "__main__":
    main()
