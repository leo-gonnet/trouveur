# Trouveur - A simple job radar

A platform to find your next job. It collects postings from a set of job sources, filters
them against a profile you control, and surfaces what is worth reading in a web UI and a
daily digest.

Engineering rules and the source-by-source API traps live in [AGENTS.md](./AGENTS.md).

## Quick start (local)

```bash
uv sync
docker run -d --name trouveur-db -p 5432:5432 \
  -e POSTGRES_USER=trouveur -e POSTGRES_PASSWORD=dev -e POSTGRES_DB=trouveur \
  pgvector/pgvector:pg16
export DATABASE_URL=postgresql+asyncpg://trouveur:dev@127.0.0.1:5432/trouveur
export SESSION_SECRET=dev-only-insecure-secret
uv run alembic upgrade head
uv run trouveur create-user
uv run trouveur run --dry-run
```

`uv` is required: this project's target host has no working `venv` module, and CI uses `uv` too.
`create-user` is the only way to make a login — there is no sign-up route. Then
`uv run trouveur serve` for the UI and `uv run trouveur runner` if you want scans to actually
fire on a schedule.

The profile and the list of companies to watch live in the database and are edited in the UI.
There is no config file for either.

If a run collects nothing, the reason is recorded per source in the `source_state` table rather
than raised.

## Sources

Each source is a self-contained adapter. Which ones run is a setting, not a code change, and the
list is meant to grow over time as new adapters are added as modules.

| Source | Coverage | Mechanism |
|---|---|---|
| Arbeitsagentur | Germany | Public JSON API, no signup |
| karriere.at | Austria | Sitemap diff + schema.org JSON-LD |
| Personio | DACH Mittelstand | Public per-tenant XML feed |
| JobSpy | LinkedIn/Indeed/Glassdoor/Google | Library (optional extra) |

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

Four things that are not guessable:

- **Point DNS at the host before the first start.** Caddy cannot obtain a certificate otherwise.
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
