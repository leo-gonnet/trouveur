<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/logo/trouveur-dark.png">
  <img src="assets/logo/trouveur-light.png" alt="Trouveur" width="150">
</picture>

# Trouveur

**A job radar.** It collects the job market into one corpus every day, then ranks that corpus against one profile. More than a million postings archived so far.

</div>

## Why

Job platforms rank on the words you typed. They return what you thought to ask for, and nothing
else.

One word is already a filter. *Software engineer* and *software developer* give two different
lists on the same site. So you run both, every day, and still miss the third phrasing. Each
platform covers its own slice, so you log into several. Some offer no posted-date filter at all: a
month-old advert sits above one published ten minutes ago.

Trouveur inverts that. **Collection knows nothing about you; selection happens afterwards.**
Sources are swept by their own structure, with nobody's keywords involved — boards of companies, national employment agencies, aggregator feeds. Ranking runs
over everything collected. A role you would have wanted but never thought to name still arrives.

**Recall is the metric.** I have not found another job recommendation tool that treats it as the
headline number rather than as a by-product.

The product itself is deliberately boring. No CV optimizations. No filters to tune. No dashboard to
configure. One list, ordered, once a day. The engineering is all underneath it.

## How it works

```
INGEST  (shared corpus, runs once for everyone)
  sweep ──> raw archive ──> job ──> facets ──> vector ──> lifecycle

MATCH   (per user, cheap, re-runnable)
  profile ──> expanded queries ──> hard filters
          ──> dense ANN ─┐
          ──> BM25/FTS  ─┴─ RRF fusion ──> LLM rerank ──> digest
```

**Everything derived is a versioned pure function of the archive.** Raw payloads are kept forever.
Fixing a parser is a re-derive, not a re-crawl: bump its version, and the ordinary daily worker
recomputes every row below it. Upgrade and backfill are one code path, so the repair path is
exercised every day.

**Retrieval is hybrid.** An embedding model reads meaning. Postgres full-text and a
trigram index read words, because German compounds mean `ingenieur` has to find
`Wirtschaftsingenieur`. The two rankings are fused by RRF. Only the last seven days are indexed, so
the index tracks the live market rather than all history.

**The final ranking is an LLM, on your own key.** A posting is scored once per profile and cached
for ever after, which puts a day's run under a cent. Everything before that stage works with
no key at all.

**A new source is one package and one registry entry.** Two modules: one does network and nothing
else, the other is a pure versioned function from raw payload to canonical job. Nothing downstream
may name a source, and a test enforces it.

## Evaluation

Retrieval quality is measured rather than asserted. `trouveur eval` plants hand-written needles in
a haystack of real postings, so their relevance is known by construction and no corpus has to be
labelled. Recall is then a number, and the design decisions behind the pipeline were settled
against it — several of them would have been guessed wrong.

## Running it

Python 3.12, FastAPI + Jinja2 + HTMX, PostgreSQL 16 + pgvector, SQLAlchemy Core + asyncpg. No build
step, no Node, no JavaScript framework. `uv` is required.

```bash
uv sync --extra embeddings
docker run -d --name trouveur-db -p 5432:5432 \
  -e POSTGRES_USER=trouveur -e POSTGRES_PASSWORD=dev -e POSTGRES_DB=trouveur \
  pgvector/pgvector:pg16
export DATABASE_URL=postgresql+asyncpg://trouveur:dev@127.0.0.1:5432/trouveur
uv run alembic upgrade head
uv run trouveur create-user                    # the only way to make a login
uv run trouveur tenants add <source> <company> # the crawl set is a table, managed from the CLI
uv run trouveur sweep
uv run trouveur drain                          # derive, embed, fetch details
uv run trouveur serve
```

`uv run pytest` runs 247 offline unit tests in about a second. An opt-in suite runs every SQL path
against a real Postgres, because SQL that compiles is not SQL that runs.

Deployment is Docker Compose: Postgres, the UI behind Caddy for TLS, and a runner that owns the
schedule and is the only process that scans. A push to `main` tests, builds and deploys over SSH.
`ENCRYPTION_KEY` must never change once set, as it encrypts users' stored API keys.

## Disclaimer

**Trouveur is provided for educational purposes and personal, non-commercial use only.** It is a
learning project, not a product.

- **You are responsible for how you use it.** Before enabling any source, check that site's
  `robots.txt` and Terms of Service, and only scrape what they permit. If a site's terms forbid
  automated access, do not enable a source for it.
- **Respect rate limits and be polite.** The defaults apply a per-host delay and identify the
  client via a `User-Agent`. Do not remove or shorten these. Do not run the pipeline in tight
  loops against live sources.
- **Fetched content belongs to its publishers.** Job listings, company data and page HTML are
  third-party content. Trouveur stores it locally for your own filtering; redistributing it may
  infringe copyright or database rights.
- **No warranty.** The software is provided "as is". The author accepts no liability for how it is
  used or for any consequences of using it, including any claims by the sites it accesses.
- Use of this software is governed by the [LICENSE](./LICENSE); using it does not grant you any
  rights to the data it retrieves.

By running Trouveur you accept that compliance with applicable laws and third-party terms is your
responsibility, not the author's.

## License

[PolyForm Noncommercial License 1.0.0](./LICENSE). The source is available for personal and other
non-commercial use. All commercial rights are reserved by the author — contact the author if you
need a commercial license.
