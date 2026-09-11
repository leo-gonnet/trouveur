"""Guards on the evaluation harness itself.

An evaluation that quietly measures the wrong thing is worse than none, because its number gets
quoted. These check the properties that make the score mean what it claims, without needing a
corpus or a model.
"""

from __future__ import annotations

from trouveur.eval import harness


def test_scoring_counts_only_the_persona_s_own_needles():
    """A needle belonging to another persona must never count toward this one's recall."""
    needles = [
        {"id": "a", "persona": "p1", "tier": "T1"},
        {"id": "b", "persona": "p1", "tier": "T2"},
    ]
    planted = {"a": 10, "b": 20}
    scored = harness._score_arm([10], planted, needles)
    assert scored["T1"] == {"found": 1, "total": 1}
    assert scored["T2"] == {"found": 0, "total": 1}


def test_a_needle_that_never_reached_the_corpus_is_not_counted_as_present():
    """It must not silently inflate the denominator or the numerator.

    A needle the pipeline rejected is a bug to investigate, not a miss to average away.
    """
    needles = [
        {"id": "a", "persona": "p1", "tier": "T1"},
        {"id": "gone", "persona": "p1", "tier": "T1"},
    ]
    scored = harness._score_arm([10], {"a": 10}, needles)
    assert scored["T1"] == {"found": 1, "total": 1}


def test_negative_tier_is_never_scored_as_recall():
    """Finding a negative is a failure, so it must not appear in any recall figure."""
    needles = [{"id": "n", "persona": "p1", "tier": "N"}]
    scored = harness._score_arm([10], {"n": 10}, needles)
    assert set(scored) == {"T1", "T2", "T3"}
    assert all(tier["total"] == 0 for tier in scored.values())


def test_regressions_reports_only_drops():
    card = harness.Scorecard(limit=200, embedding_version="v", corpus_open=1)
    card.personas.append(
        harness.PersonaResult(
            persona="p1",
            recall={"fused": {
                "T1": {"found": 2, "total": 2},
                "T2": {"found": 1, "total": 2},
                "T3": {"found": 2, "total": 2},
            }},
        )
    )
    baseline = {"personas": [{"persona": "p1", "recall": {"fused": {
        "T1": {"found": 2, "total": 2},
        "T2": {"found": 2, "total": 2},
        "T3": {"found": 1, "total": 2},
    }}}]}
    found = harness.regressions(card, baseline)
    assert len(found) == 1 and "T2" in found[0]


def test_no_baseline_is_not_a_regression():
    card = harness.Scorecard(limit=200, embedding_version="v", corpus_open=1)
    assert harness.regressions(card, None) == []


def test_haystack_partitions_are_relevant_to_the_personas():
    """A haystack of unrelated work is removed by the hard filters for free and flatters recall."""
    assert harness.HAYSTACK_PARTITIONS
    assert not {"Verkauf", "Lagerwirtschaft", "Altenpflege"} & set(harness.HAYSTACK_PARTITIONS)


def test_a_needle_sliding_down_the_ranking_is_reported_though_recall_is_unchanged():
    """The reason ranks are recorded: recall saturates and then reports nothing but regressions.

    Both runs find the needle inside k, so every recall figure is identical. Falling from rank 3
    to rank 60 is still a real loss -- the reranker's budget is far smaller than k.
    """
    unchanged = {"T1": {"found": 1, "total": 1},
                 "T2": {"found": 0, "total": 0},
                 "T3": {"found": 0, "total": 0}}
    card = harness.Scorecard(limit=200, embedding_version="v", corpus_open=1)
    card.personas.append(
        harness.PersonaResult(persona="p1", recall={"fused": unchanged}, ranks={"a": 60})
    )
    baseline = {"personas": [{"persona": "p1", "recall": {"fused": unchanged}, "ranks": {"a": 3}}]}

    found = harness.regressions(card, baseline)
    assert len(found) == 1 and "rank 3 -> 60" in found[0]


