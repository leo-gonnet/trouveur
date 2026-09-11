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
  The corollary is that a *missing* facet is equally silent, so anything that derives to nothing
  needs a vocabulary wide enough to cover how sources actually spell things -- `OESTERREICH` cost
  4.3% of a live sample its country before the evaluation caught it.
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
| `trouveur/sources/board.py` | Shared sweep for sources that return a tenant's complete board. |
| `trouveur/sources/feed.py` | Shared sweep for global, newest-first, paged corpora. Delta-first. |
| `trouveur/sources/parse.py` | Pure decoding shared by normalisers: timestamps, HTML to text. |
| `trouveur/sources/scopes.py` | Tenant-identifier grammars. Each source names the one it uses. |
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
| `trouveur/eval/` | Retrieval evaluation: personas, planted needles, scorecard. |

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
- **There is no index of tenants anywhere.** The `source_tenant` table is the only list of boards
  that exists. An unknown slug returns 404. Verify a slug with one request before adding it, or it
  fails every day until someone reads the failure count.
- Scope external ids by slug (`gitlab:8503792002`). Nothing documents Greenhouse ids as globally
  unique, and a collision would silently merge two unrelated postings onto one row.

### The board family (`sources/board.py`)

Ashby, Lever, Breezy, Rippling, Greenhouse and Personio all publish a tenant's **complete** board
in one request, and differ only in the URL and the response shape. They share `sweep_boards`; a new
one of this kind is a URL template, an extractor and a normaliser. What makes them one family is
exactly what `closable_scopes` depends on — the response *is* the live set. A source that pages,
filters or windows its results is **not** in this family and must not be forced into it.

Verified live on 2026-09-09. Each of these fails silently:

- **Lever, Breezy and Rippling return a bare top-level array**, not an object with a `jobs` key.
- **Lever names the title `text`.** There is no `title` key; reading one drops the whole board.
- **Lever's `createdAt` is epoch milliseconds**, where Arbeitnow's `created_at` is seconds. Read as
  the wrong unit a posting lands in 1970 or in the year 58 000 — use `sources/parse.py`.
- **Ashby needs `?includeCompensation=true`**, or salary arrives only as a rendered string
  (`"$211.4K – $290.6K • Offers Equity"`) that cannot be turned back into numbers.
- **Ashby titles carry leading whitespace** on live boards, and the title is an identity input.
- **An Ashby board name may be a domain** -- `mistral.ai` and `roadsurfer.com` are live boards. The
  default slug grammar rejects a dot, so Ashby uses `scopes.dotted_slug_scope`; the permission is
  deliberately not global, because for every other source a dotted slug is a pasted homepage.
- **Breezy's `country` and `state` are objects, not strings.** Read as strings they put a dict repr
  in the country column, which matches no vocabulary entry at all.
- **Rippling's listing has no description, company or date** — it is the one board source that
  genuinely needs a detail fetch. Its `description` is an object keyed by section, not a string.

### Personio (`sources/personio/`)

`robots.txt` (`jobs.personio.de`, checked 2026-09-09): empty, so no restriction is expressed. The
XML feed is Personio's own syndication endpoint. Boards are per-tenant subdomains.

- **An unknown tenant returns HTTP 429 with a Vercel "Security Checkpoint" HTML page, not 404.** A
  valid board returns 200 even when polled fast, so 429 here is a bad slug, not throttling.
  `PoliteClient` retries 429 by design, so without a content-type check a typo'd slug burns four
  attempts and a backoff every sweep and reports itself as rate limiting for ever.
- **The feed states no posting URL** — no href, link or url element exists. The canonical URL is
  built from the tenant and the id, which is why the tenant must survive in the external id.
- XML is decoded to a dict in the client, exactly as the Greenhouse client calls `response.json()`:
  that is transport decoding. Reading its *fields* stays in the versioned normaliser.

### Workday (`sources/workday/`)

