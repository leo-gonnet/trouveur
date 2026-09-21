# 04 — Enrich the profile input

## The problem

A profile is a current title, years of experience, a free-text objectives paragraph, languages,
must-haves and keywords. That is enough to say what someone wants and almost nothing about what
they have done.

Two stages pay for this. Query expansion has to invent a plausible next role from a job title —
and when it was measured, the pinned model wrote adverts describing *the role the candidate
already has*, which is exactly wrong for a career-change profile. The reranker has to judge fit
against a paragraph of aspiration with no evidence of capability behind it.

A single free-text background field — a CV summary in the user's own words — feeds both, and
costs a column and a form field.

## What to do

Add a background/experience free-text field to the profile, make it a *scoring* field so editing
it invalidates cached scores and triggers re-expansion like the other substantive fields, and
feed it into both the expansion prompt and the rerank prompt.

Then measure whether it helped, because "more context is better" is an assumption:

- For expansion, the question is whether generated adverts become more specific and better
  targeted, which the retrieval evaluation can answer through the adjacent-role tiers.
- For reranking, the question is whether the judged ordering improves (see brief 02).

Consider the failure mode before building: a long CV pasted wholesale will dominate a prompt and
may push the objectives out of the model's attention. Decide on a length budget, and decide
whether the field is summarised once at save time rather than sent raw on every call — the latter
is cheaper and more stable, since it is one cost per profile version rather than per batch.

## Discovery clues

- `trouveur/models/profile.py` — the profile model, and what the scoring-field set governs.
- `trouveur/db/queries/users.py`, `save_profile` — how a scoring field bumps the version and
  resets cached scores. Getting this set wrong is, in the code's own words, "a surprise invoice".
- `trouveur/match/expand.py`, `build_prompt` — what expansion currently sees.
- `trouveur/match/rerank.py`, `build_prompt` — what the reranker currently sees.
- `trouveur/web/` — the profile form and its validation.
- Migration examples in `alembic/versions/`.

## Watch out for

- Privacy. This field will contain the most personal text in the system and it leaves the host on
  every model call. Check how the provider pin's `data_collection` constraint is set and make
  sure it still applies.
- Cost. Adding text to the rerank prompt multiplies across every scored posting; adding it to the
  expansion prompt costs once per profile version. Prefer the latter where you can.

## Done when

The field exists end to end, both prompts use it, the version-bump behaviour is correct, and
there is a measurement showing whether it changed anything.