def test_a_needle_holding_its_rank_is_not_a_regression():
    unchanged = {"T1": {"found": 1, "total": 1},
                 "T2": {"found": 0, "total": 0},
                 "T3": {"found": 0, "total": 0}}
    card = harness.Scorecard(limit=200, embedding_version="v", corpus_open=1)
    card.personas.append(
        harness.PersonaResult(persona="p1", recall={"fused": unchanged}, ranks={"a": 5})
    )
    baseline = {"personas": [{"persona": "p1", "recall": {"fused": unchanged}, "ranks": {"a": 3}}]}

    assert harness.regressions(card, baseline) == []


def test_a_baseline_over_a_different_sized_corpus_is_not_differenced():
    """Recall depends on how much the needle had to beat, so sizes must match to subtract.

    The run that found this compared 75,259 postings against a 4,110-posting baseline and
    reported two regressions that were only a bigger haystack.
    """
    card = harness.Scorecard(limit=200, embedding_version="v", corpus_open=75_259)
    card.personas.append(
        harness.PersonaResult(
            persona="p1",
            recall={"fused": {"T1": {"found": 1, "total": 2},
                              "T2": {"found": 0, "total": 0},
                              "T3": {"found": 0, "total": 0}}},
        )
    )
    baseline = {"corpus_open": 4_110, "personas": [{"persona": "p1", "recall": {"fused": {
        "T1": {"found": 2, "total": 2},
        "T2": {"found": 0, "total": 0},
        "T3": {"found": 0, "total": 0},
    }}}]}

    found = harness.regressions(card, baseline)
    assert len(found) == 1 and "not compared" in found[0]


def test_a_real_drop_at_a_comparable_corpus_size_is_still_reported():
    card = harness.Scorecard(limit=200, embedding_version="v", corpus_open=4_200)
    card.personas.append(
        harness.PersonaResult(
            persona="p1",
            recall={"fused": {"T1": {"found": 1, "total": 2},
                              "T2": {"found": 0, "total": 0},
                              "T3": {"found": 0, "total": 0}}},
        )
    )
    baseline = {"corpus_open": 4_110, "personas": [{"persona": "p1", "recall": {"fused": {
        "T1": {"found": 2, "total": 2},
        "T2": {"found": 0, "total": 0},
        "T3": {"found": 0, "total": 0},
    }}}]}

    found = harness.regressions(card, baseline)
    assert len(found) == 1 and "T1" in found[0]


def test_a_negative_outranking_a_positive_is_counted_as_an_inversion():
    """With no threshold the page shows everything in score order, so order is the whole verdict.

    A negative at 80 above a positive at 70 is a bad posting the reader meets first.
    """
    needles = [{"id": "good", "tier": "T1"}, {"id": "bad", "tier": "N"}]
    graded = harness.grade_scores(needles, {"good": 70, "bad": 80})
    assert graded["inversions"] == 1
    assert graded["outranking"] == ["bad"]
    assert graded["margin"] == -10


def test_cleanly_separated_scores_have_no_inversions_and_a_positive_margin():
    needles = [{"id": "good", "tier": "T1"}, {"id": "bad", "tier": "N"}]
    graded = harness.grade_scores(needles, {"good": 90, "bad": 40})
    assert graded["inversions"] == 0
    assert graded["outranking"] == []
    assert graded["margin"] == 50


def test_a_tie_counts_as_an_inversion():
    """Equal scores leave the order to the tiebreak, which is not a quality signal."""
    needles = [{"id": "good", "tier": "T1"}, {"id": "bad", "tier": "N"}]
    assert harness.grade_scores(needles, {"good": 70, "bad": 70})["inversions"] == 1


def test_inversions_are_counted_per_pair_not_per_negative():
    """One bad posting above three good ones is three things the reader steps over."""
    needles = [
        {"id": "g1", "tier": "T1"}, {"id": "g2", "tier": "T2"},
        {"id": "g3", "tier": "T3"}, {"id": "bad", "tier": "N"},
    ]
    graded = harness.grade_scores(needles, {"g1": 60, "g2": 65, "g3": 70, "bad": 75})
    assert graded["inversions"] == 3


def test_an_unscored_needle_is_counted_as_unscored_not_as_a_failure():
    """A batch the model mangled leaves postings unscored; scoring them 0 would cache a lie."""
    needles = [{"id": "good", "tier": "T1"}, {"id": "bad", "tier": "N"}]
    graded = harness.grade_scores(needles, {})
    assert graded["unscored"] == 2
    assert graded["inversions"] == 0 and graded["margin"] is None
