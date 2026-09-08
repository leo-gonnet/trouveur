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
    for _ in range(max(rounds, 1)):
        click.echo(str(asyncio.run(drain_queues(settings))))


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
            profile = await users_q.get_profile(conn, user_id)
            threshold = profile.notify_threshold if profile else 70
            rows = await match_q.pending_digest(conn, user_id, threshold)
            if not rows:
                return f"Nothing pending above {threshold}."
            subject, text, body = mailer.render(rows, threshold)
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