`robots.txt` is **checked per tenant host, not per platform** — boards are tenant-hosted and their
rules differ. A representative host on 2026-09-09 disallowed only `/talentcommunity/` and
`/refreshFacet/`. The sibling SuccessFactors platform serves `Disallow: /` on one tenant host and
nothing on another, so a platform-wide verdict is not sound for this family.

- **`limit` caps at exactly 20.** `limit=21`, `50` and `100` all return HTTP 400.
- **`total` is unusable as a count or a terminator.** One query reported `total=2000` at offset 0,
  `total=0` at offset 1980 and `total=2000` at offset 2000, while still returning rows past its own
  stated total. The only reliable end of the walk is an empty `jobPostings` array.
- **`postedOn` is relative prose** (`"Posted Today"`), not a date. Never parse it; the detail's
  `startDate` is the only absolute date either payload states.
- **`locationsText` is a count** (`"3 Locations"`), not a place. Used as a location it fills the
  city column with "3 Locations" on every multi-site posting.
- Search is a POST, and a board is three facts (`tenant:wdN:SiteName`) in which case is
  load-bearing — the API 404s on a lowercased site name.

### The global feeds (`sources/feed.py`)

Workable's public board, Arbeitnow, Himalayas and Jobicy each serve one corpus, newest first, with
no tenant. They are far too large to re-fetch daily — Workable alone is ~170 000 postings at a
fixed 20 a page — so the ordinary run is a **delta** that stops once postings fall outside the
window, and therefore **closes nothing**. Only a backfill that pages to the end may close.

- **Workable's page size is fixed at 20 and cannot be raised.** `limit=100` returns HTTP 200 with
  no `jobs` key and no cursor — an empty result indistinguishable from the end of the corpus. Send
  no page-size parameter at all. Unknown query parameters are silently ignored, so a filter that
  looks like it applied may not have.
- **Workable states the location already structured** (`{city, subregion, countryName}`); do not
  re-parse the rendered string.
- **Himalayas has no `id` field** — `guid` is the identity. Its `locationRestrictions` say where a
  candidate must be, not where an office is.
- **Jobicy prefixes every field** (`jobTitle`, not `title`) and serves one capped page with no
  cursor, so it passes `complete_on_backfill=False`: treating that page as the corpus would retire
  every other Jobicy posting we hold.
- A source that names its work location in words (`"Anywhere"`, `"Worldwide"`) needs those terms in
  `vocab.REMOTE_TERMS`, or they are read as a city.

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
- **Be polite, per _provider_ rather than per hostname.** At most ~1 request/second, via
  `sources/http.py`. The budget is keyed on the registrable domain because Personio, Breezy,
  Teamtailor and Workday give every tenant its own subdomain: keyed on the hostname, 1 258 Personio
  boards would be 1 258 independent budgets — up to 1 258 requests a second at one provider, with
  every counter still reading as compliant. Over-grouping is the safe direction to be wrong in.
- **Robots is checked per tenant host, not per platform**, for any source whose boards are
  tenant-hosted, and the finding is recorded with its date.
- **A host that serves no `robots.txt` expresses no restriction.** Record that as the finding;
  silence is neither permission nor refusal, and it is not the same as an `Allow`.
- **Never reach a source through a reverse-engineered private endpoint or a hardcoded credential**,
  however freely other projects do it.
- **Never accept a source whose terms forbid retention.** The raw archive is kept forever, so a
  source permitting only 14 days of storage is incompatible by construction, not merely awkward.
- **Parse defensively.** A missing optional field is `None`, not a `KeyError`. A field you cannot
  parse must not discard the whole record.

## The crawl set, and why it is two tables

A per-tenant source needs a list of tenants, because some publish no index of their own. That list
lives in the database (`source_tenant`), not in the repository, because it is written by more than
one thing: an operator through the CLI today, a discovery pass later. A discovery pass proposing
hundreds of candidates does not belong in a hand-edited file.

The cost is real and was accepted deliberately: **the corpus a given commit produces is not
reproducible from that commit alone.** Two installations on the same code crawl different companies.

