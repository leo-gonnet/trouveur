# Coding Agent Guidelines — Trouveur

This document provides essential information for AI coding agents working on the Trouveur codebase.

**IMPORTANT — READ FIRST**

- **Act as a Senior Software Engineer and Software Architect.** Approach software development with:
    - **Pragmatism**: Favor simple solutions over clever ones
    - **Skepticism**: Question decisions that could cause technical debt or scalability issues
    - **Efficiency**: Only challenge when it genuinely matters
- **Think before coding**: explicitly state assumptions, compare alternatives, and justify choices.
- **Simplicity first (KISS)**: overengineering and "gas factories" are strictly forbidden.
- **Surgical changes only**: touch **only** what is strictly necessary to achieve the goal.
- **Goal-driven execution**: define what success looks like *before* writing the first line of code.
- **Reuse before writing**: the pattern you need almost always exists already. Find the closest equivalent in the codebase and follow it instead of inventing a second way to do the same thing.
- **No comments by default**: see [Comments](#comments). Over-commenting is one of the most common reasons a PR written by an agent has to be revised.
- **How Trouveur looks is decided by the design system**: see [Web UI](#web-ui). No color, spacing, radius or component pattern may be invented in feature code.
- **A test has to be able to fail for a reason a reviewer would care about**: see [Testing](#testing). More tests do not make a change safer. We don't care about coverage. We want meaning.
- **Build and test only what you touched** rather than the whole repo.

## Comments

The default is **no comment**. Small functions with accurate names need no explanation.

Write a comment only to record something a reader cannot see from the code:

- A constraint imposed by an external system we do not control.
- A deliberate omission ("we do not send `pav`, it 400s").
- Ordering or concurrency requirements.

**Every trap in "Source adapter rules" below is exactly this kind of constraint and MUST carry a
comment at the site that depends on it.** If someone "cleans up" the umlaut handling because
nothing explained it, we silently lose most German results.

Do not write: restatements of the code, section banners, changelog notes, or TODOs without an
owner. Leave existing comments alone unless the code they describe changed.

## Project overview

A comprehensive system for job recommendation, with excellent recall, good precision. Made of a pipeline that runs periodically, plus a rich but simple web UI. Multi-users (for now a few). 

- **Python 3.12**, FastAPI + Jinja2 + HTMX, PostgreSQL 16, SQLAlchemy Core + asyncpg, Alembic.
- **`uv` is required.** The system `venv` module is broken on the target host (no `ensurepip`), so
  `python3 -m venv` fails. There is no passwordless sudo either: anything needing root belongs in
  a reviewed script the user runs themselves, not in a command you attempt.
- Deployed as Docker Compose (`compose.yaml`). The web UI is never published to the host; Caddy
  reverse-proxies it and terminates TLS.
- **The `runner` service is the only thing that executes a scan.** It owns the schedule (a
  `pipeline_schedule` row, edited from the web UI) and drains the `pipeline_run` queue. The web
  app enqueues rows and reads status; it never calls `pipeline.run`. web and runner share
  nothing but the database, so either can be down without breaking the other.

## Critical code patterns

- **Async everywhere in the pipeline.** Sources are I/O-bound HTTP fan-out; use `httpx.AsyncClient`
  and `async for`. Never call `requests` or block the loop.
- **Pydantic models are the contract.** `models.Job` is shared verbatim by adapters, DB layer, and
  web views. Do not define a parallel dict shape for the same thing.
- **Adapters yield, they do not write.** A source returns `AsyncIterator[Job]`; only `pipeline.py`
  persists. This keeps adapters testable without a database.
- **Typed enums, not raw strings**, for `user_state` and `rule_verdict`. Include an `UNKNOWN`
  member where a source could surprise us.
- **Specific exceptions with complete-sentence messages.** Never `raise Exception(...)`, never
  `except Exception: pass`. The one sanctioned broad catch is per-source isolation (below), and it
  must log and record the error.

## DON'T: privacy policy

The repo is public. The user's career data is not, and must never enter it.

- **Never commit:** `.env`, database dumps, or anything holding the user's objectives, salary
  expectations, employers watched, or email address.
- **The profile and the watched-employer list live in the database only**, edited through the
  web UI. There is deliberately no file-based path in or out; do not add one.
- **Test fixtures are hand-written and synthetic.** Never commit a saved karriere.at page or other
  real scraped HTML — it is third-party copyrighted content and it bloats the repo.
- **Secrets come from the environment only**, delivered by the Compose `env_file` — `.env` next
  to `compose.yaml` on the host, `chmod 600`, never committed. Never read a secret from a file
  inside the repo, and never log one.

Before any commit: `git status` must be clean of the above.

## LLM cost discipline

The LLM stage is the only part of this system that costs money per run.

- **The LLM stages must be cheap.**. Ensure to have cost protection, and transparency towards the user about real time costs.
- Keep prompts in `filters/llm.py`. Do not scatter prompt fragments across modules.
- **Always pin `llm_provider`.** Unpinned, OpenRouter spreads one model across ~17 backends at a
  6.5x price spread and differing quantisation (fp4 vs fp8), so neither cost nor scores are
  reproducible. Pinning also lets `data_collection: "deny"` keep the profile away from backends
  that may train on it.
- Changing the model is an eval question, not a taste question. The harness, the labelled dataset
  and the findings are kept outside this repo; re-run them before swapping models, and do not
  reason about model choice from list prices alone (several providers bill differently).

## Web UI

- **Styling lives in one file:** `web/static/app.css`, using CSS custom properties for theming
  (incl. `prefers-color-scheme: dark`). No inline `<style>` blocks in templates beyond one-off
  layout tweaks (`style="width:45%"` etc.); reusable patterns get a class in `app.css` instead.
- No build step, no Node, on purpose — plain CSS and HTMX only.

## Database rules

- **Every schema change is an Alembic migration.** No exceptions, no manual `psql` DDL on the
  running instance.
- **SQL lives only in `trouveur/db/`.** A route or an adapter containing SQL is a bug.

## Testing

A test earns its place only if it can fail for a reason a reviewer would care about.

**Write tests for:** parsing logic, the rules filter, dedupe/upsert behaviour, search behaviour,
and every trap listed above.
**Do not write tests for:** getters, pydantic itself, SQLAlchemy itself, or mocks restating mocks.

- **No network in tests.** Use synthetic fixtures and a stubbed `httpx` transport. To check live
  API behaviour while debugging, use a throwaway shell command, never a test file — and if what
  you learn is durable, write it into this file. Rate limits are real and this host has one IP;
  do not loop over live sources to explore.
- Name tests `test_<behaviour>_when_<condition>`. Arrange/Act/Assert, no cleverness.
- **Mandatory regression guards** (these protect against silent production failure):
  - `wo=München` yields substantially more than zero results, and the query string is not
    transliterated.
  - A `pc/v4` URL is never constructed.
  - German search finds `Wirtschaftsingenieur` when the user types `ingenieur`, and finds
    `München` when the user types `munchen`.

## Commands

```bash
uv sync                                   # install/refresh dependencies
uv run pytest                             # full suite (no network)
uv run pytest tests/test_arbeitsagentur.py   # narrow while iterating
uv run alembic upgrade head               # apply migrations
uv run alembic revision -m "add X"        # new migration
uv run trouveur run --dry-run             # full pipeline, no writes, no email
uv run trouveur run --source arbeitsagentur  # one source
uv run trouveur backfill --days 7         # wider initial fetch
uv run trouveur create-user               # the ONLY way to create a login
uv run trouveur serve                     # dev server on 127.0.0.1:8080
```

## Commit style

Conventional commits, one scope per commit:

```
feat(sources): add karriere.at sitemap adapter
fix(arbeitsagentur): stop transliterating umlauts in wo=
chore(deploy): pin the postgres image to pg16
```

Types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `build`. Describe the problem and the
evidence, not a diff summary. Never commit generated files or secrets.

## Keeping this file updated

If you fix a bug that existed because a rule was unwritten, write the rule here. If a rule here no
longer matches the code, delete it. A stale instruction file is worse than none.
