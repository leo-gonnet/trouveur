# 05 — Fix the freshness economics

## What has been decided and built

Two halves of this brief are done; the expensive half is not. Recorded here so the remainder can
be picked up without re-deriving the decisions.

**A retrieval horizon of 7 days.** The recommendable corpus is bounded by age, not only by
whether a posting is still open. Seven days was chosen against measured steady-state discovery
lag -- p99 under 1.3 days for every source carrying volume -- so it is roughly five times the
worst case and drops old inventory rather than postings we were slow to find. It is a corpus
boundary, not a query filter: `job_embedding` holds postings that are open AND recent, pruning
keeps the ANN index small, and the retrieval predicate keeps the answer exact while a prune is
pending. Defined once in `trouveur/db/queries/freshness.py`; five call sites read it.

Effect on this installation: 240,821 open postings, 131,701 inside the horizon.

**Recommendations are published one day at a time.** An edition is keyed on `scored_at`, not
`posted_at` -- see the commit and `trouveur/db/queries/match.py` for why publication date loses
postings outright. Editions carry their profile version and are immutable.

**Deliberately not built: carryover.** A posting that falls into yesterday's edition unactioned
is reachable only by navigating to that day. `state` already has `saved`/`applied`, so the
obvious shape is an edition-independent Saved view, possibly with a rail of recent unactioned
high scorers. Decide it when the page has been lived with.

## What is left: the request budget

This is the part that was never about the retrieval path.

Sources are swept politely -- roughly one request a second, shared per provider -- and one
source alone (Workday, ~65 boards at up to 500 pages each) was measured at about 20 minutes per
board, roughly 22 hours for a single full pass. Nobody has decided how that budget *should* be
spent, so it is spent in configuration order.

Note before starting: **Workday, Lever, Ashby, Personio, Breezy and Rippling currently hold zero
postings between them.** Workday's 22-hour pass has never produced a row. That is the first
thing to explain, and possibly the whole finding.

1. **Measure time-to-discovery per source.** Postings carry a published date, a first-seen
   timestamp and a source, so this is reconstructable -- and the offsite archive
   (`docs/runbooks/corpus-export.md`) now keeps the history needed to do it over time rather
   than over whatever is currently in the database.
2. **Measure yield per request.** Postings per request, and more usefully *postings a user
   engaged with* per request. A source contributing thousands of irrelevant vacancies is not
   earning its rate limit.
3. **Then decide the budget deliberately.** Likely outcomes: sweep high-yield boards often and
   the long tail rarely or by sampling; drop sources contributing nothing; rotate through the
   largest source across days rather than trying to complete a pass in one cycle, which the
   scope-health tracking already makes possible.
4. **Settle the detail-fetch policy.** A posting awaiting its description is deliberately not
   queued for embedding, so it is absent from the vector index rather than merely thin. With the
   horizon in place this now has a deadline attached: a description that arrives on day 8 arrives
   after the posting has left the recommendable set. Either prioritise detail fetches for recent
   postings, or embed on title alone and re-embed when the description lands, or measure and
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