**Configuration and observation stay in separate tables.** `source_tenant` is the crawl set;
`source_scope_health` is what happened when we asked. This is load-bearing and was learned the hard
way — the table these replaced mixed an editable board list with health columns that nothing ever
wrote, so half of it was permanently dead and the UI reported "last success: —" indefinitely, which
reads as "not run yet" rather than "never recorded". If two kinds of writer own two halves of a
table, one half will rot unnoticed.

Rules that follow:

- **Managing the crawl set is a CLI action, not a web one** (`trouveur tenants …`). It is shared by
  every user, so enlarging it is an operator decision — exposing it per user means one person
  adding five hundred boards and everyone paying the crawl cost. The dashboard shows it read-only.
- **A discovery pass inserts `enabled = false`, `origin = 'discovered'`.** It proposes; a human
  promotes. Discovery must never be able to enlarge the crawl, the bill or the politeness budget on
  its own.
- **`tenants.local.toml` is a preload, not the crawl set** (`sources/seed.py`,
  `trouveur tenants import`). A stopgap until discovery exists, because typing a board list one
  `tenants add` at a time does not survive a database reset. It only ever **inserts**: deleting a
  line removes nothing, and a board an operator disabled stays disabled across a re-import. It is
  gitignored — committing one installation's board list would imply the corpus is reproducible
  from the commit, and would make every installation crawl the same companies. `.example` is the
  committed template, and a test pins that it parses and names only tenant-scoped sources.
- **Validate a scope at the write** (`registry.clean_scope`, grammars in `sources/scopes.py`). One
  grammar does not fit every source: a Greenhouse board is a path segment, a Workday board is
  `tenant:wdN:SiteName` with load-bearing capitals. A malformed slug that reaches the table
  404s on every sweep afterwards and surfaces only as a slowly growing failure count.
- **A tenant-scoped source with no enabled tenants is dropped from the run**, not swept. Sweeping
  it would make no requests, find nothing, and report a perfectly healthy empty sweep — which is
  indistinguishable from a source that is working and finding nothing.

## DON'T: robots.txt policy

**Check `robots.txt` before adding any source, and record the finding in the adapter docstring
with the date.**

- **Never scrape willhaben.at.** Its robots.txt states automated access is *"expressively
  forbidden"* and disallows `/jobs/webapi/`, `/rest/`, `/restapi/`.
- **Never scrape StepStone search-result pages.** `stepstone.at` disallows `/5/ergebnisliste.html`
  and `/?*`.
- Do not "temporarily" add a disallowed source to test something.

These four were surveyed on 2026-09-09 and are **refused**. They are listed because widely-copied
open-source job scrapers use all of them, so without a record someone re-adds one citing those
projects as precedent:

| Source | Evidence, verbatim |
|---|---|
| **AMS Austria** | `jobs.ams.at`: `User-agent: *` gets `Allow: /public/emps/$` then `Disallow: /public/emps/` — the exact path only, nothing beneath it — while `LinkedInBot` gets a blanket exemption above it. `jobroom.ams.or.at` is `Disallow: /`. The AMS HR-API is a write-only channel for employers. Austria's national job service is not crawlable, and is **not** an equivalent of the German Arbeitsagentur. |
| **SmartRecruiters** | `api.smartrecruiters.com`: `User-agent: LinkedInBot / Allow: /v1/companies/` then `User-agent: * / Disallow: /`. The posting API is functionally public but explicitly reserved to LinkedIn's crawler. |
| **Recruitee** | `api.recruitee.com`: `User-agent: * / Disallow: /`. |
| **Remotive** | `remotive.com`: `Disallow: /api/*` — its own documented public API is robots-disallowed. |

Also refused, for reasons other than robots: **Adzuna** (terms bar storage beyond a 14-day
evaluation, incompatible with a permanent archive), **Jooble** (500 requests *lifetime*),
**monster.at** (bot-walled in practice), and **LinkedIn, Indeed, ZipRecruiter and Glassdoor**,
which are reachable only through reverse-engineered private endpoints with hardcoded credentials.

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
  volume setting such as `rerank_limit` must not invalidate a cache and bill a re-score.
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
  `/recommendations` is retrieved AND `rule_verdict='pass'` AND scored, ordered by `llm_score`
  descending. `/search` shows **every** scraped posting regardless of filter outcome, including
  rejected, unscored and closed ones — that is the only view of what was actually collected.
  Adding a verdict or score filter to `search_jobs` defeats its purpose; keep the distinction in
  the query layer, not just the UI.
