# Evaluating the synthetic-advert expansion proposal

> This file measures **retrieval**. The stage after it is measured in
> [FINDINGS-RERANKER.md](FINDINGS-RERANKER.md): score stability, position bias and contamination
> inside a batch, batch size, scoring form, and what `rerank_limit` should be.

Run 2026-09-19 against `origin/main` (17c8ae0, the deployed code), over a read-only copy of the
production corpus: **225,524 postings, 223,944 vectors, 223,949 open**. Production was not
touched: `pg_dump` from `trouveur-db-1`, restored into a standalone `trouveur-eval-db` on :55433.

## Method

The repo harness supplies everything that must match production -- planting a needle through the
real ingest path, the profile, the retrieval SQL, RRF. Only the queries vary. `evalx/strategies.py`
adds one capability the production path lacks: **routing a query to one arm**, without which the
proposal cannot be tested at all (see finding 2).

Two expansion fixture sets:

- `expansions.json` -- written by a frontier model (Claude) from `personas.json` **alone**, hashed
  (`expansions.sha256`) **before `needles.json` was read**, so no strategy could be tuned to the
  answers. Upper bound on advert quality.
- `expansions_llm.json` -- the same artifacts from `deepseek/deepseek-v4-flash`, the model
  production pins. What would actually ship.

Recall is measured on the 21 planted positives. Precision is measured separately by TREC-style
pooling: the union of each strategy's top-20 is judged once per posting by the **production
reranker** (same prompt, model and pin), and every strategy is scored against that one judgement
set. The judge never sees the query that retrieved a posting, so it cannot prefer a strategy for
phrasing itself the way the judge would.

## Result

Baseline `det` (deterministic queries, no API key): T1 median rank 5, T2+T3 median 503,
13/21 inside the 150-slot rerank window, MRR 0.184.

| strategy | @25 | @150 | @400 | @2000 | MRR | T1 | T2 | judged mean | good | nDCG |
|---|---|---|---|---|---|---|---|---|---|---|
| `det` | 5/21 | 13/21 | 15/21 | 18/21 | 0.184 | 9/9 | 3/6 | 51.0 | 20 | 0.656 |
| `phrases8` (today, with key) | 5/21 | 13/21 | 15/21 | 18/21 | 0.177 | 9/9 | 4/6 | 57.3 | 22 | 0.740 |
| **`det_plus_ads8`** | **9/21** | 13/21 | **20/21** | **20/21** | 0.188 | 9/9 | 5/6 | **61.1** | **26** | **0.752** |
| `llm_ads_v2` (cheap model) | 6/21 | 12/21 | 17/21 | 20/21 | 0.182 | 9/9 | 5/6 | 54.5 | 21 | 0.689 |
| `titles30` | 8/21 | 10/21 | 11/21 | 16/21 | 0.044 | 6/9 | 4/6 | 52.3 | 16 | 0.626 |
| `tokens_lex` | 4/21 | 11/21 | 14/21 | 18/21 | 0.127 | 9/9 | 3/6 | - | - | - |
| `full_proposal` | 8/21 | 11/21 | 14/21 | 18/21 | 0.109 | 7/9 | 5/6 | 59.3 | 24 | 0.700 |

Component by component:

1. **Synthetic adverts, dense arm only, added to the user's own words: keep.** They move exactly
   the tier they should -- T2/T3 are the needles sharing no vocabulary with the profile, median
   rank 503 -> 98. Deep recall 15/21 -> 20/21 @400. And they do *not* fill the paid window with
   noise: judged mean 51.0 -> 61.1, postings scoring >=70 20 -> 26, consistent across all three
   personas. 5-8 adverts; 15 is not better than 8, and past that it degrades.

2. **Adverts must be routed to the dense arm.** A 69-word advert becomes a 65-term ANDed
   `websearch_to_tsquery`: **0 rows**. A 167-word one, 158 terms: **0 rows**. Both the tsvector and
   the `LIKE` path. Sending adverts to both arms wastes half the query budget on empty results.

