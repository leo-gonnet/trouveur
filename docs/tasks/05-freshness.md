# 05 — Fix the freshness economics

## The problem

A job radar that surfaces a posting three days after it appeared has, for competitive roles,
found nothing. The system currently optimises for coverage and treats freshness as a by-product.

The tension is concrete. Sources are swept politely — roughly one request a second, shared per
provider — and one source alone (Workday, ~65 boards at up to 500 pages each) was measured at
about 20 minutes per board, which is roughly 22 hours for a single full pass. Meanwhile
delta-first feeds contribute almost nothing to a fresh database; one returned ten postings.
Nobody has decided how that request budget *should* be spent, so it is spent in configuration
order.

## What to do

Make the trade explicit and measured, then act on it.

1. **Measure time-to-discovery per source.** For each source, how long between a posting being
   published and Trouveur holding it with a description. The data to reconstruct this largely
   exists — postings carry a published date, a first-seen timestamp and a source.
2. **Measure yield per request.** Postings per request, and more usefully *postings a user
   actually engaged with* per request. A source that contributes thousands of irrelevant
   vacancies is not earning its rate limit.
3. **Then decide the budget deliberately.** Likely outcomes: sweep high-yield boards often and
   the long tail rarely or by sampling; drop sources that contribute nothing; stop trying to
   complete a full pass of the largest source in one cycle and instead rotate through it across
   days, which the scope-health tracking already makes possible.
4. **Settle the detail-fetch policy.** A posting awaiting its description is deliberately not
   queued for embedding, so it is absent from the vector index entirely rather than merely thin.
   That is a defensible choice, but it means the dense arm competes on a smaller corpus than the
   lexical arm, invisibly. Either prioritise detail fetches for recent postings, or embed on
   title alone and re-embed when the description arrives, or keep the current behaviour and
   report the gap.
5. **Consider cross-source duplication.** The same job appears on a company's Greenhouse board
   and on Arbeitsagentur. There is a dedup marker pass; check whether it catches this case and
   whether the retrieval path uses it, because showing a user the same job three times spends
   their attention and their rerank budget.

## Discovery clues

- `trouveur/ingest/pipeline.py` — how a sweep is orchestrated and what the report records.
- `trouveur/sources/http.py` — the politeness budget, and the fact that it is per process.
- `trouveur/sources/base.py`, `board.py` — the shape of an adapter and of a paged source.
- `trouveur/db/schema.py` — `source_sweep`, `source_scope_health`, and the dedup columns on `job`.
- `trouveur/ingest/persist.py` — where the decision not to queue undetailed postings lives.
- `trouveur/runner/service.py` — the schedule and what a run actually does.

## Watch out for

- The politeness budget is per process. Any experiment that sweeps live sources while the
  production runner is sweeping doubles the request rate at every provider. Coordinate or use
  archived payloads.
- Sources are other people's infrastructure. Changing cadence upward is a request to reconsider,
  not a knob to turn freely.

## Done when

There is a per-source table of freshness and yield, a documented decision about how the request
budget is allocated, and the schedule reflects it.
