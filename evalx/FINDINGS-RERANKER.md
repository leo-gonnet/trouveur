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

## Stability: the page is stable, the top of the page is not

The same 150 postings, scored five times, same model, same provider pin, `temperature: 0`,
nothing changed between runs.

| persona | mean SD | median range | moved >10 | moved >20 | Spearman | top-20 overlap |
|---|---|---|---|---|---|---|
| `wing_nachhaltigkeit` | 2.8 | 5 | 23/150 | 8/150 | 0.957 | **0.77** |
| `backend_java` | 3.7 | 5 | 35/150 | 15/150 | 0.946 | **0.64** |
| `maschinenbau_zu_daten` | 3.2 | 5 | 38/150 | 5/150 | 0.967 | **0.70** |

Read the last two columns together, because they disagree and the disagreement is the finding.
A Spearman of 0.95 says the ranking as a whole is reproducible — which is the number you would
report if you wanted this to look fine. But the top-20 Jaccard says that **between a quarter and
a third of the first twenty postings differ between two identical runs.** Nobody reads a
correlation coefficient; they read the top of the page, and the top of the page is where the
score distribution is thinnest and a three-point wobble reorders everything.

The 2026-09-11 note in `AGENTS.md` — one needle at 45 on one run and above 70 on the next — is
the tail of this, not an anomaly: 5 to 15 postings per persona move more than 20 points.

## Position inside a batch is worth about ten points

Ten postings, scored ten times, rotated one slot each time so every posting visits every
position. Nothing else changes.

| persona | slot 0 | slot 1 | slot 4 | slot 9 | slot 0 − slot 9 | median swing per posting |
|---|---|---|---|---|---|---|
| `wing_nachhaltigkeit` | 48.5 | 41.0 | 40.0 | 38.0 | **+10.5** | 22.5 |
| `backend_java` | 51.0 | 44.5 | 44.5 | 42.5 | **+8.5** | 27.5 |
| `maschinenbau_zu_daten` | 74.0 | 66.5 | 63.0 | 59.0 | **+15.0** | 20.0 |

**Going first in a batch of ten is worth eight to fifteen points**, consistently across all three
personas, with most of the drop happening immediately after slot 0. And the same posting, in the
same batch, with the same ten adverts around it, swings 20 to 27 points depending only on where
it was printed.

Put that beside the stability table: at a fixed position the median posting moves 5 points across
five runs; moved through the ten positions it moves 20 to 27. **Position is the larger effect by a
factor of four or five, and it is not noise — it is a bias with a direction.**

It is also not randomly distributed over the page. Batches are filled in retrieval order, so
retrieval ranks 1, 11, 21, 31 … land in slot 0 and collect the bonus, every run, systematically.
The page is ordered by a score that partly encodes `rank mod 10`.

## Contamination: the score is relative to the batch, not to the candidate

Six middling postings per persona — ones the judge graded 1, where the placement is genuinely in
question — each scored three times: alone, then first in a batch with nine postings the reference
set grades 2 or 3, then first in a batch with nine it grades 0. The subject sits in slot 0 every
time, so position is held constant and only the company changes.

| persona | alone | among nine strong | among nine weak | weak − strong |
|---|---|---|---|---|
| `wing_nachhaltigkeit` | 20.8 | 17.5 | 33.3 | **+15.8** |
| `backend_java` | 18.3 | 14.2 | 22.5 | **+8.3** |
| `maschinenbau_zu_daten` | 29.2 | 20.8 | 60.0 | **+39.2** |

**The prompt asks for an absolute score and gets a relative one.** The same posting is worth 29
on its own and 60 in bad company. Nothing about the candidate changed, nothing about the advert
changed, and the instruction to score 0–100 against the profile was identical in all three calls.

The direction makes this worse than a wash. Batches are filled in retrieval order, so the weak
company is concentrated *deep* in the ranking and the strong company *shallow*. A mediocre
posting at rank 140 is graded against its mediocre neighbours and inflated; a genuinely good
posting at rank 5 is graded against other good ones and deflated. The effect systematically
compresses the page toward the middle and can lift a deep posting above a shallow one that
deserves the slot.

