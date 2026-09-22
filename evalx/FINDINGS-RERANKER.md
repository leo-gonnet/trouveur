# Measuring the reranker

Run 2026-09-22 against `origin/main` (bf33935), over a copy of the production corpus. Companion
to [FINDINGS.md](FINDINGS.md), which measures retrieval; this measures the stage after it.

The reranker decides what the user actually reads. Everything it scores is shown, ordered by that
score, with no threshold — so the ordering *is* the product. It is also the only stage that costs
money, and until now the least measured thing in the system: graded only against ~30 planted
needles, on the question "does it invert the obvious cases", and known not to be reproducible.

## Method

**The corpus.** A `pg_dump` of the production corpus taken 2026-09-19 (225,554 postings), restored
into a standalone database on :55433. Production was never written to. That copy had been narrowed
to 25,028 open postings for the encoder-swap rehearsal, which is a tenth of what production
retrieves over — so before measuring anything, production's `job_embedding` table was streamed
into it and the postings it covers re-opened. Because *"`job_embedding` holds open postings only"*
is an invariant, that one table carries both the vectors and the set of postings to re-open.

**The reference set.** Planted needles cannot measure precision: an unplanted posting ranking
first is unjudged, not wrong. So the standard pooling construction — the union of the top 200
retrieved by each configuration under test, every distinct posting judged exactly once, every
configuration scored against that one set. Three differences from the existing study in
`judge.py`, each of which it needed:

- **A stronger judge, from another model family.** `judge.py` judges with the production reranker,
  which can compare two retrieval strategies but cannot say whether the reranker is any good.
  Here the judge is `openai/gpt-5.1`. `google/gemini-2.5-pro` was the first choice and cannot be
  used at all: it answers HTTP 400 `"Reasoning is mandatory for this endpoint and cannot be
  disabled"` to `reasoning: {"enabled": false}`, which `llm.complete` always sends and which
  exists because without it a reasoning model spends the whole token budget on hidden thinking
  and returns an empty, billed response. The two carry the same list price ($1.25/$10 per
  million); the difference is that Gemini's mandatory thinking tokens bill at the output rate,
  so the same 792 judgements would have cost several times more.
- **One posting per call.** Position bias within a batch and contamination between postings
  sharing a prompt are two of the things being measured. A reference set that carried those same
  artifacts could not measure them. It costs almost nothing extra: a batch only amortises the
  profile block, and the prompt is dominated by the posting.
- **Graded relevance 0–3, not a score out of 100.** The finding that started this exercise is that
  absolute scores do not repeat. A band is a judgement a model can give twice.

The judge sees the posting truncated to `rerank._DESCRIPTION_CHARS`, exactly as the reranker sees
it. A judge reading more of an advert than the reranker is ever shown would mark it down for
missing what was never in its prompt.

**The personas** gained a background paragraph each (brief 04), written from `personas.json` alone
and hashed in `backgrounds.sha256` before `needles.json` was read, so nothing here could be tuned
to the answers. They cannot leak into the needle tiers: `deterministic_queries` does not read the
field, which is what `tests/unit/test_eval_needles.py` checks.

