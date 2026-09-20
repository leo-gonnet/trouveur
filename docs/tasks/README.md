# Task briefs

One file per piece of work worth doing, written to be handed to someone -- or some agent -- with
no prior exposure to this codebase. Each brief states the problem with its evidence, what to do
at the level of intent rather than instruction, where to look to find the rest out, and what
"done" means.

They are ordered by expected impact on the only outcome that matters: postings the user actually
wants to apply to. They are not dependencies on each other unless a brief says so.

## Orientation, common to all of them

Trouveur is a self-hosted job radar for the DACH market. It sweeps ~10 job sources into a raw
archive, normalises each posting into a canonical row with derived facets, embeds it, retrieves
per user with a hybrid dense + lexical search, and ranks the shortlist with an LLM paid for by
that user's own API key. Roughly 229,000 live postings, deployed as Docker Compose on a single
four-core VPS that also hosts the development database.

Read `AGENTS.md` first -- it is the architectural contract, and several briefs below deliberately
stop where it draws a line. `CLAUDE.md` covers working conventions. The shape of the system:

    trouveur/sources/     one adapter per job board; archive raw payloads, never parse in place
    trouveur/ingest/      normalise -> derive facets -> embed, each a versioned pure function
    trouveur/match/       expand queries -> retrieve -> fuse -> rerank; the per-user path
    trouveur/db/queries/  all SQL, grouped by the page or stage that issues it
    trouveur/eval/        planted-needle retrieval evaluation
    evalx/                A/B experiment layer from the 2026-09 retrieval study (see FINDINGS.md)

Two invariants worth knowing before changing anything:

- Everything downstream of the raw archive is a pure function of it, and each stage carries a
  version in `trouveur/versions.py`. Bumping a version refills that stage's work queue, so a
  backfill and an upgrade are the same code path. `trouveur refill --kind <stage>` is the entry.
- The system has no LLM spend of its own. Every model call is made with a user's own key, so
  cost control is structural: retrieval is free and unbounded, reranking is bounded by
  `rerank_limit` and cached per (content hash, user, profile version).

## Ground rules

- Production runs on this same host under the `trouveur` compose project. Never point a
  development or evaluation process at it. `scripts/devdb.sh` (untracked, local to the host)
  starts throwaway databases; a copy of the production corpus can be taken with `pg_dump` and
  restored into one.
- A live sweep from a development process doubles the request rate at every provider, because the
  politeness budget is per process, not per machine.
- The evaluation reports, it never gates a merge. Do not turn it into a test.

## The briefs

| # | Brief | Why it is where it is |
|---|---|---|
| 01 | [Finish the encoder swap](01-encoder-swap.md) | Measured win, already built, waiting on a backfill |
| 02 | [Measure and improve the reranker](02-reranker.md) | Decides the final order; currently unmeasured |
| 03 | [Close the feedback loop](03-feedback-loop.md) | The only change that improves the product weekly |
| 04 | [Enrich the profile input](04-profile-background.md) | Cheap; improves expansion and reranking at once |
| 05 | [Fix the freshness economics](05-freshness.md) | A radar that is three days late has found nothing |
| 06 | [Reshape how results are presented](06-presentation.md) | 150 undifferentiated rows is not a result |
| 07 | [Correct the profile-edit message](07-profile-edit-cost.md) | Small, and currently misleading |
| 08 | [Strengthen the evaluation](08-evaluation.md) | Everything above needs it to be trustworthy |
| 09 | [Simplify what no longer earns its place](09-simplify.md) | Remove before adding |
| 10 | [Operational hardening](10-operations.md) | Most findings took hours to see because nothing reports |

Runbooks for work already in flight live in `docs/runbooks/`.
