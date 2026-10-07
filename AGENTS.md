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

Bump a version in `versions.py` and the queue refills with every row below it, so **upgrade and
backfill are one code path** and the repair path stays tested.

- **Normalisation produces structure; derivation produces interpretation.** Archive what the source
  wrote and parse it one stage later, so a parser fix is a re-derive and not a re-crawl.
- **Never guess.** Every enum has `UNKNOWN`, every scalar is nullable; a wrong facet looks like data
  while poisoning every filter that reads it. And **never re-derive downstream** — a matcher or
  template that parses a location again is a second answer to one question, and they will diverge.

## Project overview

Job recommendation with excellent recall and good precision: a periodic pipeline plus a rich but
simple web UI. A few users, self-hosted, under construction. **Python 3.12**, FastAPI + Jinja2 +
HTMX, PostgreSQL 16 + pgvector, SQLAlchemy Core + asyncpg, Alembic, Docker Compose behind Caddy.
**`uv` is required** — `python3 -m venv` fails on the target host, and there is no passwordless
sudo, so anything needing root belongs in a script the user runs themselves.

**The `runner` service is the only thing that sweeps or drains queues.** web enqueues and reads,
never fetches; they share nothing but the database, so either can be down alone.

| Path | Responsibility |
|---|---|
| `sources/<name>/` | `client.py` is network only; `normalize.py` is pure and versioned. |
| `sources/` | `{board,feed}.py` are the two shared sweeps; `registry.py` is the **only** dispatch. |
| `ingest/` | `persist` (the single write path), `derive` (facets, pure), `vocab`, `places`, `embed`. |
| `discovery/` | `resolve.py` turns a link into a board (pure, versioned); `mine.py` reads our own archive for links; `reports.py` answers a link a reader reported. Upstream of normalisation, so it may name a source. |
| `match/` | Expansion, hybrid retrieval, RRF, reranking. `work/queue.py` is the one queue. |
| `db/` | **All SQL.** No SQL text outside this package. |
| `web/` | Routes, auth, templates. Reads the DB; never fetches, never sweeps. |
| `runner/` | Schedules runs and drains queues. The only caller of `ingest.run`. `eval/` scores it. |

## Ingestion is user-agnostic; selection is per user

```
INGEST  sweep ──> raw archive ──> job ──> facets ──> vector ──> lifecycle
MATCH   profile ──> queries ──> location filter ──> dense+lexical ──> RRF ──> rerank ──> edition
```

- **Never put a user's keywords in a CORPUS query.** Sources sweep by their own structure; users
  select over the corpus — V1 queried sources with the user's own words, capping recall at what the
  user thought to type. Keywords **are** fine in a DISCOVERY query, whose output is a lead
  (`docs/tasks/02`).
- **Retention and the candidate window are different windows**, and conflating them loses postings:
  `retrieval_horizon_days` is how long we keep a posting, `retrieve.NEW_ARRIVALS_HOURS` how far back
  a scheduled run looks, a posting getting one chance on the day it arrives — a run the *user*
  caused passes the whole horizon instead. **Age and arrival differ too**: staleness reads
  `COALESCE(posted_at, first_seen_at)`, the candidate window `first_seen_at`. **A scheduled run
  matches only once its sweep is prepared**, waiting only for work that could run now.

## Critical code patterns

- **Async everywhere in the pipeline**; `httpx.AsyncClient`, never `requests`, never block the loop.
  CPU work goes through `asyncio.to_thread`, and **batch everything** — a per-row `await` or
  mutation over a batch is a bug.
- **Pydantic models are the contract.** `CanonicalJob` and `JobFacets` are shared verbatim by
  adapters, the DB layer and the web views; never define a parallel dict shape for the same thing.
- **Adapters yield, they do not write**, which keeps them testable with no database, and
  **normalizers are pure** — no clock, no client, no connection — which is what lets a replay
  reproduce the original result.
- **Nothing downstream of normalisation may name a source.** If a change needs edits in the matcher
  or the web layer as well as a source, the abstraction has leaked. Enforced by a test.