## What the three effects add up to

| effect | size, in points | direction |
|---|---|---|
| run-to-run noise, position fixed | 5 (median range over 5 runs) | none |
| position within the batch | 8–15 (slot 0 vs slot 9) | favours `rank mod 10 == 0` |
| company within the batch | 8–39 | inflates deep, deflates shallow |

Only the first is noise. The other two are biases with a direction, both larger, and **both are
artifacts of batching ten postings into one prompt** — neither can exist at a batch size of one.
That reframes brief 02's question. The instability is not mainly the model being flaky about
absolute numbers; it is that the unit of judgement is the batch rather than the posting.

## Batch size: one posting per call, and it is not close

The same 150 postings, the same prompt, scored against the reference set at four batch sizes.

| batch | `wing` | `backend` | `maschinenbau` | mean nDCG@20 |
|---|---|---|---|---|
| **1** | **0.962** | **0.927** | **0.954** | **0.947** |
| 5 | 0.898 | 0.840 | 0.831 | 0.856 |
| 10 (production) | 0.871 | 0.806 | 0.957 | 0.878 |
| 20 | 0.819 | 0.859 | 0.930 | 0.869 |

**Batch size 1 wins on every persona.** Between 5, 10 and 20 the ordering is not monotonic and the
differences are inside what three personas can resolve — so the honest statement is not "smaller
batches are better", it is "**scoring one posting at a time is better than scoring several, and
the rest is noise**". That is what the mechanism predicts: position bias and contamination are
both properties of putting more than one posting in a prompt, and neither can exist at a batch of
one. The effects measured directly and the ranking quality they produce agree.

Replicated in a second run, this time metering the money — because "one call per posting" is the
expensive-sounding recommendation and it needs its price attached:

| batch | mean nDCG@20 | cost per user per run of 150 | postings lost to a malformed response |
|---|---|---|---|
| **1** | **0.937** | **$0.00997** | 1–2 of 150 |
| 10 | 0.849 | $0.00694 | 3–6 of 150 |

**The cost objection does not survive contact with the number.** Batch 1 is 44% more expensive in
relative terms and *one third of a US cent* more in absolute terms, for +0.088 nDCG@20. It also
loses fewer postings, because `rerank.parse_response` discards a whole batch when the JSON is bad
— at a batch of ten that is ten postings the user never sees, at a batch of one it is one.

**The one real obstacle is latency, not money.** `match/pipeline.py` sends batches sequentially so
the ceiling can be checked before each one, and 150 sequential calls is 12 to 35 minutes per user
against 2 to 4 today. Adopting this means making the paid loop concurrent while keeping the
pre-flight budget check — with a bounded semaphore the overshoot is at most a few in-flight calls,
which at these prices is a fraction of a cent. That is a design change to the most
safety-critical loop in the system, so it is written down here as the recommendation and left for
its own piece of work rather than bundled into this one.

## Scoring form: absolute against banded is a wash

Four bands with ties broken by retrieval rank, against the production 0–100, both at batch 10,
each run twice so the repeat overlap is measurable.

| persona | absolute nDCG@20 | banded nDCG@20 | absolute repeat overlap | banded repeat overlap |
|---|---|---|---|---|
| `wing_nachhaltigkeit` | 0.857 | 0.907 | 0.74 | 0.82 |
| `backend_java` | 0.805 | 0.768 | 0.60 | 0.54 |
| `maschinenbau_zu_daten` | 0.947 | 0.929 | 0.60 | 0.74 |
| **mean** | **0.870** | **0.868** | **0.65** | **0.70** |

Banded is better on one persona, worse on another, and its slightly higher repeat overlap does not
hold across all three. **Change nothing here.**

The brief's hypothesis was that absolute 0–100 scoring is what drifts. The evidence says it is
not. The form makes no reliable difference; the model already compresses 0–100 into 14 to 20
distinct values by itself, with the largest single tie covering 18 to 50 of 150 postings, so the
scale is banded in practice whatever the prompt asks for; and two strong judges scoring *one
posting at a time* agreed within one band 99% of the time. The instability lives in the batch,
not in the scale.

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