3. **Adverts must be ADDED to the deterministic queries, not replace them.** Substituting costs
   T1: `data_t1_rare_pdm` 31 -> 341, `wing_t1_rare_iso` 130 -> 733. The additive variants hold
   T1 at 9/9 with no rank damage while keeping the T2 gain.

4. **30 adjacent titles: drop.** Every one of the 9 T1 needles ranked worse; `wing_t1_kreislauf`
   1 -> 19; three needles lost entirely; MRR 0.184 -> 0.044; judged nDCG below baseline (0.626).
   **Not a budget artifact**: `titles30_deep` with 8x the per-query depth is *worse* (MRR 0.034).
   The cause is structural -- RRF sums 1/(60+rank) across lists, so a posting sitting mid-list in
   30 correlated title searches outscores the exact match that appears first in one.

5. **Rare exact tokens: drop.** As 20 separate lexical queries: 1 needle improved, 8 worsened
   (T2+T3 median 503 -> 846). OR'd into a single query to protect the budget: still no gain
   (MRR 0.181 vs 0.184), and no gain on top of adverts either. The one token-dependent needle it
   was meant to rescue, `wing_t1_rare_iso`, moved 130 -> 121.

6. **Mean-pooling the advert vectors: drop.** MRR 0.166 vs 0.184 baseline, median rank 169. The
   HyDE paper's own recipe measures worse here than simply issuing the adverts as separate queries.

7. **The whole proposal as stated is worse than doing nothing** on the metric that decides the
   bill: `full_proposal` puts 11/21 in the window against the baseline's 13/21, at 285 queries
   and 268s per run against 36 queries and 33s.

## Generation is the weak link, not the idea

The proposal's benefit is a function of advert quality, and the pinned cheap model does not
deliver it by default:

- Its first-draft adverts describe the role the candidate **already has** -- fatal for a
  career-change profile, which is exactly the case adverts are supposed to fix. T2 stayed at 3/6.
- Its 15 adverts shared 259 distinct words against 435 for the good set: near-duplicate queries.
- A prompt fix (`ads_v2`: "write the role they are moving TO, never the role they already have",
  one distinct role per advert, ~70 words, discriminating words first) took it to T2 5/6 and
  @2000 20/21 -- most of the gap closed, for $0.0004 per profile.
- **Reliability is the real cost.** Asking for 15 long adverts in one call failed in 2 of 3
  personas (unparseable JSON, a single 2,593-word blob, or the 6,000-token ceiling). Chunking into
  3 calls of 5 still blew the budget once. Short adverts succeeded 15/15 for all three.

## Three defects found on the way, independent of the proposal

1. **The encoder truncates at 128 tokens, not the ~512 the code comments assume.** Measured on 500
   real postings: 96.4% exceed it; the median posting has **41%** of its assembled embedding text
   encoded. `_DESCRIPTION_BUDGET = 1200` in `ingest/embed/text.py` is ~2.4x more than the model
   ever reads. This also caps advert length: a 200-word advert is silently cut to its first third.

2. **`expand.parse_response` silently discards a common model response shape.** Asked for "a JSON
   array of strings", the model returns an array of `{title, description}` objects a good fraction
   of the time; the `isinstance(item, str)` filter drops every element and returns `[]`, degrading
   to deterministic expansion with nothing surfaced to the user.

3. **The pinned provider is failing right now.** `deepinfra/fp8` returns 429 `engine_overloaded`,
   and `llm.complete` sets `allow_fallbacks: False`, so reranking fails with the misleading
   message "OpenRouter is rate-limiting this key". The same model served fine on other providers.

## On the two non-retrieval parts of the proposal

- **Making a key mandatory for "Match now": the data says no.** `det` with no key reaches 18/21 at
  depth and 13/21 in the window -- it is a working search, not a degraded one, and it beats
  `titles30` and `full_proposal` on the window metric. It contradicts the written rule that
  retrieval must work with no key, and the rich expansion is what the key already buys.