- **There is no score threshold, and re-adding one is a regression.** It hid postings the user had
  already paid to have scored, behind a number they had to guess — and guessing it low enough to
  see them made it meaningless. Volume is bounded once, by `rerank_limit`, which is also the only
  setting that costs money. The page shows what was paid for and the reader draws their own line,
  so **the ordering is the product**: `ORDER BY m.llm_score DESC` is load-bearing, not cosmetic.
  The digest is bounded the same way, by count rather than by score.
- **The score is the first thing on a row and it is coloured** (`score_pill`, `.score.high/.mid/
  .low`). The band names in the macro and in `app.css` must match: they did not, and every score
  of 65 and over rendered with no colour at all for as long as that went unnoticed.
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

### Layout: split by whether a database is needed

That split is the one with operational meaning — it decides what can gate a merge without a
service container.

| Path | Needs Postgres | Runs |
|---|---|---|
| `tests/unit/` | no | every push, under a second |
| `tests/integration/` | yes | every push in CI, via a `pgvector` service |

`tests/integration/` is skipped at collection unless `TROUVEUR_TEST_DATABASE_URL` is set, so a
plain `uv run pytest` stays offline and instant.

- **No network anywhere, ever.** Use synthetic fixtures and a stubbed transport. To check live API
  behaviour while debugging, use a throwaway shell command, never a test file — and if what you
  learn is durable, write it into this file.
- **Run the integration suite before shipping any change to `db/queries/`, the migration, or a
  template.** **SQL that compiles is not SQL that runs**, and a template only runs when rendered:

  ```bash
  podman run -d --rm --name pg -e POSTGRES_USER=trouveur -e POSTGRES_PASSWORD=x \
      -e POSTGRES_DB=trouveur -p 55432:5432 pgvector/pgvector:pg16
  export TROUVEUR_TEST_DATABASE_URL=postgresql+asyncpg://trouveur:x@127.0.0.1:55432/trouveur
  DATABASE_URL=$TROUVEUR_TEST_DATABASE_URL uv run alembic upgrade head
  uv run pytest
  ```

### Golden files

`tests/unit/golden/` pins the **entire** output of normalisation and derivation per source. The
core of this system is two pure functions, and a change to either moves every posting in the
corpus, so example-based tests covering the fields somebody thought of are not enough.

```bash
uv run pytest tests/unit/test_golden.py --update-goldens   # then READ the diff
```

Never regenerate to turn a red test green without reading what moved. Each case records the
versions it was generated at: a facet diff with an unchanged `derive_version` means production
still holds the old readings and no re-derive has been scheduled.

### Two mechanisms that make the above work

- **`StrictUndefined` in Jinja.** The default renders an unknown attribute as an empty string, so
  a template reading a field its query does not select produces a blank cell and a green suite.
  Turning it on immediately found two: the job-card macro read `llm_reason` that `search_jobs`
  never selected, and `closed_at` that `recommendations` never selected.
