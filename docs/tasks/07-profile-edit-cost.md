# 07 — Correct the profile-edit message

## The problem

There was a proposal to warn users that editing their profile would "erase their hidden
expansion data". It would not: query expansions are keyed by `(user_id, profile_version,
expansion_version)`, so editing a profile writes a *new* row and erases nothing.

What an edit to a scoring field actually does is clear every cached LLM score for that user, so
the next match re-scores the shortlist from scratch on the user's own key. That is a real cost,
it is invisible today, and it is the thing worth telling them about.

Note the asymmetry the code is careful about: scores are cleared, but `state` and `notified_at`
survive — un-dismissing a job or re-sending a digest entry because a profile changed would be
the user's history lost. Any message must not imply otherwise.

## What to do

Replace the intent of the warning. On the profile form, when the user is about to change a field
that bumps the version, tell them plainly that their scores will be recomputed on the next match
and roughly what that will cost, based on what their last run actually cost. Say that their saved
and dismissed jobs are unaffected.

Two things to get right:

- Only scoring fields bump the version. Editing something cosmetic should say nothing, or the
  warning becomes noise and is ignored when it matters.
- The estimate should come from recorded spend rather than a guess — per-user spend is already
  tracked.

## Discovery clues

- `trouveur/db/queries/users.py` — `save_profile`, the scoring-field set, and `reset_scores` with
  the comment explaining exactly what survives an edit and why.
- `trouveur/db/schema.py` — `user_query_expansion` and its composite key; `user_llm_spend`.
- `trouveur/match/pipeline.py` — where expansion is cached and reused per profile version.
- `trouveur/web/` — the profile form.

## Done when

Editing a scoring field warns about recomputation cost with a real estimate; editing anything
else is silent; and nothing in the wording suggests data is lost.
