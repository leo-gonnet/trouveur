"""Guards for retrieval fusion, the rules cut and the paid stage's cost controls."""

from __future__ import annotations

from decimal import Decimal

from trouveur.match.expand import combine, deterministic_queries, parse_response
from trouveur.match.fuse import reciprocal_rank_fusion
from trouveur.match.rerank import parse_response as parse_scores
from trouveur.match.rerank import would_exceed_budget
from trouveur.match.rules import evaluate
from trouveur.models import Candidate, RuleVerdict, UserProfile


def _candidate(**kwargs) -> Candidate:
    base = {"job_id": 1, "content_hash": b"x", "title": "Wirtschaftsingenieur"}
    return Candidate(**{**base, **kwargs})


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


def test_rules_reject_role_types_by_title_only():
    profile = UserProfile(user_id=1)
    assert evaluate(_candidate(title="Werkstudent Logistik"), profile)[0] is RuleVerdict.REJECT
    # A senior advert merely mentioning Werkstudenten is not itself a Werkstudent role.
    survivor = _candidate(
        title="Teamleiter Produktion", description="Sie betreuen unsere Werkstudenten."
    )
    assert evaluate(survivor, profile)[0] is RuleVerdict.PASS


def test_deal_breakers_see_the_company_but_role_matching_does_not():
    """Staffing agencies are caught by name, which is why deal-breakers read the company field."""
    profile = UserProfile(user_id=1, deal_breakers=["Zeitarbeit"])
    caught = _candidate(title="Ingenieur", company="ACME Zeitarbeit GmbH")
    assert evaluate(caught, profile)[0] is RuleVerdict.REJECT


def test_rules_pass_when_signals_are_missing():
    """The bias is explicit: a false pass costs a fraction of a cent; a false reject is unseen."""
    profile = UserProfile(user_id=1, deal_breakers=["Zeitarbeit"])
    bare = _candidate(title="Engineer", company=None, description=None)
    assert evaluate(bare, profile)[0] is RuleVerdict.PASS


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


def test_a_staffing_agency_is_rejected_by_the_source_flag_not_by_prose():
    """The evaluation harness found this: is_agency was derived and then never read.

    Every persona whose deal-breakers happened to include the exact word was protected, and every
    persona whose did not sent staffing placements to a paid reranker and then to the user. An
    advert can be a placement without ever printing "Zeitarbeit", which is why the structured flag
    exists in the first place.
    """
    profile = UserProfile(user_id=1, deal_breakers=[])
    flagged = _candidate(title="Wirtschaftsingenieur (m/w/d)", company="Vermittlung GmbH",
                         description="Eine spannende Aufgabe bei unserem Kunden.", is_agency=True)
    verdict, reason = evaluate(flagged, profile)
    assert verdict is RuleVerdict.REJECT
    assert "agency" in reason


def test_an_unknown_agency_flag_does_not_reject():
    """None means no detail has arrived yet. Absence of evidence is not evidence."""
    profile = UserProfile(user_id=1)
    unknown = _candidate(title="Wirtschaftsingenieur (m/w/d)", is_agency=None)
    assert evaluate(unknown, profile)[0] is RuleVerdict.PASS
    known_good = _candidate(title="Wirtschaftsingenieur (m/w/d)", is_agency=False)
    assert evaluate(known_good, profile)[0] is RuleVerdict.PASS