- **Rendering tests must render a job card.** A page test over an empty list proves the query
  returned and nothing else. The `seeded` fixture creates one scored, rule-passed match so the
  card template is actually executed.
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
  - **Transliterated German country names resolve** (`OESTERREICH`, not only `Österreich`). The
    API transliterates umlauts; a vocabulary keyed only on the umlauted form silently gave every
    Austrian posting no country at all.
  - **A staffing agency is rejected by the source's structured flag**, not by finding "Zeitarbeit"
    in prose.
  - **The politeness budget is shared across a provider's tenant subdomains**, not one per host.
  - **An unknown Personio tenant is reported as a bad slug**, not retried as a 429.
  - **Workday never requests more than 20 a page**, terminates on an empty page rather than on
    `total`, never parses the relative `postedOn`, and never treats `locationsText` as a place.
  - **Workable is never sent a page-size parameter**, since an unsupported one returns an empty
    body that reads as the end of the corpus.
  - **A delta sweep reports no closable scope**, and a capped single-page feed reports none even on
    a backfill.
  - **A bare top-level array board is read as the list itself** (Lever, Breezy, Rippling), and an
    envelope appearing later shows up as an empty sweep rather than a TypeError.
  - **An Ashby board may be named after a domain** (`mistral.ai`, `roadsurfer.com`), and the same
    string is still rejected for every other source, where it means a pasted homepage.
  - Every source package is registered; `schema.py` and the migration agree; no SQL outside
    `db/queries/`; no source name used as a value downstream.
  - **Every route rejects an anonymous caller** (`tests/unit/test_web_auth.py`, parametrized over
    the router, so a new route is covered the moment it exists).
  - **Every query in `db/queries/` executes** against a real server, and adding one without an
    execution entry fails.
  - **Every Postgres enum has a Python counterpart and the members match** — Python ↔ `schema.py`
    offline, `schema.py` ↔ server in integration.
  - A stored API key never appears in a rendered page.

## Retrieval evaluation

`trouveur eval` measures whether retrieval finds what it should. It is **not a test**: it produces
numbers to compare against a baseline, and it never gates a merge.

**Planted known items.** Labelling a corpus is the expensive part of evaluating search, so this
does the opposite: a haystack of real postings that nobody labels, into which ~27 hand-written
needles are planted whose relevance is known by construction.

**What it can and cannot measure.** Recall is rigorous — a needle either came back or it did not.
**Precision is not measurable**, because the haystack is real and an unplanted posting ranking
highly is unjudged, not wrong. Reporting a precision number here would be inventing one. Planted
negatives give the usable substitute: postings the deterministic pipeline must exclude, so "did
anything that should have been filtered survive" is answerable without judging the haystack.

**Score the rules cut on both sides, not just on negatives.** The cut is what stands between
retrieval and the user, so a positive that is retrieved and then rejected by it is never seen —
and recall at any depth still counts it as found. `cut_by_rules` reports those. Grading only the
negatives measured half the pipeline and called a silent recall loss a success.

**Record each needle's rank, not only whether it was found.** `found/total` at one depth
saturates: at 29 of 30 needles found there is no headroom left and the number can only ever
report a regression, while a needle sliding from rank 12 to rank 90 — a real loss, since the
reranker's budget is far smaller than `k` — is invisible to it. `ranks` is what makes an
improvement visible at all, and `regressions()` reports a slide past `RANK_SLIDE`.

**Needles are tiered by which retriever should find them**, because one aggregate number cannot
answer the question worth asking — whether the hybrid earns its cost:

| Tier | Shape | Should be found by |
|---|---|---|
| `T1` | shares the persona's vocabulary | lexical alone |
| `T2` | same role, **no shared vocabulary** | dense only |
| `T3` | adjacent role, different title, sometimes another language | dense + query expansion |
| `N` | must be excluded by rules or hard filters | nothing — it should never survive |

The harness scores with the **deterministic** expansion only, so a run costs nothing and does not
vary with a model. T3 is therefore currently measured without the LLM expansion its row names:
what expansion adds is not yet a number this produces.

**A T2 or T3 needle that shares a content word with its persona is worthless** — it silently
becomes a T1 and the tier stops measuring dense recall. Four of twelve leaked a word on the first
draft (`startup`, `maintenance`, `Auswertung`), so `tests/unit/test_eval_needles.py` enforces it.

Other rules the harness depends on:

- **Personas are fictional.** They must never be a real user's profile — this repo is public.
- **Needles are planted through the real ingest path**, not inserted into `job`. A needle that
  skipped normalisation and derivation would be a row the pipeline could never have produced.
- **The haystack is every configured source**, because the objective is recall over the whole
  corpus and a haystack drawn from one adapter measures that adapter. Arbeitsagentur is the single
  exception: alone it contributes tens of thousands of postings a day and would drown the other
  eleven, so it is narrowed to `HAYSTACK_PARTITIONS`.
