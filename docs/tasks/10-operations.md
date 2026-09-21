# 10 — Operational hardening

## The problem

Most of what the 2026-09 study found took hours to discover because nothing reports it. The
system runs unattended on a single host, and its failure modes are quiet: a queue that stops
draining, a source whose scopes have all gone stale, an upstream model provider returning errors,
a vector index that is half a corpus behind.

Three concrete instances found so far:

- The production database container has Docker's default **64MB of `/dev/shm`**. A parallel HNSW
  index build puts its working memory there, so raising `maintenance_work_mem` appropriately for
  200k vectors fails with a message that reads like a full disk. Fixed on branch
  `feat/retrieval-eval`; confirm the running container picks it up.
- The LLM transport pinned a single provider with fallbacks disabled, so one upstream outage
  took the whole paid stage down and reported it to users as a problem with *their* key. Fixed
  on the same branch; the general lesson — a pin is a preference, an outage is not the user's
  fault — is worth applying wherever else it appears.
- An ONNX build of a candidate embedding model returned all-NaN vectors. Nothing raised; cosine
  over NaN is a ranking indistinguishable from random. A guard now refuses non-finite vectors at
  load, which is the pattern to copy: validate at the boundary where the data enters.

## What to do

- **Report the pipeline's health where someone will see it.** Queue depth per kind, age of the
  oldest unworked item, embedding coverage against open postings, per-source last-success and
  failure counts. An admin overview already exists and already computes some of this; extend it
  rather than building something parallel.
- **Alert on the conditions that mean the product has quietly stopped working**: no successful
  sweep for a source in N days, embed queue not shrinking, detail queue growing without bound,
  scope health degrading.
- **Verify the backup actually restores.** A backup script exists; a restore that has never been
  performed is a hypothesis. Restore it into a throwaway database and check the corpus is intact.
- **Write down the operational runbook** — how to roll back a deploy, how to pause the runner,
  how to reset the development databases, what to do when a source starts failing. Some of this
  is in commit messages and nowhere else.

## Discovery clues

- `compose.yaml`, `Dockerfile` — the deployment, including the one-image/three-roles arrangement.
- `trouveur/runner/service.py` — the sole executor of scans and owner of the schedule.
- `trouveur/db/queries/admin.py` — corpus overview, description coverage, vector-space counts.
- `trouveur/work.py` — the queue, `backlog()`, and how claiming works.
- `trouveur/db/schema.py` — `source_sweep`, `source_scope_health`, `pipeline_run`.
- `backup.sh`, and the deploy workflow under `.github/`.

## Watch out for

- Production and development share this host. Anything that restarts containers or rebuilds
  indexes affects the live system.
- The runner owns the schedule row and drains the run queue; two runners would contend.

## Done when

The health of every stage is visible without a shell, the conditions that mean "quietly broken"
raise something, a restore has actually been performed, and the runbook exists.
