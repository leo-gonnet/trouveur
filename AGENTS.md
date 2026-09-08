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

## The organizing principle

**Everything derived is a pure function of something we stored, and every non-trivial function is
versioned.**

```
raw archive ──normalize(v)──> job ──derive(v)──> facets
     │                          └───embed(v)───> vector
     └── kept forever; the only input that cannot be recomputed
```

This buys evolution. When the location parser is wrong — and it will be — we re-derive from the
archived payload. If only the parsed value had been stored, that reading would be gone and no
backfill could recover it, because a backfill re-derives from stored columns and those columns hold
the wrong answer. The only repair would be re-fetching a million postings.

Bumping a version in `trouveur/versions.py` refills the work queue with every row below it, and the
ordinary worker drains it. **Upgrade and backfill are therefore the same code path**, which is the
only arrangement in which the repair path stays tested — it runs every day.

Three rules follow, and none of them are negotiable:

- **Normalisation produces structure; derivation produces interpretation.** A location arrives as
  the source wrote it and is parsed one stage later. That split is what makes a parser fix a
  re-derive rather than a re-crawl.
- **Never guess.** Every enum has UNKNOWN and every scalar is nullable. A wrong facet is worse than
  a missing one: it silently poisons every filter and ranking that reads it while looking like data.
- **Never re-derive downstream.** If the matcher or a template parses a location again, there are
  two answers to one question and they will diverge — which surfaces as a filter and a score
  disagreeing about the same posting, and is close to undebuggable from the symptom.

## Comments

The default is **no comment**. Small functions with accurate names need no explanation.

Write a comment only to record something a reader cannot see from the code:

- A constraint imposed by an external system we do not control.
- A deliberate omission ("we do not send `pav`, it 400s").
- Ordering or concurrency requirements.