- **The haystack is real postings from occupational fields the personas plausibly compete in.**
  Filling it with retail vacancies would let the hard filters remove most of it for free and
  flatter every number.
- **Drain details before scoring.** The needles carry descriptions; a haystack of title-only
  postings is not the corpus production has, and the comparison would be between unlike things.
  The sharper reason: `persist` does not queue a posting for embedding until its description
  arrives, so postings with an outstanding detail fetch are **absent from the ANN index**, not
  merely thin — the dense arm then competes against a fraction of what the lexical arm sees and
  its recall is flattered by that gap. Only Arbeitsagentur, Workday and Rippling have a detail
  phase, and for those a description costs one polite request per posting — so the drain is capped
  at `DETAIL_BUDGET` and what it does not reach is reported as the scorecard's description
  coverage rather than hidden.
- **Run it with the real embedding model.** `EMBEDDING_PROVIDER=deterministic` makes every dense
  number noise, so the harness warns rather than letting you read it as a result.
- **`TROUVEUR_EVAL_DATABASE_URL` is required and must be a scratch database.** The harness plants
  fake postings and overwrites personas' profiles.

### Scoring the reranker

`--rerank` carries the planted-known-item idea one stage further: the needles are the only judged
items in the corpus, so they -- and **only** they -- are sent to the real reranker. Scoring the
whole retrieved shortlist would spend real money to produce numbers nobody can mark, for the same
reason precision is not measurable at retrieval.

- **Grade the order, not a cut-off.** There is no threshold to clear: the page shows everything
  scored, sorted by score, so the question is whether a planted negative outranks a planted
  positive — a bad posting the reader meets first. `inversions` counts those pairs and `margin`
  is the distance between the worst positive and the best negative.
- **The key comes from `TROUVEUR_EVAL_LLM_KEY`, and is never stored.** Users' keys live in the
  database and are entered through the web UI; this harness must not become a second way in.
- It reuses `rerank.score_batch`, so the prompt, model and provider pin are the ones production
  sends. A copy of the prompt here would grade something no user ever runs.
- **Rerank numbers are noisier than retrieval numbers.** Measured 2026-09-11 at `temperature=0`
  with the provider pinned, one needle scored 45 on one run and above 70 on the next. Treat a
  single run's `lost` list as a signal, not a result, and re-run before acting on a small change.

```bash
export TROUVEUR_EVAL_DATABASE_URL=postgresql+asyncpg://trouveur:x@127.0.0.1:55433/trouveur
DATABASE_URL=$TROUVEUR_EVAL_DATABASE_URL uv run alembic upgrade head
uv run trouveur eval --sweep                 # fetch a haystack, then score
uv run trouveur eval --rerank                # also score the needles with the real reranker
uv run trouveur eval --save-baseline         # record the result for future comparison
```

## Commands

```bash
uv sync                                   # install/refresh dependencies
uv sync --extra embeddings                # add the local ONNX embedding provider
uv run pytest                             # unit only, unless TROUVEUR_TEST_DATABASE_URL is set
uv run pytest tests/unit -q               # the fast gate
uv run pytest tests/unit/test_golden.py --update-goldens   # regenerate, then read the diff
uv run ruff check trouveur/               # lint
uv run alembic upgrade head               # apply migrations
uv run alembic revision -m "add X"        # new migration

uv run trouveur sweep                     # fetch sources into the corpus
uv run trouveur sweep --source greenhouse # one source
uv run trouveur drain                     # work the deferred queues once
uv run trouveur refill --kind derive      # re-queue everything below the current version
uv run trouveur match --user 1            # retrieve, cut, rerank for one user
uv run trouveur tenants list             # the crawl set, with per-tenant health
uv run trouveur tenants add greenhouse n26   # accepts a slug or a full careers URL
uv run trouveur tenants import --dry-run  # preload from tenants.local.toml (gitignored)
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
| `trouveur eval --sweep` | hits live APIs to build a haystack |
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