- **"You will erase your hidden data" is the wrong warning.** `user_query_expansion` is keyed
  `(user_id, profile_version, expansion_version)`, so a profile edit writes a *new* row and erases
  nothing. What a scoring-field edit does do (`users.save_profile` -> `reset_scores`) is clear
  every cached LLM score, which costs real money on the next match. The honest message is about
  that cost, not about lost data.

## Reproducing

    scripts/devdb.sh up                        # or the standalone container on :55433
    uv run python -m evalx.run --plant --out evalx/results.json
    uv run python -m evalx.judge --depth 20 --strategies "det,det_plus_ads8,titles30"

Judging needs `TROUVEUR_EVAL_LLM_KEY`. The whole evaluation above cost **$0.0122**.

## What this does not measure

- 3 personas, 21 positives. A one-needle difference is 4.8 points; the conclusions drawn here are
  the ones where several metrics move together and a mechanism explains them.
- Precision is pooled, so a posting no strategy retrieved is never judged.
- The reranker is not reproducible run to run (harness note); the judged numbers are gaps between
  strategies measured against one shared judgement set, not absolute quality.

---

# Part two: the encoder, the fusion, and what I had not questioned

The evaluation above varies how the query is phrased. That is, in the end, a way of working
around the embedding model -- HyDE exists because a symmetric sentence encoder cannot match a
three-word query to a 400-word advert. Part two measures the things part one assumed.

## The encoder is the ceiling, by a wide margin

Method: a fixed pool of 2,530 postings -- 1,548 of them the near-misses the live system actually
returns for these personas, half drawn from the lexical arm which uses no encoder at all, plus
random filler -- with the planted needles ranked inside it by **exact cosine**, not HNSW, so ANN
recall is not a confound. Same pool, same queries, one variable.

| model | doc text | queries | @10 | @50 | @250 | MRR | T1/T2/T3 @50 |
|---|---|---|---|---|---|---|---|
| MiniLM-L12-v2 (current) | current | `det` | 6/21 | 8/21 | 16/21 | 0.203 | 6/1/1 |
| MiniLM-L12-v2 | title+desc | `det` | 7/21 | 10/21 | 16/21 | 0.146 | 6/2/2 |
| MiniLM-L12-v2 | current | `det`+8 adverts | 4/21 | 12/21 | 18/21 | 0.188 | 6/1/5 |
| **mpnet-base-v2** | current | `det` | **9/21** | **15/21** | **21/21** | **0.225** | 6/**4**/**5** |

**paraphrase-multilingual-mpnet-base-v2 with plain deterministic queries beats the incumbent with
eight synthetic adverts.** Every planted needle inside the top 250, against 16 of 21. Per tier
inside the top 50, the two tiers the whole advert exercise existed to reach: same-role-different-
words 1/6 -> 4/6, adjacent-role 1/6 -> 5/6.

That is a larger gain than every query-side change in part one combined, and it reframes them:
the adverts were compensating for the encoder, not extending it.

What it costs, which is part of the verdict and not a footnote: 768 dimensions instead of 384,
and ~2.5x the compute per document. This host embeds ~180 documents a minute, so the corpus is a
21-hour backfill today and roughly 54 hours with mpnet, during which the new space is incomplete.
That is why the storage change is two columns and two width settings rather than a truncate and
a re-embed -- the dense arm keeps serving the old space until the new one is covered.

Dropping company and location from the embedded text is a real trade rather than a free win:
tier coverage improves (T2/T3 1/1 -> 2/2) and MRR falls (0.203 -> 0.146). Not changed.

## A model that returned NaN, and seven hours spent on it

`jinaai/jina-embeddings-v2-base-de` was the most promising candidate on paper: German/English
bilingual, 8192-token context, small. It scored 0/21 with MRR 0.0011.

It is not a bad model. **This ONNX build returns all-NaN vectors.** Cosine over NaN produces a
ranking indistinguishable from random, so the failure arrives as a plausible, publishable-looking
"this model is worse" rather than as an error. Seven hours of embedding went into it before the
vectors themselves were looked at.

Both the harness and the production provider now refuse non-finite vectors -- the production one
matters more, because NaN would have flowed into halfvec and turned the ANN index into noise
that looks exactly like a working search returning bad results.

