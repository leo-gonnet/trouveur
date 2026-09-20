# 08 — Strengthen the evaluation

## The problem

The retrieval evaluation plants hand-written needles into a real haystack and measures recall by
construction. The design is sound; the instrument is too small and partly stale.

- **Under-powered.** 21 planted positives across 3 personas. One needle is 4.8 points, so any two
  close configurations are indistinguishable, and several published comparisons rest on
  differences of one or two needles.
- **Stale baseline.** The committed baseline was measured over roughly 4,000 postings. The live
  corpus is 229,000. The harness already refuses to difference runs whose corpus sizes differ by
  more than a tolerance, which means the baseline currently reports nothing.
- **Shipped path is not the measured path.** The 2026-09 study added per-arm query routing —
  adverts to the dense arm only — in a separate `evalx/` layer. Production now does that, the
  harness does not.
- **Saturation.** On the old small corpus recall had saturated, so the only movement it could
  report was a regression. Rank-based metrics were added for this reason; make sure they are
  what regressions are judged on.

## What to do

1. **Grow the needle set**, keeping the tier design — needles are tiered by which retriever
   *should* find them, which is what makes the aggregate interpretable. New needles should be
   written against real personas, ideally derived from actual profiles rather than invented.
2. **Fold the routing into the harness** so the evaluated path is the shipped one, and retire
   whatever in `evalx/` is then duplicated.
3. **Re-baseline against a pinned corpus snapshot.** Two runs are only comparable over comparable
   corpora; a snapshot that can be restored makes a run reproducible months later.
4. **Keep precision honest.** Planted needles cannot measure precision, and the harness says so.
   Pooled judging (brief 02) is the instrument for that; keep the two clearly separated rather
   than blending them into one score.

## Discovery clues

- `trouveur/eval/harness.py` — the whole design, including the docstrings explaining what it can
  and cannot measure, the tier definitions, and `incomparable()`.
- `trouveur/eval/data/needles.json`, `personas.json` — the instrument itself.
- `evalx/` and `evalx/FINDINGS.md` — the routing layer and the study that produced it.
- `trouveur/cli.py`, the `eval` command and its `--sweep` and `--save-baseline` flags.

## Watch out for

- The evaluation reports, it never gates a merge. That is an explicit architectural rule; do not
  turn it into a test.
- Needles must be planted through the real ingest path, not inserted into tables, or the
  evaluation measures rows the pipeline could never produce.
- Reranker scores are not reproducible between runs; never act on a single run's "lost" list.

## Done when

The needle set is large enough that a one-needle difference is not a headline, the harness
exercises the routed path, and a fresh baseline exists against a snapshot that can be restored.
