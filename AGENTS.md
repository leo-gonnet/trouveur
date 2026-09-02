# Coding Agent Guidelines — Trouveur

You are working as a senior engineer on a small, single-user job-search pipeline. Be skeptical of
overengineering. Prefer surgical changes over rewrites. Think before coding: most tasks here are
small, and the expensive mistakes are silent ones (a scraper that returns zero rows and reports
success), not loud ones.

Unnecessarily clever abstractions and narrating comments are liabilities. So are tests that
cannot fail.

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

A daily pipeline plus a small web UI. One user. Roughly 200 new jobs/day.

```
collect -> upsert/dedupe -> rules filter -> LLM score -> email digest
```

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

Module map:

| Path | Responsibility |
|---|---|
| `trouveur/sources/` | One adapter per external source. Network lives here and nowhere else. |
| `trouveur/db/` | **All SQL.** No SQL text outside this package. |
| `trouveur/filters/` | `rules.py` (free, deterministic) then `llm.py` (paid, cached). |
| `trouveur/notify/` | Digest rendering and SMTP. |
| `trouveur/web/` | Routes, auth, templates. Reads the DB; never fetches, never runs the pipeline. |
| `trouveur/pipeline.py` | Orchestration only. No parsing, no SQL. |
| `trouveur/runner/` | Schedules and executes pipeline runs. The only caller of `pipeline.run` in production. |

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

## Source adapter rules

**This is the highest-value section in this file.** These behaviours were verified by probing the
live APIs. Every one of them fails *silently* — zero rows or an HTTP 400, never an exception.

### Arbeitsagentur (`sources/arbeitsagentur.py`)

Base: `https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/`
Header: `X-API-Key: jobboerse-jobsuche` plus a normal browser `User-Agent`.

- **The two endpoints we use are on different API versions. This is not a typo.**

  | Purpose | Path | Working version | Other versions |
  |---|---|---|---|
  | Search | `pc/v6/jobs` | **v6** | `pc/v4/jobs`, `pc/v4/app/jobs` -> **403** |
  | Detail | `pc/v4/jobdetails/{base64(refnr)}` | **v4** | `pc/v5`, `pc/v6` -> **403** |

  Do not "harmonise" these onto one version. Both directions were probed; each returns 403 on
  the version that looks consistent.
- **The result list key is `ergebnisliste`, not `stellenangebote`.** Older docs disagree.
- **Search results carry no description.** `stellenangebotsBeschreibung` only exists on the
  detail endpoint, so a description costs one extra request per job. Fetch details only for
  postings that survive the rules filter.
- Useful detail-only flags: `istArbeitnehmerUeberlassung` (staffing agency) and
  `istPrivateArbeitsvermittlung`. Prefer these over keyword-matching "Zeitarbeit" in free text.
- **Send umlauts literally (URL-encoded).** `wo=München` returns ~178 results;
  `wo=Muenchen` returns **0**. Never transliterate a city name.
- **`arbeitsort=` is not a synonym for `wo=`.** `arbeitsort=Wien` returns 0; `wo=Wien` returns 6.
- **There is no server-side remote filter.** `homeoffice=true` returns **HTTP 400** and
  `arbeitszeit=ho` returns 0 rows. Filter remote client-side on the `homeofficemoeglich` field.
- **Never send `pav`.** It returns **HTTP 400**.
- Dedupe key is `referenznummer`. Daily runs use `veroeffentlichtseit=1`; backfills use `7`.

### karriere.at (`sources/karriere_at.py`)

robots.txt (checked 2026-08-31): `User-agent: * / Disallow:` — crawling is explicitly permitted.

- **The keyword search has no working pagination.** `?page=N`, `?seite=N` and `/seite-N` all
  return the same 15 rows (`/seite-N` 404s). The real listing is XHR-driven. Do not add a
  pagination loop; it will silently re-fetch page 1 forever.
- **Recall scales with keyword count, not page depth.** Adding profile keywords is the way to
  widen Austrian coverage.
- **Search listings render the title server-side**, so candidates can be filtered on title
  before paying for a detail page. Use `rules.title_is_plausible` for that gate.
- The sitemap (~12,500 jobs, ~950 changing daily) carries `lastmod` but **no titles**, so a
  sweep spends one request per job to discover mostly irrelevant vacancies. It is opt-in
  (`sitemap_sweep`) and off by default. Prefer more keywords over sweeping.
- Austrian salaries are usually stated **monthly**, and Austrian contracts commonly pay 14
  monthly salaries. The rules filter annualises with x14 so it only rejects jobs clearly below
  the floor.

### Personio (`sources/personio.py`)

- `https://{tenant}.jobs.personio.de/xml` is public and needs no authentication.
- **There is no global index of tenants.** This source only sees the rows in `personio_tenant`,
  added through the web UI. The list stays out of git because it reveals who you are watching.
  It is deliberately its own table, not a `profile` column: adding a company must not bump
  `profile.version`, which would invalidate every cached LLM score.
- Some tenants 307-redirect or are retired. Skip non-200 responses; do not treat them as errors.
- The feed carries **no salary and no country**, only an office city. Leave both None rather
  than inferring a country from an office name.

### JobSpy (`sources/jobspy_source.py`)

- Optional dependency (`uv sync --extra jobspy`); pulls pandas. When absent the source yields
  nothing rather than failing.
- Runs **last and narrow**: every board caps a search near 1000 results, and LinkedIn rate-limits
  around page 10 from a single IP. This host has one IP.