- **Store UTC; convert at the edge, through `clock.py` and nowhere else.** `settings.timezone`
  applies only where a person is involved — a displayed instant, or an hour somebody *chose*, which
  is wall-clock and drifts with DST if resolved elsewhere. A stored `date` is not an instant.
- **Specific exceptions with complete-sentence messages**; never `raise Exception(...)` or
  `except Exception: pass`. Broad catches belong only where isolation is the point — per source, per
  user, the runner's tick loop — and each must log and record.

## Sources, access and the crawl set

**Recall comes first.** `robots.txt` and a site's terms do not rule out a source, and private or
undocumented endpoints are allowed. What rules a source out is that we cannot reach it without
getting blocked, or without a credential we are not entitled to. Every source-specific fact — page
caps, field names, silent-failure traps — is a comment in that source's `client.py` or
`normalize.py`: **read the adapter first, and re-probe a live API before changing a constant**,
since these fail silently rather than raising.

- **Don't get blocked** — a block loses the source for days, which costs more than it was worth. Go
  slow, back off at the first sign, and be polite **per _provider_, not per hostname** (~1 req/s via
  `sources/http.py`, keyed on the registrable domain), since many platforms give every tenant a
  subdomain. Record a host's `robots.txt` with its date as *block risk*, never as permission.
- **Never use a personal account**, take keys from the environment only, and **don't take a source
  we may not retain** — the archive is permanent, so one storable for days cannot work here.
- **Isolate failures**: one dead source must not abort a run, and a source failing wholesale is
  paused rather than charging each of its postings an attempt.

- **Completeness is not success.** `closable_scopes` names the scopes whose *entire* live set the
  sweep observed, per scope and never per source; a delta sweep returns none, closing on it would
  retire the whole corpus. Be **delta-first**, **parse defensively** (a missing optional field is
  `None`, one unreadable field must not discard the record), and **scope an external id by tenant**
  unless the platform documents its ids as globally unique.
- **The crawl set lives in the database** (`source_tenant`), not the repo, so **the corpus a commit
  produces is not reproducible from that commit alone**. Changing it is a CLI action: a board costs
  every user, an account only its credit. Keep configuration and observation in separate tables, and
  validate a scope at the write — a malformed slug 404s nightly behind a rising failure count.
- **Agents propose, humans merge**: an agent opens a PR or writes a row disabled, never enlarging
  the crawl or the politeness budget on its own.

## Privacy

The repo is public. Users' career data is not, and must never enter it.

- **Never commit** `.env`, database dumps, or anything holding a user's objectives, salary, watched
  employers, address or password. **Fixtures are hand-written** — never a real payload.
- **Profiles live in the database only**, edited through the web UI — there is deliberately no file
  path in or out (the tenant registry is the exception).
- **Secrets come from the environment only**: never read one from the repo, never log one, and show
  a generated password once in the response that created it rather than in a URL.

## LLM cost discipline

One installation key, credit per user in dollars, metered against the balance of whoever the call
was made for. Real spend is well under a cent a day, so this is about not being *surprising* rather
than about money. `match/rerank.py` and `match/pipeline.py` comment how the scorer paces.

- **Retrieval is free and must work with no key and no credit at all**, the deterministic query
  expansion being the floor rather than a degraded fallback.
- **Score a posting once per profile version, ever**, cached on `(content_hash, user,
  profile_version)`. **Only bump `profile.version` for fields that change what a good match is**
  (`SCORING_FIELDS`), and never re-stamp it from retrieval, or rows needing a re-score look current.
- **Check the limits before the calls go out**, from the database rather than a snapshot, since runs
  can overlap. Nobody is exempt, admins included, and scoring off means no paid call at all.
  **Keep the provider pinned and reasoning disabled** — both installation settings — or price and
  scores stop being reproducible and a reasoning model burns its budget on hidden thinking.

## Web UI

