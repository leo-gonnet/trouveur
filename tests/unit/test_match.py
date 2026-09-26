"""Guards for retrieval fusion and the paid stage's cost controls."""

from __future__ import annotations

from decimal import Decimal

from trouveur.db.queries.users import SCORING_FIELDS
from trouveur.match.expand import build_prompt as expand_prompt
from trouveur.match.expand import combine, deterministic_queries, parse_response
from trouveur.match.fuse import reciprocal_rank_fusion
from trouveur.match.rerank import build_prompt as rerank_prompt
from trouveur.match.rerank import parse_score, would_exceed_budget
from trouveur.models import UserProfile


class _Advert:
    """The columns `pending_rerank` selects, as the prompt builder reads them."""

    job_id = 1
    title = "Nachhaltigkeitsmanager"
    company = "Beispiel GmbH"
    description = "Wir suchen..."
    locations = [{"raw": "Wien"}]
    salary_min_eur_year = None
    salary_max_eur_year = None
    work_mode = "hybrid"


def test_fusion_rewards_agreement_between_retrievers():
    """A posting both retrievers rank highly must beat one only a single retriever found.

    This is the whole reason for running two: dense recall and lexical recall each miss things the
    other catches, and agreement is the strongest signal available before anything is scored.
    """
    dense = [10, 20, 30]
    lexical = [30, 40, 50]
    ranked = [job_id for job_id, _ in reciprocal_rank_fusion([dense, lexical])]
    assert ranked[0] == 30
    assert ranked.index(30) < ranked.index(20)


def test_fusion_keeps_results_found_by_only_one_retriever():
    # Dropping single-source hits would discard exactly the recall the second retriever exists for.
    fused = dict(reciprocal_rank_fusion([[1, 2], [3, 4]]))
    assert set(fused) == {1, 2, 3, 4}


def test_fusion_of_nothing_is_empty():
    assert reciprocal_rank_fusion([]) == []
    assert reciprocal_rank_fusion([[], []]) == []


def test_budget_is_checked_before_a_call_not_after():
    assert would_exceed_budget(Decimal("1.00"), Decimal("5.00"), Decimal("0.05")) is False
    assert would_exceed_budget(Decimal("4.99"), Decimal("5.00"), Decimal("0.05")) is True


def test_zero_budget_blocks_every_call():
    # A user who has not set a ceiling must not be charged by default.
    assert would_exceed_budget(Decimal(0), Decimal(0), Decimal(0)) is True


def test_malformed_score_response_yields_nothing_never_zero():
    """Scoring garbage as 0 would cache a wrong verdict and hide good jobs permanently."""
    assert parse_score("I'm sorry, I cannot help with that.") is None
    assert parse_score("") is None
    assert parse_score("{broken json") is None


def test_out_of_range_scores_are_discarded_not_clamped():
    # Clamping would invent a score the model never gave.
    assert parse_score('{"score": 500}') is None
    assert parse_score('{"score": 88}').score == 88


def test_the_prompt_carries_exactly_one_advert():
    """Scoring is one posting per call, and that is a quality decision, not a cost one.

    With ten in a prompt, a posting's score moved with where it sat in the list -- slot 0 ran 8-15
    points above slot 9 -- and batches were filled in retrieval order, so the bias did not wash
    out. Anything that puts a second advert back in this prompt reintroduces that silently.
    """
    prompt = rerank_prompt(UserProfile(user_id=1, title="x"), _Advert())
    assert prompt.count("description:") == 1
    assert "JOB ADVERT" in prompt
    # No id to key a score on, because there is only ever one posting to key it to.
    assert "id=" not in prompt


def test_the_profile_block_comes_before_the_advert():
    """Identical across every call of a run, so a provider that caches prompt prefixes gets it
    free. Advert first would make each call a fresh prefix and cache nothing."""
    prompt = rerank_prompt(UserProfile(user_id=1, title="x"), _Advert())
    assert prompt.index("CANDIDATE PROFILE") < prompt.index("JOB ADVERT")


def test_expansion_works_without_a_model():
    """Retrieval must be fully functional for a user who has set no API key."""
    profile = UserProfile(
        user_id=1, title="Wirtschaftsingenieur", keywords=["Prozessoptimierung"]
    )
    queries = deterministic_queries(profile)
    assert "Wirtschaftsingenieur" in queries
    assert "Prozessoptimierung" in queries


def test_expansion_falls_back_when_the_model_returns_junk():
    assert parse_response("no json here") == []


def test_combine_deduplicates_case_insensitively_and_keeps_user_words_first():
    combined = combine(["Wirtschaftsingenieur"], ["wirtschaftsingenieur", "Industrial Engineer"])
    assert combined[0] == "Wirtschaftsingenieur"
    assert combined == ["Wirtschaftsingenieur", "Industrial Engineer"]


def test_the_reranker_reads_the_summarised_background_not_the_raw_field():
    """The cost design of the background field, in one assertion.

    `background` is distilled once per profile version and passed in; the raw field on the
    profile is never sent to this prompt. Reading `profile.background` here instead would look
    identical in a diff and send a 4,000-character CV once per posting scored -- thousands of times
    in a run, on the user's own card.
    """
    profile = UserProfile(user_id=1, title="Wirtschaftsingenieur", background="RAW CV " * 500)
    prompt = rerank_prompt(profile, _Advert(), background="LCA, ISO 14001, circular economy")

    assert "LCA, ISO 14001, circular economy" in prompt
    assert "RAW CV" not in prompt


def test_the_rerank_prompt_says_unstated_rather_than_omitting_the_background():
    """An empty line the model has to interpret is worse than a stated absence, and the rest of
    the profile block already uses this word for it."""
    prompt = rerank_prompt(UserProfile(user_id=1, title="x"), _Advert())
    assert "background: unstated" in prompt


def test_expansion_shows_the_generators_the_whole_background():
    """The opposite trade from the reranker's, and deliberately so: expansion is billed once per
    profile version, and it is the stage that was measured writing adverts for the role the
    candidate already has when it had nothing but a title to go on."""
    profile = UserProfile(user_id=1, title="Wirtschaftsingenieur", background="Ökobilanzierung "
                          "für Investitionsgüter, Traineeprogramm Bau, ISO 14040.")
    prompt = expand_prompt(profile)
    assert "ISO 14040" in prompt


def test_the_background_is_a_scoring_field():
    """Every field on the Profile page changes what a good match is. Leaving this one out of the
    set would let a user rewrite their history and keep scores computed without it -- and the
    page promises the opposite."""
    assert "background" in SCORING_FIELDS
