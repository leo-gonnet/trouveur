"""Guards for retrieval fusion and the paid stage's cost controls."""

from __future__ import annotations

from decimal import Decimal

from trouveur.db.queries.users import SCORING_FIELDS
from trouveur.match.expand import build_prompt as expand_prompt
from trouveur.match.expand import combine, deterministic_queries, parse_response
from trouveur.match.fuse import reciprocal_rank_fusion
from trouveur.match.rerank import build_prompt as rerank_prompt
from trouveur.match.rerank import parse_response as parse_scores
from trouveur.match.rerank import would_exceed_budget
from trouveur.models import UserProfile


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


def test_budget_is_checked_before_a_batch_not_after():
    assert would_exceed_budget(Decimal("1.00"), Decimal("5.00"), Decimal("0.05")) is False
    assert would_exceed_budget(Decimal("4.99"), Decimal("5.00"), Decimal("0.05")) is True


def test_zero_budget_blocks_every_call():
    # A user who has not set a ceiling must not be charged by default.
    assert would_exceed_budget(Decimal(0), Decimal(0), Decimal(0)) is True


def test_malformed_score_response_yields_nothing_never_zero():
    """Scoring garbage as 0 would cache a wrong verdict and hide good jobs permanently."""
    assert parse_scores("I'm sorry, I cannot help with that.") == []
    assert parse_scores("") == []
    assert parse_scores("[{broken json") == []


def test_out_of_range_scores_are_discarded_not_clamped():
    # Clamping would invent a score the model never gave.
    assert parse_scores('[{"id": 1, "score": 500}]') == []
    assert [score.score for score in parse_scores('[{"id": 1, "score": 88}]')] == [88]


def test_partial_batch_keeps_the_entries_that_parsed():
    scores = parse_scores('[{"id": 1, "score": 90}, {"id": 2, "score": "bad"}]')
    assert [score.id for score in scores] == [1]


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
    identical in a diff and multiply a 4,000-character CV across every batch of ten postings --
    fifteen times over at the default rerank_limit, on the user's own card.
    """
    profile = UserProfile(user_id=1, title="Wirtschaftsingenieur", background="RAW CV " * 500)
    prompt = rerank_prompt(profile, [], background="LCA, ISO 14001, circular economy")

    assert "LCA, ISO 14001, circular economy" in prompt
    assert "RAW CV" not in prompt


def test_the_rerank_prompt_says_unstated_rather_than_omitting_the_background():
    """An empty line the model has to interpret is worse than a stated absence, and the rest of
    the profile block already uses this word for it."""
    prompt = rerank_prompt(UserProfile(user_id=1, title="x"), [])
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