## The background field, at the paid stage: it depends on the objectives

Same candidate set for both arms — the `nobg` pool — so this isolates what the background does to
the *scoring*, with the retrieval difference held out. Three repeats per arm, because a single
run's nDCG@20 sits inside the noise floor measured above.

| objectives the profile carries | mean Δ nDCG@20 from adding the background | run-to-run SD |
|---|---|---|
| **full**, as the personas were written | **+0.003** | 0.016 |
| **thin**, cut to the clauses stating a wish | **+0.051** | 0.031 |

| persona | thin, without | thin, with | Δ |
|---|---|---|---|
| `wing_nachhaltigkeit` | 0.912 | 0.908 | −0.005 |
| `backend_java` | 0.609 | 0.713 | **+0.103** |
| `maschinenbau_zu_daten` | 0.872 | 0.925 | **+0.053** |

**The first row is a confounded test and the second is the real one.** These personas' objectives
paragraphs double as potted CVs — *"I build backend services in Java with Spring Boot and
MongoDB… I want a small product company"* — so adding a background to them adds almost nothing
that was not already there, and measures almost nothing. Cut the objectives back to the wish and
the picture changes: `backend_java` collapses from 0.829 to 0.609 without capability evidence and
recovers most of the way, to 0.713, once the background supplies it.

So the field earns its place, but not for the reason brief 04 gave. It does nothing for a user
who has already written their history into the objectives box, and it does real work for one who
has written only what they want — which is what that box actually invites, and what a career
changer is most likely to type.

## What to change, and what to leave alone

| brief 02 asked | answer | evidence |
|---|---|---|
| Is the score stable? | **No, where it matters.** The ranking correlates at 0.95 between identical runs, but a quarter to a third of the top 20 — the part anyone reads — differs. | 5 repeats × 150 postings × 3 personas |
| Scoring form? | **Leave it.** Absolute 0–100 and four bands are indistinguishable, and the model already bands its own answers into ~15 values. | 0.870 vs 0.868 mean nDCG@20 |
| Batch size? | **Change it to 1.** +0.07 to +0.09 nDCG@20, one third of a cent per run, fewer postings lost to bad JSON, and it is the only setting where the two biases cannot exist. | 4 sizes, replicated, plus the two bias experiments |
| `rerank_limit`? | **Leave it at 150, and stop calling it a cost setting.** Quality has not run out at 150 and the money is not the constraint; it is a reading budget. | judged grades along the retrieval ranking to depth 200 |

The single sentence: **the reranker's instability is not the model being vague about numbers, it
is that the unit of judgement is the batch rather than the posting.** Position within a batch is
worth 8–15 points and the company a posting keeps is worth 8–39, against 5 points of genuine
run-to-run noise — and both of the large effects are artifacts of the batch, and both correlate
with retrieval rank rather than washing out, because batches are filled in retrieval order.

The counter-evidence that makes this more than a story: two strong models from different families,
each scoring **one posting per call**, agreed within one band on 99% of 279 postings. The task is
not ambiguous when the unit is a posting.

| brief 04 asked | answer |
|---|---|
| Does the background help expansion? | **No.** Identical needle-tier coverage, MRR 0.160 → 0.151, and judged precision flat to slightly worse. `MAX_QUERIES = 8` truncates most of its effect away before retrieval sees it. |
| Does it help reranking? | **Only when the objectives do not already contain it** — +0.003 with the personas' CV-like objectives, +0.051 when they are cut to the wish. |
| Should the field ship? | **Yes**, on the strength of the second row, and because a real user's objectives box is far likelier to hold aspiration than a career summary. Its cost is one call per profile version and ~150 tokens per batch. |

What would change these answers: three personas is a small sample, and every number here is a gap
between configurations measured against one judge's opinion, not against a user's. The judged set
is stored and reusable, so the next change to this stage costs nothing to evaluate.

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