**Every trap in [Source adapter rules](#source-adapter-rules) is exactly this kind of constraint and
MUST carry a comment at the site that depends on it.** If someone "cleans up" the umlaut handling
because nothing explained it, we silently lose most German results.

Do not write: restatements of the code, section banners, changelog notes, or TODOs without an owner.
Leave existing comments alone unless the code they describe changed.

## Project overview

A comprehensive system for job recommendation, with excellent recall and good precision. A pipeline
that runs periodically, plus a rich but simple web UI. Multi-user (for now, a few).

- **Python 3.12**, FastAPI + Jinja2 + HTMX, PostgreSQL 16 + pgvector, SQLAlchemy Core + asyncpg,
  Alembic.
- **`uv` is required.** The system `venv` module is broken on the target host (no `ensurepip`), so
  `python3 -m venv` fails. There is no passwordless sudo either: anything needing root belongs in a
  reviewed script the user runs themselves, not in a command you attempt.
- Deployed as Docker Compose (`compose.yaml`). The web UI is never published to the host; Caddy
  reverse-proxies it and terminates TLS.
- **The `runner` service is the only thing that sweeps sources or drains queues.** It owns the
  schedule (a `pipeline_schedule` row, edited in the web UI) and the `pipeline_run` queue. The web
  app enqueues rows and reads status; it never fetches and never runs a scan. web and runner share
  nothing but the database, so either can be down without breaking the other.

### Ingestion is user-agnostic; selection is per user

This is the central design decision and the one most easily undone by accident.

```
INGEST  (shared corpus, no user involved)
  sweep ──> raw archive ──> job ──> facets ──> vector ──> lifecycle

MATCH   (per user, cheap, re-runnable)
  profile ──> expanded queries ──> hard filters
          ──> dense ANN ─┐
          ──> BM25/FTS  ─┴─ RRF fusion ──> rules cut ──> LLM rerank ──> digest
```

V1 queried every source with the user's own keywords, which capped recall at whatever the user
thought to type: a job they would have wanted but did not name was never fetched at all, so no
amount of downstream ranking could recover it. **Never reintroduce a user's keywords into a source
query.** Sources sweep by their own structure; users select over the corpus.

### Module map

| Path | Responsibility |
|---|---|
| `trouveur/sources/<name>/client.py` | Network only. Yields `RawDocument`. No parsing. |
| `trouveur/sources/<name>/normalize.py` | Pure, versioned: archived payload → `CanonicalJob`. |
| `trouveur/sources/registry.py` | The **only** place source-specific dispatch happens. |
| `trouveur/sources/<name>/boards.txt` | Tenant registry for a per-tenant source. Configuration, in git. |
| `trouveur/ingest/persist.py` | The single write path from archive to `job`. |
| `trouveur/ingest/derive.py` | Deterministic interpretation → facets. Pure. |
| `trouveur/ingest/vocab.py` | **Every** vocabulary, once. |
| `trouveur/ingest/embed/` | Provider seam; local ONNX default. |
| `trouveur/ingest/pipeline.py` | Sweep orchestration. No parsing, no SQL. |
| `trouveur/work/queue.py` | The one versioned work queue. |
| `trouveur/match/` | Query expansion, hybrid retrieval, RRF, rules cut, reranking. |
| `trouveur/db/` | **All SQL.** No SQL text outside this package. |
| `trouveur/web/` | Routes, auth, templates. Reads the DB; never fetches, never sweeps. |
| `trouveur/runner/` | Schedules runs and drains queues. The only caller of `ingest.run`. |
| `trouveur/versions.py` | Every derived stage's version, in one file. |

## Critical code patterns

- **Async everywhere in the pipeline.** Sources are I/O-bound HTTP fan-out; use `httpx.AsyncClient`.
  Never call `requests` or block the loop. Synchronous CPU work (embedding) goes through
  `asyncio.to_thread`.
- **Batch everything.** At tens of thousands of documents a sweep, the round trip is the
  bottleneck, not the work. A per-row `await` inside a loop over a batch is a bug.
- **Pydantic models are the contract.** `CanonicalJob` and `JobFacets` are shared verbatim by
  adapters, the DB layer and the web views. Do not define a parallel dict shape for the same thing.
- **Adapters yield, they do not write.** A source hands `RawDocument` batches to a sink; only the
  pipeline persists. This keeps adapters testable with no database.
- **Normalizers are pure.** No clock, no client, no connection. Purity is what makes replay over the
  archive reproduce the original result.
- **Nothing downstream of normalisation may name a source.** If a change needs edits in the matcher
  or the web layer as well as in a source, the abstraction has leaked. Enforced by a test.
- **Typed enums, not raw strings**, with an `UNKNOWN` member wherever a source could surprise us.
- **Specific exceptions with complete-sentence messages.** Never `raise Exception(...)`, never
  `except Exception: pass`. The sanctioned broad catches are per-source isolation in
  `ingest/pipeline.py`, per-user isolation in `match/pipeline.py`, and the runner's tick loop —
  each must log and record.

## Source adapter rules

**This is the highest-value section in this file.** Every behaviour below was verified by probing
the live APIs on **2026-09-08**, and every one of them fails *silently* — zero rows, the entire
corpus, or an HTTP 400 — never an exception. Re-probe before changing any of them; do not infer them
from the shape of a URL. Each has a regression guard in `tests/test_arbeitsagentur.py` or
`tests/test_greenhouse.py`.

### Arbeitsagentur (`sources/arbeitsagentur/`)

Base: `https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/`
Headers: `X-API-Key: jobboerse-jobsuche` plus a browser-shaped `User-Agent` (the library default is
rejected). `robots.txt` returns 403 and does not apply; this is a published JSON API for third-party
use.

- **The two endpoints are on different API versions. This is not a typo.**

  | Purpose | Path | Works | Other versions |
  |---|---|---|---|
  | Search | `v6/jobs` | **v6** | `v4/jobs` → **403** |
  | Detail | `v4/jobdetails/{base64(refnr)}` | **v4** | `v5`, `v6` → **403** |

  Both directions were probed. Do not "harmonise" them.
- **`veroeffentlichtseit` is NOT a number of days.** Only `{0, 1, 7, 14}` filter. Values `2, 3, 4,
  5, 30, 100` are all accepted and all return the **entire ~1.0M corpus**. A "2-day catch-up" would
  silently fetch a million rows and report success.
- **`size` caps at 500.** `size=501` returns **HTTP 200 with an empty result list**, so raising the
  page size "for throughput" collects nothing at all.
- **`size × page` may not exceed 10 000**; beyond it, HTTP 400. Any partition with more matches than
  that is unreachable — count and log the overflow, never silently keep the first 10 000.
- **The result list key is `ergebnisliste`**, not `stellenangebote`. Older docs disagree.
- **Search results carry no description.** `stellenangebotsBeschreibung` exists only on the detail
  endpoint, so a description costs one extra request per posting.
- **Broad sweeping needs no keyword.** An empty query returns the whole corpus (~1 015 000 live,
  ~36 000 new/day). Partition by the `berufsfeld` facet: 136 partitions daily, largest ~2 500,
  comfortably inside the result window. Read the facet from the API rather than hardcoding it.
- **The berufsfeld facet does not sum to the total** (~0.4% carry none), so a partitioned sweep has a
  small blind spot by construction. Record the shortfall; do not assume it is zero.
- **Send umlauts literally.** Never transliterate a city name.
- **Never send `pav`** (400), `homeoffice` (400) or `arbeitszeit=ho` (0 rows). There is no
  server-side remote filter; remote comes from `homeofficemoeglich`.
- Useful detail-only flags: `istArbeitnehmerUeberlassung`, `istPrivateArbeitsvermittlung`. Prefer
  these over keyword-matching "Zeitarbeit" in prose. Absent a detail fetch the answer is **unknown**,
  not False.

### Greenhouse (`sources/greenhouse/`)

`robots.txt` (`boards-api.greenhouse.io`, checked 2026-09-08): the only rule is `Disallow: /embed/`,
so `/v1/boards/` is explicitly permitted. No authentication.

- One request returns a tenant's **complete** live board (`meta.total`, no pagination), so the
  response is itself the seen-set and closing is exact rather than inferred.
- **`?content=true` includes the full description**, so there is no detail phase.
- **`content` is HTML that has itself been HTML-escaped** (`&lt;div&gt;`). Unescape once, *then*
  strip tags. Doing it the other way round leaves entity text in the description and feeds markup
  to the embedder and the reranker.
- **`location.name` is free text**, and multiple locations arrive semicolon-separated in one string
  (`"Remote, Canada; Remote, US"`). Pass it through unparsed; interpreting it is derivation.
- **There is no index of tenants anywhere.** `sources/greenhouse/boards.txt` is the only list of
  boards that exists. An unknown slug returns 404. Verify a slug with one request before adding it,
  or it fails every day until someone notices.
- Scope external ids by slug (`gitlab:8503792002`). Nothing documents Greenhouse ids as globally
  unique, and a collision would silently merge two unrelated postings onto one row.

### All adapters, without exception

- **Failure isolation.** One dead source must never abort a run. `ingest/pipeline.py` wraps each
  source and records the error in `source_sweep`.
- **Completeness is not success.** `SweepOutcome.closable_scopes` names the scopes whose *entire*
  live set the sweep observed. A delta sweep returns none: seeing only what was published yesterday
  says nothing about whether an older posting is still live, and closing on it would retire the
  whole corpus on the first run. Report completeness per scope, not per source — one tenant's board
  failing says nothing about another's.
- **Delta-first.** Use the cheapest incremental mechanism the source offers. Never re-fetch the
  whole corpus on a daily run.
- **Be polite.** At most ~1 request/second per host, via `sources/http.py`. Identify the project in
  the `User-Agent`.
- **Parse defensively.** A missing optional field is `None`, not a `KeyError`. A field you cannot
  parse must not discard the whole record.

## Configuration versus observation

A per-tenant source needs a list of tenants, because some publish no index of their own. That list
is **configuration and lives in the repository** (`sources/<name>/boards.txt`), not in the database
and not editable in the UI. Three reasons, in order of weight:

1. **It is not per-user.** The corpus is shared, so which companies get crawled is an operator
   decision. Exposing it per user means one user adding five hundred boards and everyone paying the
   crawl cost.
2. **It should be reviewable.** Adding a company is a diff, with the slug verified in the commit
   that adds it.
3. **A commit should reproduce its corpus.** With the list in a database, the same code produces
   different results on two installations and neither is wrong.

What the database keeps is the **observation**: `source_scope_health`, one row per tenant, written
on every sweep. The distinction is load-bearing and was learned the hard way — the table this
replaced mixed an editable board list with health columns that nothing ever wrote, so the UI
reported "last success: —" indefinitely, which reads as "not run yet" rather than "never recorded".

Never put configuration and observation in one table. If a human authors it, it belongs in git; if
the system produces it, it belongs in Postgres.

Discovering *new* tenants is deliberately not part of the runtime. If it is ever automated, it
should be a maintenance script that proposes a diff to the registry file for review — never a
process that writes the crawl set while the pipeline is running.

## DON'T: robots.txt policy

**Check `robots.txt` before adding any source, and record the finding in the adapter docstring
with the date.**

- **Never scrape willhaben.at.** Its robots.txt states automated access is *"expressively
  forbidden"* and disallows `/jobs/webapi/`, `/rest/`, `/restapi/`.
- **Never scrape StepStone search-result pages.** `stepstone.at` disallows `/5/ergebnisliste.html`
  and `/?*`.
- Do not "temporarily" add a disallowed source to test something.

## DON'T: privacy policy

The repo is public. Users' career data is not, and must never enter it.

- **Never commit:** `.env`, database dumps, or anything holding a user's objectives, salary
  expectations, employers watched, email address or API key.
- **Profiles and users' API keys live in the database only**, edited through the web UI. There is
  deliberately no file-based path in or out for them; do not add one. This does **not** apply to the
  tenant registry — see below.
- **Test fixtures are hand-written and synthetic.** Never commit a captured page or a real scraped
  payload — third-party content, repository bloat, and a fixture nobody wrote is a fixture nobody
  understands when it starts failing.
- **Secrets come from the environment only**, delivered by the Compose `env_file`. Never read a
  secret from a file inside the repo, and never log one.
- **Users' API keys are encrypted at rest** with `ENCRYPTION_KEY` and surfaced only as a
  fingerprint. Never render a key back to the browser, not even to the user who set it.

Before any commit: `git status` must be clean of the above.

## LLM cost discipline

**There is no installation-wide API key.** Reranking runs on each user's own OpenRouter credential,
so the deployment has no LLM spend of its own and one user's exhausted budget cannot affect another.

- **Retrieval and the rules cut run first**, and both are free. They exist to make the paid stage
  small.
- **Always cache by `(content_hash, user, profile_version)`.** A posting is scored once per profile,
  ever. Re-scoring an unchanged posting is a bug, not an inefficiency.
- **Check the ceiling before a batch, not after.** A retry loop on someone else's card is not
  something to discover from the user. Spend is metered in **USD**, the currency OpenRouter bills
  in — an EUR column would put a stale exchange rate between the meter and the cap.
- **Bump `profile.version` only for fields that change what a good match is** (`SCORING_FIELDS`). A
  notification threshold must not invalidate a cache and bill a re-score.
- **Query expansion costs one call per profile version, not per job**, and is cached. The
  deterministic expansion is the floor, not a degraded fallback: retrieval must work fully with no
  key at all.
- **A malformed response leaves a batch unscored, never scored 0.** Scoring garbage as 0 caches a
  wrong verdict and hides good jobs permanently.
- **Always send `reasoning: {"enabled": false}`.** Without it a reasoning model spends the whole
  `max_tokens` budget on hidden thinking and returns empty content — batch lost *and* billed.
- **Always pin a provider.** Unpinned, OpenRouter spreads one model across many backends at a wide
  price spread and differing quantisation, so neither cost nor scores are reproducible. Pinning also
  lets `data_collection: "deny"` keep profiles away from backends that may train on them.
- Changing the default model is an eval question, not a taste question.

## Web UI

- **Recommendations and Search have deliberately different scope; do not blur them.**
  `/recommendations` is the strict page: retrieved AND `rule_verdict='pass'` AND
  `llm_score >= threshold`. It is meant to be short, often empty. `/search` shows **every** scraped
  posting regardless of filter outcome, including rejected, unscored and closed ones — that is the
  only view of what was actually collected. Adding a verdict or score filter to `search_jobs`
  defeats its purpose; keep the distinction in the query layer, not just the UI.
- **Templates never re-derive.** Read stored facets. A template that parses a location or infers a
  work mode is a second implementation of a question `derive.py` already answered.
- **The dashboard must surface what fails silently**: partition overflow, sweep completeness, the
  gap between stored and recommendable, queue depth, and more than one embedding version present.
- **Styling lives in one file:** `web/static/app.css`, using CSS custom properties for theming (incl.
  `prefers-color-scheme: dark`). No inline `<style>` blocks beyond one-off layout tweaks.
- No build step, no Node, on purpose — plain CSS and HTMX only.

## Database rules

- **Every schema change is an Alembic migration.** No exceptions, no manual `psql` DDL.
- **SQL lives only in `trouveur/db/queries/`.** A route, adapter or worker containing SQL is a bug.
  Enforced by a test.
- **`schema.py` and the migration must declare the same tables.** Enforced by a test.
- Uniqueness contracts:
  - `job (source, external_id)` is **provenance identity** — "the same row from the same board".
  - `job.dedup_group` is **semantic identity** — "the same job in the world". It is a **marker** and
    must never merge or delete rows. V1 conflated the two in one UNIQUE constraint and silently
    dropped every posting that arrived from a second source. Markers can be re-run; a merge cannot
    be undone.
- **`job_embedding` holds open postings only.** Closing a job deletes its row in the same statement,
  which is what keeps the ANN index proportional to the live corpus rather than to all history —
  with no denormalised `is_open` flag to drift. This is an invariant, not an optimisation.
- **Keep the two search paths in sync.** Searchable text is indexed twice on purpose: a `german`
  `tsvector` for stemming and weighting, and a `pg_trgm` index over an `unaccent`-folded column for
  substring matching inside German compounds. Neither alone is sufficient — `german` alone misses
  `Ingenieur` inside `Wirtschaftsingenieur`, and trigram alone misses umlaut folding and cannot
  rank. If you change what is searchable, change both.
- **asyncpg uses `numeric_dollar` paramstyle, so `%` is NOT doubled** in `LIKE` patterns. Writing
  `'%%'` (as psycopg2 requires) leaves two literal percent signs and silently breaks the trigram
  path.
- **Batch every mutation.** `WHERE id = ANY($1)`, not a loop.
- **Backfills are chunked, resumable and idempotent**, guarded with `IS DISTINCT FROM`, and use
  keyset pagination — never `OFFSET`, which re-scans everything it already skipped.

## Testing

A test earns its place only if it can fail for a reason a reviewer would care about.

**Write tests for:** parsing logic, derivation, the rules cut, retrieval fusion, dedupe/upsert
behaviour, query shape, cost controls, and **every trap in this file**.
**Do not write tests for:** getters, pydantic itself, SQLAlchemy itself, or mocks restating mocks.

- **No network and no database in the default suite.** Use synthetic fixtures and a stubbed
  transport. It runs in under a second; keep it that way. To check live API behaviour while
  debugging, use a throwaway shell command, never a test file — and if what you learn is durable,
  write it into this file.
- **One deliberate exception: `tests/test_integration_db.py`**, skipped unless
  `TROUVEUR_TEST_DATABASE_URL` is set. **SQL that compiles is not SQL that runs**, and nothing else
  can catch that class of bug — writing it found a query that bundled two statements (asyncpg
  rejects them), an f-string prefix dropped so a literal `{_HARD_FILTERS}` shipped to the server,
  and place names missing from both search columns, which broke city search entirely. **Run it
  against a throwaway database before shipping any change to `db/queries/` or the migration:**

  ```bash
  podman run -d --rm --name pg -e POSTGRES_USER=trouveur -e POSTGRES_PASSWORD=x \
      -e POSTGRES_DB=trouveur -p 55432:5432 pgvector/pgvector:pg16
  export TROUVEUR_TEST_DATABASE_URL=postgresql+asyncpg://trouveur:x@127.0.0.1:55432/trouveur
  DATABASE_URL=$TROUVEUR_TEST_DATABASE_URL uv run alembic upgrade head
  uv run pytest
  ```
- Name tests `test_<behaviour>_when_<condition>` or as a plain statement of the invariant.
  Arrange/Act/Assert, no cleverness.
- **When you add a structural guard, verify it can fail** by temporarily introducing the violation.
- **Mandatory regression guards** (these protect against silent production failure):
  - `veroeffentlichtseit` accepts only `{0,1,7,14}`; a sweep never uses another value.
  - `size <= 500` and `size × page <= 10 000`.
  - No `pc/v4` search URL and no `v5`/`v6` detail URL is ever constructed.
  - A delta sweep reports no closable scope, and a failed board is never closable.
  - Work mode is never decided by description prose (company boilerplate, benefits lists).
  - `content_hash` is versioned and changes when a description arrives.
  - `%%` never survives SQL compilation.
  - `search_jobs` carries no verdict or score filter.
  - German search finds `Wirtschaftsingenieur` when the user types `ingenieur`, and finds
    `München` when the user types `munchen` (both need the integration test).
  - Closing a posting deletes its embedding.
  - Every source package is registered; `schema.py` and the migration agree; no SQL outside
    `db/queries/`; no source name used as a value downstream.

## Commands

```bash
uv sync                                   # install/refresh dependencies
uv sync --extra embeddings                # add the local ONNX embedding provider
uv run pytest                             # full suite (no network, no database)
uv run ruff check trouveur/               # lint
uv run alembic upgrade head               # apply migrations
uv run alembic revision -m "add X"        # new migration

uv run trouveur sweep                     # fetch sources into the corpus
uv run trouveur sweep --source greenhouse # one source
uv run trouveur drain                     # work the deferred queues once
uv run trouveur refill --kind derive      # re-queue everything below the current version
uv run trouveur match --user 1            # retrieve, cut, rerank for one user
uv run trouveur create-user               # the ONLY way to create a login
uv run trouveur serve                     # dev server on 127.0.0.1:8080
uv run trouveur runner                    # scheduler + queue workers
```

Prefer the narrowest command that proves your change. Do not run a sweep against live sources to
test a parser — use a fixture.

**Run freely:** `pytest`, `ruff check`, `alembic upgrade head` (against a throwaway database).

**Ask first** — each of these has a side effect you cannot take back:

| Command | Why |
|---|---|
| `trouveur sweep` | hits live APIs and writes |
| `trouveur match` | spends a user's LLM credit |
| `trouveur refill --kind embed` | re-embeds the corpus; hours of CPU |
| `trouveur test-notify` | reaches a real inbox |
| `trouveur runner` | long-running; starts real scans on a schedule |
| `git push`, `gh` commands that create or comment | public and hard to undo |

## Commit style

Conventional commits, one scope per commit:

```
feat(sources): add the Personio adapter
fix(arbeitsagentur): stop sending veroeffentlichtseit=2, it returns the whole corpus
chore(deploy): pin the postgres image to pg16
```

Types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `build`. Describe the problem and the
evidence, not a diff summary. Never commit generated files or secrets.

## Keeping this file updated

If you fix a bug that existed because a rule was unwritten, write the rule here. If a rule here no
longer matches the code, delete it. A stale instruction file is worse than none.