**The design system is the token block and component list at the top of `app.css`. Read that header
first.** A template uses those classes and nothing else: no colour, radius or spacing in a template,
and a new look is a new component there rather than a one-off. One word means one thing everywhere
(`good`/`warn`/`bad`, `high`/`mid`/`low`), and repeated markup goes through `_macros.html`.

- **One stylesheet, no build step, no Node, and nothing the browser loads comes from another
  origin** (tested): htmx and the font are vendored with their licences, so a fresh clone works
  offline and no CDN learns the reader's IP and page on every load of somebody's job search.
- **Everything htmx does must be visible when it fails** — a failed swap raises a toast, and a
  lapsed session leaves by `HX-Redirect` rather than a 303 it would paste into a button.
- **Set anything every page needs in the session middleware, not per route** — admin-only included,
  enforced by URL prefix. A per-handler check is the line somebody forgets on the next route.
- **`places.tsv.gz` is committed vocabulary**, so derivation stays pure; rebuilding it moves every
  facet and needs a `DERIVE_VERSION` bump.

### The reader's pages

- **`/recommendations` is retrieved and scored**, ordered by score. **`/search` shows every scraped
  posting** whatever the filters said — unretrieved, unscored and closed included — being the only
  view of what was actually collected, so keep score filters out of it.
- **No score threshold, no run statistics and no dashboard on a reader's page**: it shows what was
  paid for, ordered, and the reader draws their own line. Diagnostics belong on Operations.
- **An edition is stored, not derived, and never rewritten.** It records what the reader was
  *shown*, where `user_job_match` holds the current verdict — that duplication is the point. A closed
  posting stays, a dismissed one leaves, a profile change publishes a second edition for the day,
  and a posting reaches a user once per profile version. **Page by cursor, not OFFSET.**
- **A reported link is stored raw and answered once.** "Found it elsewhere" is the only measure of
  recall on a job somebody wanted, so the route stores the URL and the runner does the rest: it
  resolves, looks, and records ONE answer that is then never rewritten. Resolving in the route too
  would answer differently the day the rules change, and both answers would be on the page.
- **Location is the only hard filter**; everything else on Profile is a preference the reranker reads
  as text. Don't add a second — a filter hides a posting with no way for the reader to learn it
  existed, where a preference only moves it down the list. **Prefer losing precision to losing
  recall**: a posting we could not place passes, and the reranker recovers precision where nothing
  recovers one filtered out. See `_LOCATION` in `db/queries/match.py` and `ingest/derive.py`.

### Operations and accounts

- **Operations is one page, for an operator**, answering "is the corpus being collected properly".
  **Surface what fails silently**: partition overflow, sweep completeness, queue depth, the gap
  between stored and recommendable, more than one embedding version. **A run must be watchable while
  it runs**, or a slow source and a wedged one look identical, and **estimate only from measured
  history**. **Cancellation is a request, not a kill**: a sweep stops between sources, never inside.
- **The first account is the admin**, there being provably nobody else it could be — so a test
  fixture must set `is_admin` explicitly or it silently tests an admin's view of every page.
- **The email is the login; the display name is what the UI shows**, resolved once and stored. The
  address folds to lower case with the unique index on `lower(email)` — folding in the app alone
  lets a case-variant duplicate in. Check `is_active` per request, a token outliving a disabling.

## Database rules

- **Every schema change is an Alembic migration**, no manual DDL, and **SQL lives only in
  `db/queries/`** — a route, adapter or worker holding SQL is a bug. Both enforced by tests, as is
  `schema.py` and the migration chain agreeing.
- **Two identities, deliberately separate.** `job (source, external_id)` is *provenance* — the same
  row from the same board. `job.dedup_group` is *semantic* — the same job in the world — and is a
  **marker that must never merge or delete rows**. It keys on title and employer, not the place
  (aggregators blank it), and must never be loosened to a fuzzy match.
- **`job_embedding` holds open postings only**; closing a job deletes its row in the same statement,
  keeping the table proportional to the live corpus with no flag to drift.
- **The dense arm is exact, with no ANN index**, which would apply the filter after the graph walk.
  See `_DENSE_SQL`; revisit only at millions of vectors, measured.
