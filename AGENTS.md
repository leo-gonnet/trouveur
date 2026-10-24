# Coding Agent Guidelines — Trouveur

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

**This file is short on purpose, and editing it needs human approval.** It holds only rules that
cross modules; a fact about one source, query or function is a comment at that code. This is a side
project for a handful of users: where a rule reads as absolute, take it as "there was a reason, find
it first" rather than as a safety interlock.

## Comments

The default is **no comment**. Small functions with accurate names need no explanation.

Write a comment only to record something a reader cannot see from the code:

- A constraint imposed by an external system we do not control.
- A deliberate omission ("we do not send `pav`, it 400s").
- Ordering or concurrency requirements.

**Every silent-failure trap in an adapter is exactly this kind of constraint and MUST carry a comment
at the site that depends on it.** If someone "cleans up" the umlaut handling because nothing
explained it, we silently lose most German results.

Do not write: restatements of the code, section banners, changelog notes, or TODOs without an owner.
Leave existing comments alone unless the code they describe changed.

## The organizing principle

**Everything derived is a pure function of something we stored, and every non-trivial stage is
versioned.**

```
raw archive ──normalize(v)──> job ──derive(v)──> facets
     │                          └───embed(v)───> vector
     └── kept forever; the only input that cannot be recomputed
```

Bump a version in `versions.py` and the queue refills every row below it: **upgrade and backfill
are one code path**.

- **Normalisation produces structure; derivation produces interpretation**, so a parser fix is a
  re-derive, not a re-crawl.
- **Never guess**: every enum has `UNKNOWN`, every scalar nullable. A wrong facet looks like data
  while poisoning the filters.
- **Never re-derive downstream.** A second answer to one question diverges from the first.

## Project overview

Python 3.12, FastAPI, Jinja2, HTMX, Postgres 16 + pgvector, SQLAlchemy Core, asyncpg, Alembic,
Compose behind Caddy. **`uv` is required**, and anything needing root belongs in a script the user
runs.

**`runner` is the only thing that sweeps or drains queues**; web enqueues and reads, never fetches.
They share only the database, so either can be down alone.

| Path | Responsibility |
|---|---|
| `sources/<name>/` | `client.py` network only; `normalize.py` pure and versioned. |
| `sources/` | `{board,feed}.py` the two shared sweeps; `registry.py` the **only** dispatch. |
| `ingest/` | `persist` (single write path), `derive` (pure), `vocab`, `places`, `embed`. |
| `discovery/` | Link → board; archive mining; reported links. Upstream of normalisation, so it may name a source. |
| `match/` | Expansion, hybrid retrieval, RRF, reranking. `work/queue.py` is the one queue. |
| `db/` | **All SQL.** None outside it. |
| `web/` | Routes, auth, templates. Reads the DB; never fetches, never sweeps. |
| `runner/` | Schedules runs, drains queues, the only caller of `ingest.run`. |

## Ingestion is user-agnostic; selection is per user

- **Never put a user's keywords in a CORPUS query**, which caps recall at what they thought to
  type. They are fine in a DISCOVERY query, whose output is a lead.
- **Retention and the candidate window are different windows.** `retrieval_horizon_days` is how long
  we keep a posting; `retrieve.NEW_ARRIVALS_HOURS` how far back a scheduled run looks. A run the
  *user* caused passes the whole horizon.
- **Age and readiness differ**: staleness reads `COALESCE(posted_at, first_seen_at)`, the candidate
  window `ready_at`, stamped once and never moved.
- **A scheduled run matches only once its sweep is prepared**, waiting only for work that could run.

## Critical code patterns

- **Async everywhere**; `httpx.AsyncClient`, never `requests`, never block the loop. CPU work goes
  through `asyncio.to_thread`. **Batch everything** — a per-row `await` or mutation is a bug.
- **Pydantic models are the contract.** `CanonicalJob` and `JobFacets` are shared verbatim by
  adapters, the DB layer and the web views; never build a parallel dict for one of them.
- **Adapters yield, they do not write**, and **normalizers are pure** — no clock, no client, no
  connection — so a replay reproduces the first result.
- **Nothing downstream of normalisation may name a source**; needing an edit there as well as in a
  source means the abstraction leaked.
- **Store UTC; convert at the edge, through `clock.py` only.** `settings.timezone` applies only
  where a person is involved, such as an hour somebody *chose*, which drifts with DST if resolved
  elsewhere.
- **Specific exceptions with complete-sentence messages**; never `raise Exception(...)` or
  `except Exception: pass`. A broad catch belongs only where isolation is the point — per source,
  per user, the tick loop — and must log and record.

## Sources, access and the crawl set

**Recall comes first.** `robots.txt` and a site's terms do not rule out a source, and private or
undocumented endpoints are allowed. What rules one out is being unable to reach it without getting
blocked, or without a credential we are not entitled to.

- **Read the adapter first, and re-probe a live API before changing a constant**, since these fail
  silently. Every source-specific fact is a comment in its own adapter.
- **Don't get blocked**: a block loses the source for days. Go slow, back off at the first sign, and
  be polite **per _provider_, not per hostname** (~1 req/s via `sources/http.py`, keyed on the
  registrable domain). A host's `robots.txt` is *block risk*, never permission.
- **Never use a personal account**, and **don't take a source we may not retain** — the archive is
  permanent.
- **Isolate failures**: one dead source must not abort a run, and a wholesale failure pauses the
  source rather than charging it per posting.
- **Completeness is not success.** `closable_scopes` names scopes whose *entire* live set the sweep
  saw, never per source; a delta sweep returns none, and closing on it retires everything.
- Be **delta-first**, **parse defensively** (one unreadable field must not discard the record), and
  **scope an external id by tenant** unless the platform documents its ids as globally unique.