- JobSpy is synchronous, so call it via `asyncio.to_thread`, never inline.
- Values arrive from a DataFrame, so missing fields show up as the string `"nan"`.

### All adapters, without exception

- **Failure isolation.** One dead source must never abort the run. `pipeline.py` wraps each source
  and records the error in `source_state`. A failing source is a logged warning, not a crash.
- **Delta-first.** Use the cheapest incremental mechanism the source offers (`veroeffentlichtseit`,
  sitemap `lastmod`, ETag). Never re-fetch the whole corpus on a daily run.
- **Be polite.** At most ~1 request/second per host. Honour ETag/`If-Modified-Since`. Send a
  `User-Agent` that identifies this project.
- **Parse defensively.** Sources change without notice. A missing optional field is `None`, not a
  `KeyError`. A field you cannot parse must not discard the whole record.
- Prefer the shared `sources/jsonld.py` schema.org parser over a bespoke one. karriere.at, most ATS
  boards, and most company career pages all emit `JobPosting` JSON-LD.

## DON'T: robots.txt policy

**Check `robots.txt` before adding any source, and record the finding in the adapter docstring.**

- **Never scrape willhaben.at.** Its robots.txt states automated access is *"expressively
  forbidden"* and disallows `/jobs/webapi/`, `/rest/`, `/restapi/`.
- **Never scrape StepStone search-result pages.** `stepstone.at` disallows `/5/ergebnisliste.html`
  and `/?*`. Its listings reach us through other sources anyway.
- karriere.at is explicitly permitted (`User-agent: * / Disallow:`) and publishes a jobs sitemap.
  That is why it is our Austrian backbone.

Do not "temporarily" add a disallowed source to test something.

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

- **The rules stage must run first and must be cheap.** It exists to make the LLM stage small.
- **Always cache by `content_hash`.** A job is scored once, ever. Re-scoring is a bug.
- Changing the profile deliberately invalidates cached scores — that is the one sanctioned
  re-score path.
- Keep prompts in `filters/llm.py`. Do not scatter prompt fragments across modules.
- Model: `deepseek/deepseek-v4-flash` via OpenRouter, pinned to one provider. Request strict
  JSON; validate it with pydantic and treat a malformed response as "unscored", never as score 0.
- **Always send `reasoning: {"enabled": false}`.** Without it a reasoning model spends the whole
  `max_tokens` budget on hidden thinking and returns empty content — the batch is lost *and*
  billed.
- **Always pin `llm_provider`.** Unpinned, OpenRouter spreads one model across ~17 backends at a
  6.5x price spread and differing quantisation (fp4 vs fp8), so neither cost nor scores are
  reproducible. Pinning also lets `data_collection: "deny"` keep the profile away from backends
  that may train on it.
- Changing the model is an eval question, not a taste question. The harness, the labelled dataset
  and the findings are kept outside this repo; re-run them before swapping models, and do not
  reason about model choice from list prices alone (several providers bill differently).

## Web UI

- **Recommendations and Search have deliberately different scope, do not blur them.**
  `/recommendations` (`queries.recommendations`) is the strict page: `rule_verdict='pass'` AND
  `llm_score IS NOT NULL` AND `llm_score >= threshold`. It is meant to be short, often empty.
  `/search` (`queries.search_jobs`) shows every scraped job regardless of filter outcome — that
  is where a rejected or unscored job stays visible. Adding a rule-verdict or score filter to
  `search_jobs` defeats its purpose; keep the distinction in the query layer, not just the UI.
- **The dashboard's health panel depends on `pipeline._record_source_health`.** Every source in
  `build_sources()` gets a `source_state` row every run, success or failure, so a source that
  stops running shows up as unhealthy rather than silently vanishing from the numbers. If you add
  a source, it is covered automatically — do not special-case it.
- **Styling lives in one file:** `web/static/app.css`, using CSS custom properties for theming
  (incl. `prefers-color-scheme: dark`). No inline `<style>` blocks in templates beyond one-off
  layout tweaks (`style="width:45%"` etc.); reusable patterns get a class in `app.css` instead.
- No build step, no Node, on purpose — plain CSS and HTMX only.

## Database rules

- **Every schema change is an Alembic migration.** No exceptions, no manual `psql` DDL on the
  running instance.
- **SQL lives only in `trouveur/db/`.** A route or an adapter containing SQL is a bug.
- Use `INSERT ... ON CONFLICT` for upserts; the uniqueness contract is
  `(source, source_native_id)` and `content_hash`.
- **Keep the two search indexes in sync.** Searchable text is indexed twice on purpose: a
  `german` `tsvector` for stemming, and a `pg_trgm` index over an `unaccent`-folded column for
  substring matching inside German compounds. If you change what is searchable, change both.
  Neither alone is sufficient — `german` alone misses `Ingenieur` inside `Wirtschaftsingenieur`,
  and trigram alone misses umlaut folding.

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

Prefer the narrowest command that proves your change. Do not run the full pipeline against live
sources to test a parser — use a fixture.

**Run freely:** `pytest`, `ruff check`, `alembic upgrade head`, and `run --dry-run`. None of them
touch the network, spend money, or write.

**Ask first** — each of these has a side effect you cannot take back:

| Command | Why |
|---|---|
| `trouveur test-notify` | reaches a real inbox |
| anything hitting OpenRouter (the LLM filter, or an eval run) | billed per call |
| `trouveur run` without `--dry-run` | writes to the database, may email |
| `trouveur runner` | long-running; starts real scans on a schedule |
| `git push`, `gh` commands that create or comment | public and hard to undo |

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
