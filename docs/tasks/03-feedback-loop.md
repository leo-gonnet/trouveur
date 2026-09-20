# 03 — Close the feedback loop

## The problem

`user_job_match` already records what each user did with each posting — its `state` column and
`state_changed_at` carry dismissals and whatever else the UI offers. That is labelled relevance
data, produced by the only person whose opinion matters, and nothing reads it back.

Every other item on the roadmap makes the system better once. This one makes it better every
week it is used, and it is the difference between a static filter and a product.

## What to do

Build it in the cheapest order, and stop as soon as it works.

1. **Surface the signal first.** Establish what is actually recorded, how much of it there is per
   user, and how noisy it is. A design that assumes plentiful labels will fail on a user with
   eleven dismissals. Check whether the current UI even offers enough actions to generate signal;
   if it does not, that is the first change, and it should stay minimal — an explicit "not for me"
   is worth more than an elaborate rating scheme nobody uses.
2. **Use it in the prompt before using it in a model.** The reranker already receives a profile.
   Giving it a short, current list of what this user has rejected — titles and the reason if one
   exists — is a few lines of prompt and needs no training, no new storage and no new failure
   mode. Measure it against the judged set from brief 02 before keeping it.
3. **Only then consider anything learned.** A per-user relevance model over the existing features
   is possible, but it is the third option, not the first, and it should be justified by the
   prompt approach falling short.

Two design constraints worth deciding early:

- **Dismissals are not all negatives.** "Wrong location", "already applied" and "bad role" are
  different signals and collapsing them loses the useful one. If the UI can capture a cheap
  reason, it is worth far more than the bare dismissal.
- **Feedback must not silently override the profile.** The user's stated objectives are what they
  asked for; learned preferences are inferred. When they conflict, the stated one should win, or
  the system becomes unpredictable in a way the user cannot correct.

## Discovery clues

- `trouveur/db/schema.py`, `user_job_match` — what is recorded today, and the comment on
  `upsert_matches` about why user history is never overwritten by retrieval.
- `trouveur/db/queries/match.py` — the state transitions and the pages that read them.
- `trouveur/web/` — where the actions are offered.
- `trouveur/match/rerank.py` — where profile text enters the prompt.
- `trouveur/db/queries/users.py`, `reset_scores` — what a profile edit already invalidates, and
  why history deliberately survives it.

## Watch out for

- Re-scoring cost. Anything that changes what the reranker sees invalidates cached scores. Decide
  deliberately whether feedback bumps the profile version — bumping it on every dismissal would
  bill the user for every click.
- Feedback loops that narrow. A system trained only on what the user already liked stops showing
  them the adjacent roles that query expansion exists to surface. Keep a share of exploration, and
  measure recall on the adjacent-role tier before and after.

## Done when

User actions demonstrably change what the next run shows, the change is measured against the
judged set rather than asserted, and a user with almost no history is no worse off than today.