**How far to trust the judge.** 279 of the 792 postings were judged a second time by
`moonshotai/kimi-k2.5`, a different family again, on the same rubric. Exact agreement 73%, within
one band **99%**, and 93% on the only distinction any of the numbers below actually turn on —
whether a posting is worth reading at all. Kimi is the stricter of the two (43 postings called
good against gpt-5.1's 57), which is a calibration offset rather than a disagreement about
ordering.

That number does more than validate the reference set. Two models that have never seen each
other's output place essentially every posting in the same band, so **relevance here is not an
inherently ambiguous judgement** — which means the instability measured below is a property of
the production reranker's scoring form and model, not of the question it is being asked.

**What this cannot do, stated plainly.** It is an LLM grading an LLM. The judge sees a posting and
a profile and never the query that retrieved it, so it cannot prefer a configuration for phrasing
itself the way the judge would — but a posting no configuration retrieved is never judged at all,
and three personas is a small sample. These are gaps between configurations, not absolute quality.

## The judged set, and what the paid window is actually worth

792 postings judged: 294, 247 and 251 for the three personas, pooled from the top 200 of both
expansion variants. That is the first precision number this system has ever had.

| persona | pool | good (≥2) in top 150 | strong (3) in top 150 |
|---|---|---|---|
| `wing_nachhaltigkeit` | 294 | 33 (22%) | 12 (8%) |
| `backend_java` | 247 | 82 (55%) | 25 (17%) |
| `maschinenbau_zu_daten` | 251 | 41 (27%) | 13 (9%) |

Of the 150 postings a user pays to score and then reads, between a fifth and a half are worth
reading, and under one in five is a strong match. The spread between personas is the finding to
take seriously: `backend_java` searches a well-populated market in its own vocabulary, and the
two German engineering personas — the career-change cases — do not.

## Depth: `rerank_limit` does not saturate at 150

Free to measure: the reference set already says what each posting is worth and the pool already
says what order retrieval put them in.

Cumulative postings worth reading, by retrieval rank:

| persona | 25 | 50 | 75 | 100 | 125 | 150 | 175 | 200 |
|---|---|---|---|---|---|---|---|---|
| `wing_nachhaltigkeit` | 14 | 21 | 23 | 27 | 30 | 33 | 37 | 41 |
| `backend_java` | 24 | 41 | 55 | 66 | 72 | 82 | 89 | 103 |
| `maschinenbau_zu_daten` | 12 | 22 | 26 | 32 | 38 | 41 | 45 | 46 |

Strong matches only (grade 3):

| persona | 25 | 50 | 75 | 100 | 125 | 150 | 175 | 200 |
|---|---|---|---|---|---|---|---|---|
| `wing_nachhaltigkeit` | 6 | 10 | 11 | 11 | 11 | 12 | 14 | 14 |
| `backend_java` | 11 | 15 | 18 | 19 | 22 | 25 | 27 | 29 |
| `maschinenbau_zu_daten` | 7 | 10 | 11 | 11 | 11 | 13 | 13 | 13 |

**Two different answers, and the difference is the point.** Strong matches are nearly all found by
rank 75: two of three personas gain one or two between 75 and 150, and `backend_java` keeps
climbing only because its market is deep. But merely *good* postings keep arriving at a steady
three to fourteen per 25 slots all the way to 200, with no sign of stopping — the pool was only
built to 200, so where it does stop is not measured here.

So `rerank_limit = 150` is not wasteful and is not obviously right either: **it is a reading
budget, not a cost budget or a quality threshold**, and the evaluation cannot pick it for the
user. What it can say is that the two candidate reasons to lower it both fail. Quality has not run
out at 150, and the money is not the constraint.

## The background field, at the retrieval stage: no

Brief 04's premise is that expansion has to invent a plausible next role from a job title, and
that when it was measured the pinned model wrote adverts for the role the candidate already has.
A background paragraph should fix that. Measured two independent ways, it does not.

**Planted needles** — `live_nobg` and `live_bg` are the expansions the deployed prompts produce
today for the same profile with the field filled in and cleared, routed exactly as production
routes them:

| strategy | @10 | @25 | @50 | @150 | @400 | @2000 | MRR | median rank | T1 | T2 | T3 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `live_nobg` | 4/21 | 7/21 | 10/21 | 12/21 | 15/21 | 18/21 | **0.160** | **49** | 9/9 | 4/6 | 5/6 |
| `live_bg` | 5/21 | 6/21 | 9/21 | 12/21 | 14/21 | 18/21 | 0.151 | 82 | 9/9 | 4/6 | 5/6 |

Tier coverage is **identical** — T3, the adjacent-role tier the field was supposed to reach, is
5/6 either way. MRR is marginally worse and the median needle rank roughly doubles.

**Judged precision over the same pool**, which does not depend on the needles at all:

| persona | good@150 without → with | strong@150 without → with |
|---|---|---|
| `wing_nachhaltigkeit` | 33 → 30 | 12 → 12 |
| `backend_java` | 82 → 74 | 25 → 23 |
| `maschinenbau_zu_daten` | 41 → 42 | 13 → **17** |

The field is not inert: the two variants' top-150 overlap by only 0.56–0.60, so roughly 43% of
what the user would be shown changes. It simply does not change for the better. The one place it
helps is the one the brief predicted — `maschinenbau_zu_daten`, the career-change persona whose
current and target titles share no words, gains four strong matches. One persona and four
needles' worth of movement is a hypothesis, not a result.

The likely mechanism is worth recording, because it bounds how much could ever have been
expected: `combine()` caps the query list at `MAX_QUERIES = 8`, and these personas' deterministic
queries already fill seven of those eight slots. Whatever the background does to the generated
*phrases* is almost entirely truncated away before retrieval sees it. Only the adverts carry it,
and those are what moved.

## Reproducing

    scripts/devdb.sh up                         # or the standalone container on :55433
    export TROUVEUR_EVAL_DATABASE_URL=postgresql+asyncpg://trouveur:x@127.0.0.1:55433/trouveur
    export DATABASE_URL=$TROUVEUR_EVAL_DATABASE_URL
    uv run alembic upgrade head
    uv run python -m evalx.live                 # expansion artifacts, both variants
    uv run python -m evalx.reference --repool   # the judged set; resumable, cap with --max-usd
    uv run python -m evalx.rerank_lab depth     # free: no model call
    uv run python -m evalx.rerank_lab stability,position,contamination,batch,form,background

`TROUVEUR_EVAL_LLM_KEY` is required for everything but `depth`, and is deliberately never a
stored user credential. `reference.py` and `rerank_lab.py` both take `--max-usd` and stop rather
than pass it; `reference.py` judges shallow retrieval ranks first, so stopping early leaves the
top of the ranking completely judged instead of a scatter of holes through it.

Neither writes to `user_job_match` or `llm_score_cache`. An experiment that reused a user's cache
would read a stored verdict instead of calling the model and measure nothing — which is also why
none of these runs can be reproduced by re-running a real match.
