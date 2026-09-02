from __future__ import annotations

import asyncio
import getpass
import logging
import sys

import click

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
    """Trouveur — self-hosted job aggregator with pluggable sources."""
    _setup_logging(verbose)


@main.command()
@click.option("--days", default=1, show_default=True, help="Look back this many days.")
@click.option("--source", "only_source", default=None, help="Run a single source.")
@click.option("--dry-run", is_flag=True, help="Fetch and filter only. No writes, no email.")
@click.option("--no-llm", is_flag=True, help="Skip the paid LLM scoring stage.")
@click.option("--no-email", is_flag=True, help="Do everything except send the digest.")
def run(days: int, only_source: str | None, dry_run: bool, no_llm: bool, no_email: bool) -> None:
    """Run the daily pipeline."""
    from trouveur.pipeline import run as run_pipeline

    report = asyncio.run(
        run_pipeline(
            since_days=days, only_source=only_source, dry_run=dry_run,
            use_llm=not no_llm, send_email=not no_email,
        )
    )
    click.echo(report.summary())
    for name, error in report.errors.items():
        click.echo(f"  source {name} failed: {error}", err=True)


@main.command()
@click.option("--days", default=7, show_default=True, help="Backfill window.")
def backfill(days: int) -> None:
    """Wider initial fetch. Same as `run --days N --no-email`."""
    from trouveur.pipeline import run as run_pipeline

    report = asyncio.run(run_pipeline(since_days=days, send_email=False))
    click.echo(report.summary())


@main.command("create-user")
@click.option("--username", prompt=True)
def create_user(username: str) -> None:
    """Create or replace the single login. The only way to make a user."""
    from argon2 import PasswordHasher

    from trouveur.db import queries as q
    from trouveur.db.engine import connect

    password = getpass.getpass("Password: ")
    if password != getpass.getpass("Repeat password: "):
        raise SystemExit("passwords do not match")
    if len(password) < 12:
        raise SystemExit("use at least 12 characters")

    async def _create() -> None:
        async with connect() as conn:
            await q.create_user(conn, username, PasswordHasher().hash(password))

    asyncio.run(_create())
    click.echo(f"user {username!r} created")


@main.command("test-notify")
def test_notify() -> None:
    """Send a digest built from fixture data. Reaches a real inbox."""
    from types import SimpleNamespace

    from trouveur.notify import email

    rows = [
        SimpleNamespace(
            id=1, title="Wirtschaftsingenieur (m/w/d) Prozessoptimierung",
            company="Beispiel GmbH", location_city="Wien", location_country="AT",
            remote=True, salary_min=65000, salary_max=80000, salary_period="YEAR",
            llm_score=88, llm_reason="Strong match: remote, Wien-based, process optimisation.",
            url="https://example.com/job/1",
        )
    ]
    subject, text, html = email.render(rows, threshold=70)
    email.send(get_settings(), subject, text, html)
    click.echo("test digest sent")


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8080, show_default=True)
@click.option("--reload", is_flag=True)
def serve(host: str, port: int, reload: bool) -> None:
    """Run the web UI. Binds loopback only; Caddy terminates TLS in front of it."""
    import uvicorn

    uvicorn.run("trouveur.web.app:app", host=host, port=port, reload=reload)


@main.command()
def runner() -> None:
    """Run the scheduler/worker that owns all pipeline execution.

    Long-running. Owns the daily schedule and drains the run queue; the web UI only enqueues.
    """
    from trouveur.runner.service import main as runner_main

    asyncio.run(runner_main())


if __name__ == "__main__":
    main()
