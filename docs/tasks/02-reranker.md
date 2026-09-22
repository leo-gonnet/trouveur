# 02 — Measure and improve the reranker

> **Answered 2026-09-22** — see [`evalx/FINDINGS-RERANKER.md`](../../evalx/FINDINGS-RERANKER.md).
> A judged reference set of 792 postings exists and is reusable. The instability is real but is
> an artifact of **batching**, not of the scoring form: position within a batch is worth 8-15
> points and the company a posting keeps 8-39, against 5 points of run-to-run noise. Verdicts:
> batch size **1** (not 10), scoring form **unchanged**, `rerank_limit` **unchanged at 150** and
> reclassified as a reading budget. The batch change needs the paid loop made concurrent first,
> which is left as its own piece of work.

## The problem

The reranker decides what the user actually reads: everything it scores is shown, ordered by that
score, with no threshold. It is also the only stage that costs money, and it is the least
measured thing in the system.

Today it is graded only against planted needles — the harness scores the ~30 hand-written needles
for a persona and counts how often a planted negative outranks a planted positive. That answers
"does it invert the obvious cases" and nothing about the 150 real postings a user sees. Worse,
the scores are known not to be reproducible: the same needle has scored 45 on one run and ≥70 on
the next with the provider pinned and temperature at zero. If the score is not stable, neither is
the page order.

## What to do

Three separable pieces. The first is a prerequisite for the others.

**Build a judged reference set, once.** Planted needles cannot measure precision — an unplanted
posting ranking first is unjudged, not wrong. The standard construction is pooling: take the
union of the top results across several configurations, judge each distinct posting exactly once,
and score every configuration against that one set. A study in `evalx/judge.py` already does this
with the production reranker as judge; the upgrade is to judge with a *stronger* model than the
one in production, so the production model can be measured against it rather than against itself.
Store the judgements so they are reusable and the cost is paid once.

**Then measure the things nobody has checked:**

- *Stability.* Score the same postings several times and quantify the spread. If it is large,
  that is a product bug, not a curiosity — it means two runs disagree about what the user should
  read first.
- *Scoring form.* Absolute 0–100 scoring is what drifts. Pairwise or listwise ranking is usually
  steadier. Test whether a different form gives the same ordering more reproducibly at similar
  cost.
- *Batching.* Postings are scored ten at a time in one prompt. Check for position bias within a
  batch and for contamination between postings sharing a prompt — both are known failure modes
  of batched LLM scoring, and both are invisible without an experiment.
- *Depth.* `rerank_limit` is 150. It sets the bill and the page length simultaneously and has
  never been justified. Find where quality saturates.

**Then improve what the measurement says is worth improving**, which may be the prompt, the
scoring form, the batch size, or the model choice. Do not tune all four at once.

## Discovery clues

- `trouveur/match/rerank.py` — all prompt text lives here by design; do not scatter it.
- `trouveur/match/llm.py` — transport, provider pinning, cost metering.
- `trouveur/models/profile.py` — `rerank_limit` and what it governs.
- `evalx/judge.py` — the pooling construction, and its stated biases.
- `trouveur/eval/harness.py`, `grade_scores` — the existing needle-based grading.

## Watch out for

- Spending someone's money. The evaluation key is read from the environment and is deliberately
  never a stored user credential. Keep it that way.
- Judging with the same model family you are measuring invites circularity. The judge scores a
  posting against a profile and never sees the query, which limits it, but say so plainly.
- Caching. A score is cached per (content hash, user, profile version); an experiment that reuses
  a user will silently read cache rather than the model.

## Done when

There is a stored judged set, a reproducible way to score a configuration against it, a number
for score stability, and a decision — with evidence — on scoring form, batch size and
`rerank_limit`. Findings written down beside the retrieval ones.
