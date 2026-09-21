# Encoder swap — in-situ measurement

The bake-off in `../FINDINGS.md` ranked models by exact cosine over a fixed pool. This is the
same question asked through the actual pipeline: real hard filters, both retrieval arms, RRF,
HNSW indexes, the planted-needle harness.

## Setup

A copy of the production corpus reduced to **25,021 open postings** including every planted
needle, so a full backfill was affordable (~5.5 hours at 77 documents a minute) instead of the
~54 hours the whole corpus would take. Reduced by setting `closed_at` directly rather than
through the close path, so the narrow vectors survived.

Two confounds were found and removed before measuring, both of which would have flattered the
new model:

1. **The narrow HNSW index was missing**, so the old space was being searched exactly while the
   new one used an approximate index. Rebuilt, and the narrow numbers turned out identical
   either way at this corpus size — exact and approximate agree with `ef_search` at 200.
2. **`job_embedding` held rows for closed postings.** Production deletes an embedding when a
   posting closes, so the index only ever contains the live set; setting `closed_at` directly
   broke that. 198,946 of 223,967 rows belonged to closed postings, which would have made the
   narrow index 89% dead weight — pgvector filters *after* the index walk, so that is lost recall
   rather than wasted space, and it would have crippled the incumbent. Deleted, restoring the
   invariant, and both spaces then held exactly the same 25,021 postings.

## Result

|                       | 384 (incumbent) | 768 (mpnet) |
|-----------------------|-----------------|-------------|
| fused recall          | 18/21           | **21/21**   |
| dense arm alone       | 18/21           | 20/21       |
| lexical arm alone     | 9/21            | 9/21        |
| recall@10             | 5/21            | 7/21        |
| recall@25             | 10/21           | 12/21       |
| recall@50             | 12/21           | **18/21**   |
| recall@100            | 17/21           | **21/21**   |
| mean reciprocal rank  | 0.202           | 0.214       |
| needles missed        | 3, all T2       | **none**    |

T2 — needles for the same role written with none of the profile's vocabulary — goes from 3/6 to
6/6. Those three misses were the only misses, and they are exactly the tier the bake-off
predicted would move.

Two honest qualifications:

- **Retrieval-stage inversions rose slightly**, 15 to 17 across the three personas: a few more
  planted negatives sit above a planted positive in the fused list. One more negative is also
  filtered out entirely. These are small counts on 9 negatives and the reranker sits downstream
  of them, but it is movement in the wrong direction and worth watching.
- **The lexical arm now contributes something it did not before.** At 384 the fused result equalled
  dense alone (18 and 18); at 768 fusion finds one needle dense misses (21 against 20). That is a
  point against retiring the hybrid, which `docs/tasks/09-simplify.md` raises.

Raw scorecards: `before-384.txt`, `after-768.txt`.
