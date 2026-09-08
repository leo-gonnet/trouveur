# Trouveur — a self-hosted job radar

A platform to find your next job. It sweeps job sources into a shared corpus, then ranks that
corpus against each user's profile and surfaces what is worth reading in a web UI and a digest.

Engineering rules and the source-by-source API traps live in [AGENTS.md](./AGENTS.md).

## How it works

Ingestion does not know about users, and selection does not fetch. That split is the whole design:
searching sources with a user's keywords caps recall at whatever they thought to type.

```
INGEST  (shared corpus, runs once for everyone)
  sweep ──> raw archive ──> job ──> facets ──> vector ──> lifecycle

MATCH   (per user, cheap, re-runnable)
  profile ──> expanded queries ──> hard filters
          ──> dense ANN ─┐
          ──> BM25/FTS  ─┴─ RRF fusion ──> rules cut ──> LLM rerank ──> digest
```

Raw payloads are archived verbatim and kept, and everything derived from them carries a version.
Fixing a parser is therefore a re-derive, not a re-crawl: bump the version, refill the queue, and
the ordinary worker brings the whole corpus up to date.

**Reranking runs on each user's own OpenRouter key**, entered in Settings, encrypted at rest and
capped by a monthly ceiling that is checked before each batch. The deployment itself has no LLM
spend. Everything except reranking — sweeping, retrieval, hybrid search, the web UI — works with
no API key at all.

## Quick start (local)

```bash
uv sync
docker run -d --name trouveur-db -p 5432:5432 \
  -e POSTGRES_USER=trouveur -e POSTGRES_PASSWORD=dev -e POSTGRES_DB=trouveur \
  pgvector/pgvector:pg16
export DATABASE_URL=postgresql+asyncpg://trouveur:dev@127.0.0.1:5432/trouveur
export SESSION_SECRET=dev-only-insecure-secret
export ENCRYPTION_KEY=dev-only-change-me
uv run alembic upgrade head
uv run trouveur create-user
uv run trouveur sweep --source greenhouse   # needs a board added in the UI first
uv run trouveur drain                       # derive, embed, fetch details
uv run trouveur match --user 1
```

The test suite is offline and runs in well under a second. A second, opt-in suite exercises every
SQL path against a real Postgres and is skipped unless `TROUVEUR_TEST_DATABASE_URL` is set — worth
running before any change to the queries or the migration, because SQL that compiles is not SQL
that runs.

`uv` is required: this project's target host has no working `venv` module, and CI uses `uv` too.
`create-user` is the only way to make a login — there is no sign-up route. Then
`uv run trouveur serve` for the UI and `uv run trouveur runner` if you want scans to fire on a
schedule and the queues to drain continuously.

Semantic retrieval needs the local embedding model: `uv sync --extra embeddings`. Without it, set
`EMBEDDING_PROVIDER=deterministic` to exercise the pipeline (its vectors carry no meaning, and rows
it writes are stamped so its use is visible in the data).

Profiles, API keys and the list of companies to watch live in the database and are edited in the
UI. There is no config file for any of them.

If a sweep collects nothing, the reason is recorded per source in `source_sweep` rather than
raised — including partition overflow, which is coverage lost with no error anywhere.

## Sources

Each source is two modules: `client.py` does network and nothing else, `normalize.py` is a pure
versioned function from an archived payload to a canonical job. Adding a source is a package plus
one registry entry — if it needs edits anywhere else, the abstraction has leaked.

| Source | Coverage | Mechanism | Can retire postings? |
|---|---|---|---|
| Arbeitsagentur | Germany (~1.0M live, ~36k/day) | Public JSON API, no signup. Swept by occupational field, not keywords. | No — delta only |
| Greenhouse | EU-wide, per company | Public board API, no auth. One request returns a tenant's complete board. | Yes, per board |

The pair is deliberately mismatched: one is a partitioned delta search with a separate detail
phase, the other a complete per-tenant dump with descriptions inline. An abstraction that survives
both will survive the next ten.

## Deploy

Docker Compose: Postgres, the web UI behind Caddy for TLS, and a `runner` service that owns the
scan schedule and is the only process that executes a scan. Push to `main` runs the tests, builds
the image and deploys over SSH.

Copy `compose.yaml`, `Caddyfile`, `backup.sh` and `.env.example` (as `.env`, `chmod 600`) to the
host, fill in `.env`, then:

```bash
docker compose run --rm migrate   # web and runner assume the tables already exist
docker compose up -d
docker compose run --rm web trouveur create-user
```

Five things that are not guessable:

- **Point DNS at the host before the first start.** Caddy cannot obtain a certificate otherwise.
- **`ENCRYPTION_KEY` must be set and must never change.** It encrypts users' stored API keys;
  losing it means every user has to re-enter theirs.
- **`TROUVEUR_IMAGE` must match the image CI pushes**, which is `ghcr.io/<owner>/<repo>`. Make
  that package public, or `docker login ghcr.io` on the host with a `read:packages` token.
- **`docker compose run --rm migrate` on every deploy**, before the new containers start.
- **Rollback is `TROUVEUR_TAG`**: set it to an older commit SHA in `.env` and `up -d` again. The
  deploy writes the deployed SHA there.

`deploy.yml` needs three repository secrets: `SSH_HOST`, `SSH_USER`, and `SSH_KEY` (the private
half of a keypair whose public half is in that account's `authorized_keys`). Pushing the image
uses the built-in `GITHUB_TOKEN`.

`backup.sh` dumps the database out of the db container; cron it on the host.

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