- **The crawl set lives in the database** (`source_tenant`), not the repo, so **the corpus a commit
  produces is not reproducible from that commit alone**. Changing it is a CLI action. Keep
  configuration and observation apart, and validate a scope at the write.
- **Agents propose, humans merge**: an agent opens a PR or writes a row disabled, never enlarging
  the crawl or the politeness budget on its own.

## Privacy

The repo is public. Users' career data is not, and must never enter it.

- **Never publish** `.env`, dumps, or anything holding a user's objectives, salary, employers,
  address or password. **Fixtures are hand-written** — never a real payload.
- **Profiles live in the database only**, edited through the web UI: no file path in or out.
- **Secrets come from the environment only**: never read one from the repo, never log one, and show
  a generated password once in the response that created it, never in a URL.

## LLM cost discipline

One installation key; credit per user, metered against whoever the call was for.

- **Retrieval is free and must work with no key and no credit**, the deterministic query expansion
  being the floor rather than a degraded fallback.
- **Score a posting once per profile version, ever**, cached on `(content_hash, user,
  profile_version)`. **Bump `profile.version` only for `SCORING_FIELDS`**, never from retrieval, or
  rows needing a re-score look current.
- **Check the limits before the calls go out**, from the database rather than a snapshot, since
  runs overlap. Nobody is exempt, admins included.
- **Keep the provider pinned and reasoning disabled**, or price and scores stop being reproducible.

## Web UI

**The design system is the token block and component list at the top of `app.css`. Read that header
first.** A template uses those classes only; a new look is a new component there, not a one-off.
One word means one thing everywhere (`good`/`warn`/`bad`, `high`/`mid`/`low`), and repeated markup
goes through `_macros.html`.

- **One stylesheet, no build step, no Node, and nothing the browser loads from another origin**
  (tested): htmx and the font are vendored, so no CDN learns the reader's IP and page.
- **Everything htmx does must be visible when it fails**: a failed swap raises a toast, and a
  lapsed session leaves by `HX-Redirect`, not a 303 pasted into a button.
- **Set anything every page needs in the session middleware, not per route** — admin-only included,
  enforced by URL prefix, never by a check each handler must remember.

### The reader's pages

- **`/recommendations` is retrieved and scored**, ordered by score. **`/search` shows every scraped
  posting**, closed and unscored included: it is the only view of what was collected, so keep score
  filters out of it.
- **An edition is stored, never derived and never rewritten**: it holds what the reader was
  *shown*, where `user_job_match` holds today's verdict. **Page by cursor, not OFFSET.**
- **Location is the only hard filter**; everything else on Profile is a preference the reranker
  reads as text. Don't add a second — a filter hides a posting with no way for the reader to learn
  it existed. **Prefer losing precision to losing recall**: the reranker recovers precision, nothing
  recovers a posting filtered out.

### Operations and accounts

- **Operations exists to surface what fails silently**, and **a run must be watchable while it
  runs**, or a slow source and a wedged one look alike. **Cancellation is a request, not a kill**:
  a sweep stops between sources.
- **The first account is the admin**, so a test fixture must set `is_admin` explicitly or it
  silently tests an admin's view of every page.
- Check `is_active` per request: a token outlives a disabling.

## Database rules

- **Every schema change is an Alembic migration**, no manual DDL, and **SQL lives only in
  `db/queries/`**. Both tested, as is `schema.py` matching the migration chain.
- **Two identities, deliberately separate**: `job (source, external_id)` is *provenance*, the same
  row from the same board, and `job.dedup_group` is *semantic*, the same job in the world. It is a
  **marker that must never merge or delete rows**, nor be loosened to a fuzzy match.
- **Keep the two lexical paths in sync** — a `german` `tsvector` that stems and ranks, and a trigram
  index over an `unaccent`-folded column for substrings inside German compounds. **asyncpg uses
  `numeric_dollar` paramstyle, so `%` is NOT doubled**: `'%%'` silently breaks the trigram path.
- **Backfills are chunked, resumable and idempotent**, paging by keyset.

## Testing

**Write tests for** parsing, derivation, retrieval fusion, dedupe and upsert behaviour, query shape,
cost controls, and every silent-failure trap a comment records. **Not for** getters, pydantic,
SQLAlchemy, or mocks restating mocks.

- **No network anywhere, ever**: synthetic fixtures and a stubbed transport. Probe a live API from
  a throwaway shell, never from a test.
- **`tests/unit/` needs no database**; `tests/integration/` is skipped unless
  `TROUVEUR_TEST_DATABASE_URL` is set. **Run integration before shipping a change to `db/queries/`,
  a migration, or a template** — SQL that compiles is not SQL that runs.
- **When you add a structural guard, check it can fail** by briefly introducing the violation.
- **A rendering test must render a job card**; over an empty list it proves only that the query
  returned. `StrictUndefined` is on, so a missing field raises instead of rendering blank.
- **`trouveur eval` is not a test**: numbers against a baseline, never a merge gate. It measures
  recall only — **precision is not measurable** here.

## Commands

`uv run trouveur --help` lists them.

Prefer the narrowest command that proves your change. **Ask first** for anything you cannot take
back: `sweep` and `eval --sweep` (live APIs), `match` (credit), `refill --kind embed` (hours of
CPU), `test-notify` (a real inbox), `runner`, `git push`.

## Commit style

Conventional commits, one scope per commit; types `feat`, `fix`, `refactor`, `test`, `docs`,
`chore`, `build`. Describe the problem and the evidence, not a diff summary.

Never commit generated files or secrets. Two are deliberately committed and neither is ours:
`places.tsv.gz` (vocabulary) and `web/static/vendor/` (licences in `vendor/VENDOR.md`).
