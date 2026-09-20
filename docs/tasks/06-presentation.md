# 06 — Reshape how results are presented

## The problem

There is no score threshold: everything the reranker scores is shown, ordered by score. With
`rerank_limit` at 150 that is a 150-row list with no shape, in which the difference between a
posting scoring 91 and one scoring 43 is a vertical position and nothing else.

The reranker already produces a one-sentence reason and a list of red flags for every posting,
and the retrieval layer already knows which arm found a posting and at what rank. Almost none of
that reaches the user, so a bad recommendation is diagnosable by the maintainer and not by the
person reading it.

## What to do

This is a product change; treat it as one, and do not add a score cut-off by the back door — the
decision to show everything scored was deliberate and the alternative is shape, not truncation.

- **Band the results by what the score means.** The rerank prompt already defines its own bands:
  excellent / good / plausible but compromised / poor. Those are the bands the page should use,
  because they are what the number was produced against. A reader can then stop at a boundary
  instead of scrolling until interest fades.
- **Show the reason.** It is already generated, stored and paid for.
- **Show the red flags** — the prompt asks for them explicitly and they are the fastest way for a
  user to reject a posting without opening it.
- **Consider showing why it was retrieved** — the matched query, or simply whether it came from
  the user's own words or from expansion. This is the difference between "the system is wrong"
  and "my profile says something I did not mean".
- **Make dismissal cheap and reasoned**, because brief 03 depends on that signal existing.

Worth checking while in here: what the page does when a run is partial, when scoring stopped on
budget, or when a user has no key at all — the last of these is supported by design and should
read as a working search rather than a broken one.

## Discovery clues

- `trouveur/web/` — templates and routes for the recommendations page.
- `trouveur/match/rerank.py` — the score bands in the prompt, and the reason and red-flag fields.
- `trouveur/db/schema.py`, `user_job_match` — `llm_score`, `llm_reason`, `llm_red_flags`, `state`.
- `trouveur/db/queries/match.py` — the queries behind the page, including how ordering is done.
- `trouveur/models/profile.py` — `rerank_limit`, which is simultaneously the bill and the page
  length; brief 02 questions the number itself.

## Watch out for

- Do not introduce a threshold that silently hides paid-for results. Banding is presentation;
  hiding is a policy change and needs its own decision.
- The digest email and the page should agree about what is worth reading. Changing one without
  the other splits the product in two.

## Done when

The page has shape, every row explains itself, and dismissing something is one click with an
optional reason.