## The fusion rule did not kill the titles

Thirty titles collapsed MRR from 0.184 to 0.044, diagnosed as RRF rewarding a posting for
appearing mid-list in many correlated searches. Dropping titles on that basis abandons a query
set for a defect in a different component, so the diagnosis was tested: same queries, four rules.

| query set | rrf (production) | arm_max | weighted | arm_max_w |
|---|---|---|---|---|
| `det` | **0.184** | 0.059 | 0.184 | 0.083 |
| `det_plus_ads8` | **0.188** | 0.112 | 0.187 | 0.149 |
| `titles30` | 0.044 | 0.033 | **0.048** | 0.035 |
| `full_proposal` | **0.109** | 0.036 | 0.106 | 0.039 |

No rule rescues the titles. Production RRF wins outright everywhere else, so `fuse.py` is
unchanged. `arm_max` is much worse because collapsing an arm discards the agreement between
queries, which is the signal RRF reads. `weighted` being *identical* to `rrf` on `det` is the
check that the weighting does nothing when every query is already the user's own.

## An operational defect the migration found

The production database container has Docker's default **64MB of /dev/shm**. A parallel HNSW
build puts its working memory there, so raising `maintenance_work_mem` to anything appropriate
for 200k vectors fails with `could not resize shared memory segment` -- which reads like a full
disk and is not one. Fixed with `shm_size: 1gb` in compose.yaml.

## The pool did not manufacture the result

The hard pool's dense near-misses are chosen by the incumbent, so the incumbent competes against
its own worst confusions and a challenger does not -- a bias towards the challenger, flagged when
the pool was built. Settling it rather than caveating it: the same two models over a pool of
2,530 purely random distractors, neutral between them.

| model | pool | @10 | @50 | @250 | MRR | T1/T2/T3 @50 |
|---|---|---|---|---|---|---|
| MiniLM-L12-v2 | hard | 6/21 | 8/21 | 16/21 | 0.203 | 6/1/1 |
| mpnet-base-v2 | hard | 9/21 | 15/21 | 21/21 | 0.225 | 6/4/5 |
| MiniLM-L12-v2 | random | 9/21 | 16/21 | 21/21 | 0.238 | 6/4/6 |
| **mpnet-base-v2** | random | **12/21** | **20/21** | 21/21 | **0.261** | **8/6/6** |

mpnet wins on both, so the bias did not create the finding. The two pools also confirm each
other's construction: the hard pool is genuinely harder for both models, which is what it was
for.

## Adverts and a better encoder are complementary

| | @50 | @250 | MRR | T1/T2/T3 @50 |
|---|---|---|---|---|
| mpnet + `det` | 15/21 | 21/21 | 0.225 | 6/4/5 |
| mpnet + `det` + 8 adverts | 16/21 | 20/21 | **0.252** | 5/5/6 |

The best MRR measured anywhere. The advert work is not made redundant by switching model, so the
two changes stack and neither has to wait for the other.

## Chunked document vectors: measured, and worse

The encoder reads 128 tokens and the median posting needs 309, so splitting a posting into two
vectors and taking the best match is the obvious fix that needs no new model. It does not work:
@50 7/21 against 8/21, MRR 0.128 against 0.203, and the same-role-different-words tier falls to
0/6.

Max-pooling lifts every long posting, because the second chunk is usually benefits and equal-
opportunity boilerplate, and a spurious match there scores the whole posting. That flattens the
discrimination instead of adding reach. Worth knowing before paying for a 2x index: as the
obvious implementation, chunking is a regression. Title-prefixed semantic chunks might not be --
untested.

## What part two still does not answer

- e5-large was started and deliberately stopped. At ~10x the incumbent's compute per document it
  is not deployable on a four-core host whatever it scores, and the CPU was better spent
  confirming the result actually being recommended.
- Nothing here re-measures the reranker, which is the stage that decides what the user reads.
- 21 positives and 3 personas throughout. A one-needle difference is 4.8 points, so close
  strategies are not separable; the conclusions drawn are the ones where several metrics move
  together and a mechanism explains them.