- **Keep the two lexical paths in sync** — a `german` `tsvector` that stems and ranks, and a trigram
  index over an `unaccent`-folded column for substrings inside German compounds. **asyncpg uses
  `numeric_dollar` paramstyle, so `%` is NOT doubled**: `'%%'` silently breaks the trigram path.
- **Backfills are chunked, resumable and idempotent**, paging by keyset rather than `OFFSET`.

## Testing

A test earns its place only if it can fail for a reason a reviewer would care about. **Write tests
for** parsing, derivation, retrieval fusion, dedupe and upsert behaviour, query shape, cost controls,
and every silent-failure trap a comment records. **Not for** getters, pydantic, SQLAlchemy, or
mocks restating mocks.

- **No network anywhere, ever**: synthetic fixtures and a stubbed transport. Probe a live API with
  a throwaway shell command, never a test file.
- **`tests/unit/` needs no database**; `tests/integration/` is skipped unless
  `TROUVEUR_TEST_DATABASE_URL` is set. **Run integration before shipping a change to `db/queries/`,
  the migration, or a template** — SQL that compiles is not SQL that runs.
- **When you add a structural guard, check it can fail** by briefly introducing the violation.
- **A rendering test must render a job card** — a page test over an empty list proves only that the
  query returned. `StrictUndefined` is on, so a missing field raises rather than rendering blank.
- **`tests/unit/golden/` pins the whole output of normalisation and derivation per source**, a change
  to either moving every posting. Regenerate with `--update-goldens`, then **read the diff**.
- **`trouveur eval` is not a test**: numbers against a baseline, never a merge gate. Needles planted
  through the real ingest path make recall rigorous while **precision is not measurable** — an
  unplanted posting ranking highly is unjudged, not wrong. Personas are fictional (public repo); it
  needs a scratch database, the real model, and `TROUVEUR_EVAL_LLM_KEY`.

## Commands

```bash
uv sync                                   # deps; --extra embeddings, --extra archive for the rest
uv run pytest tests/unit -q               # the fast gate; `ruff check trouveur/` to lint
uv run alembic upgrade head               # apply migrations; `revision -m "add X"` for a new one

uv run trouveur sweep [--source greenhouse]   # fetch sources into the corpus
uv run trouveur drain                     # work the deferred queues once
uv run trouveur requeue|refill --kind X   # retry parked items; re-queue everything below a version
uv run trouveur match --user 1            # retrieve, cut, rerank for one user
uv run trouveur coverage [--json]         # per reader area: jobs per source, and what only it has
uv run trouveur tenants list|add|import   # the crawl set; `add` takes a slug or a careers URL
uv run trouveur tenants drop <src> <scope>   # tried and decided against; keeps the row, not swept
uv run trouveur create-user --email me@example.com   # the FIRST login becomes the admin
uv run trouveur serve|runner              # dev server on 127.0.0.1:8080; scheduler + workers
uv run trouveur eval --sweep|--rerank|--save-baseline   # needs TROUVEUR_EVAL_DATABASE_URL
uv run --with geonamescache==3.0.2 python tools/build_places.py   # rebuild the city list
```

Prefer the narrowest command that proves your change, and never sweep a live source to test a parser.
**Ask first** for anything you cannot take back: `sweep` and `eval --sweep` (live APIs), `match`
(credit), `refill --kind embed` (hours of CPU), `test-notify` (a real inbox), `runner`, `git push`.

## Commit style

Conventional commits, one scope per commit:

```
feat(sources): add the Personio adapter
fix(arbeitsagentur): stop sending veroeffentlichtseit=2, it returns the whole corpus
chore(deploy): pin the postgres image to pg16
```

Types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `build`. Describe the problem and the
evidence, not a diff summary. Never commit generated files or secrets. Two things are
deliberately committed and neither is generated by us: `places.tsv.gz`, which is vocabulary, and
`web/static/vendor/`, which is third-party code and fonts we serve from our own origin -- both
explained in Web UI, the second with its provenance and licences in `vendor/VENDOR.md`.
