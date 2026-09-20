# 01 — Finish the encoder swap

## The problem

The dense arm embeds with `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`: 384
dimensions, symmetric, and — measured, not assumed — truncating at **128 tokens rather than the
~512 the code comments claim**. 96.4% of postings exceed that limit and the median posting has
only 41% of its assembled text encoded.

A bake-off over a copy of the production corpus found the encoder, not the query phrasing, is the
ceiling. `paraphrase-multilingual-mpnet-base-v2` (768-d) with *plain deterministic queries* beat
the incumbent *with eight synthetic adverts*. Confirmed on two independently built pools so it is
not an artifact of pool construction — on the neutral pool, 20/21 planted needles in the top 50
against 16/21, MRR 0.261 against 0.238, and per-tier 8/6/6 against 6/4/6.

Evidence and method: `evalx/FINDINGS.md`, part two.

## What is already done

On branch `feat/retrieval-eval`:

- `local-onnx-mpnet` is registered as a provider.
- `job_embedding` has a second nullable `embedding_768` column with its own partial HNSW index
  (alembic `0008`), so both vector spaces coexist.
- Width is two settings, because during a backfill they genuinely differ: `EMBEDDING_DIM` is what
  the embed worker *writes*, `EMBEDDING_READ_DIM` is what retrieval *reads*. Retrieval keeps
  serving the old space until the new one is covered.
- The column for a width is named in exactly one place, so a reader and writer cannot disagree.
- Verified end to end at small scale: wide vectors written beside narrow ones, wide reads
  correctly restricted to rows that have a wide vector, narrow reads unaffected.

## Rehearsed, with numbers

The whole sequence below was run on a 25,021-posting copy of the production corpus on
2026-09-21: refill, drain, coverage, and the harness on each space in turn. Result, through the
real pipeline rather than a pool: fused recall 18/21 -> 21/21, recall@50 12/21 -> 18/21, and the
three needles the old space missed are all found. Details and the two confounds that had to be
removed first: `evalx/swap/README.md`. Order of operations for production:
`docs/runbooks/encoder-swap.md`.

Measured backfill rate on this host with nothing else competing: **77 documents a minute**, so
the full corpus is roughly two days.

## What is left

Production cannot start until this branch is merged and deployed: the running image has no mpnet
provider, no wide column and no migration 0008. That deploy is a no-op by design -- new code,
same vector space, same results -- and is the prerequisite for everything below.

Then: run the backfill, verify the new space in situ, flip the read width, retire the old column.

Proceed roughly like this:

1. **Rehearse on a copy, not on production.** Take a `pg_dump` of the production corpus into a
   throwaway database and do the whole sequence there first, including the measurement. The
   rehearsal is what tells you how long production will take and whether anything breaks at size.
2. **Queue the work.** Changing the provider changes the embedding version string, and
   `trouveur refill --kind embed` queues every row whose stored version is not the current one.
   It is chunked and resumable; interrupting it costs nothing.
3. **Drain it.** The ordinary embed worker does the rest. Expect this to be the long pole: this
   host embeds roughly 180 documents a minute with the current model and roughly 70 with mpnet,
   so the full corpus is on the order of two days. The work queue uses `SKIP LOCKED`, so more
   than one drain worker is supported by design — but the box has four cores and production's
   runner is competing for them, so pause or throttle the runner rather than oversubscribing.
4. **Watch coverage, do not guess it.** Count rows with a wide vector against open postings.
   Consider adding this to the admin overview, which already reports embedded counts and distinct
   vector spaces.
5. **Measure before flipping.** Run the planted-needle harness against the copy with the read
   width on the old space, then on the new one, over the same corpus snapshot. This is the
   in-situ confirmation of the bake-off; do not skip it because the bake-off was convincing.
6. **Flip the read width** only once coverage is complete, and only on a corpus where step 5
   passed.
7. **Retire the old column** in a separate migration, after the new space has served for long
   enough that you would have noticed a problem.

## Discovery clues

- `trouveur/ingest/embed/` — the provider seam, the width guard, the column-for-width mapping.
- `trouveur/versions.py` — how versioned stages and backfills relate.
- `trouveur/cli.py`, the `refill` and `drain` commands.
- `trouveur/db/queries/match.py` — the dense query and how it picks its column.
- `trouveur/db/queries/admin.py` — corpus overview, where coverage reporting belongs.
- `trouveur/eval/harness.py` and `trouveur eval` — the measurement.

## Watch out for

- **Postgres shared memory.** Building an HNSW index at this size needs `maintenance_work_mem`
  raised, and a parallel build puts that memory in `/dev/shm`, which Docker caps at 64MB by
  default. The failure message says "could not resize shared memory segment", which reads like a
  full disk and is not one. `compose.yaml` on this branch sets `shm_size`; make sure the
  production container actually picks it up.
- **Two large models in one process tree will exhaust a 7GB box.** Do not embed with one model
  while another is loaded elsewhere.
- **Disk.** A second vector space is not free; 224k rows of 768-d halfvec plus its index is on the
  order of a gigabyte.

## Done when

Every open posting has a wide vector, the needle harness on the new space is at least as good as
the bake-off predicted, retrieval reads the new space in production, and the old column is gone
in its own migration. Record the before and after numbers in `evalx/FINDINGS.md`.
