# 09 — Simplify what no longer earns its place

## The problem

Three things in the retrieval path are carried on assumptions that measurement has weakened.

**The hybrid may not be earning its cost.** An earlier run found fused recall of 18/21 against
dense alone at 17/21 — the lexical arm contributed almost exclusively on the tier that shares
vocabulary with the profile, which dense nearly finds anyway. If the encoder improves (brief 01),
the case gets weaker still. This is a number to re-measure, not a debate to re-open.

**The trigram path cannot see descriptions.** The unaccented fold column is built from title,
company and location only, so the `LIKE` half of the lexical query never matches inside a
posting's body — measured: an exact token that matched 544 postings through the tsvector matched
18 through the fold. That may be the right trade (the column and its index are large) but it is
currently an accident of the schema rather than a documented decision.

**The description budget is fiction.** The embedding text takes 1,200 characters of description,
and the encoder reads about 128 tokens — roughly 2.4× less. The constant states an intent the
system does not carry out.

## What to do

Measure first, then remove; and do the encoder swap first, because it changes the answer to the
first question.

- Re-run the per-arm recall comparison on the current corpus with the current encoder. If the
  lexical arm still only contributes on the shared-vocabulary tier, consider what a simpler
  system would look like — but weigh that against what the lexical arm is *for*: exact rare
  tokens that a vector smears into its nearest common concept. Recall alone may not capture that.
- Decide the fold column's scope deliberately. Either extend it to descriptions and accept the
  index cost, or document why it covers only the header fields.
- Make the description budget honest — either size it to what the model actually reads, or let a
  longer-context model make it moot, and say which in the constant's comment.
- Review whether the `websearch_to_tsquery` conjunction semantics suit multi-word user queries.
  It ANDs every term, which is why long inputs match nothing; short user phrases are fine, but
  the behaviour should be a choice.
- Consider German compound handling. The tsvector uses the `german` configuration, which stems
  but does not decompound, so "Ingenieur" inside "Wirtschaftsingenieur" is invisible to it. The
  trigram path exists to cover that gap; check it actually does.

## Discovery clues

- `trouveur/db/queries/match.py` — both lexical paths in one query, with the comment explaining
  why neither alone is sufficient on German text.
- `trouveur/match/fuse.py` — why fusion is by rank and not by score.
- `trouveur/ingest/embed/text.py` — the description budget and the reasoning behind the field
  order.
- `trouveur/eval/harness.py` — it already scores each arm separately for exactly this question.
- `evalx/FINDINGS.md` — the measurements quoted above.

## Done when

Each of the three has either a measurement justifying it as it stands, a change, or a comment
recording the decision and its evidence.
