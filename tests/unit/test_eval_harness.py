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


def test_a_positive_scored_below_the_threshold_is_reported_as_lost():
    """The paid stage's own recall loss: retrieved, rule-passed, and still never shown.

    Every recall figure above this counts the needle as found, because retrieval did find it.
    """
    needles = [{"id": "good", "tier": "T1"}, {"id": "bad", "tier": "N"}]
    graded = harness.grade_scores(needles, {"good": 69, "bad": 10}, threshold=70)
    assert graded["lost"] == ["good"]
    assert graded["leaked"] == []


def test_a_negative_scored_at_the_threshold_is_reported_as_leaked():
    """At the threshold, not merely above it -- the digest gates on `>=`."""
    needles = [{"id": "good", "tier": "T1"}, {"id": "bad", "tier": "N"}]
    graded = harness.grade_scores(needles, {"good": 95, "bad": 70}, threshold=70)
    assert graded["leaked"] == ["bad"]
    assert graded["lost"] == []


def test_an_unscored_needle_is_counted_as_unscored_not_as_a_failure():
    """A batch the model mangled leaves postings unscored; scoring them 0 would cache a lie."""
    needles = [{"id": "good", "tier": "T1"}, {"id": "bad", "tier": "N"}]
    graded = harness.grade_scores(needles, {}, threshold=70)
    assert graded["unscored"] == 2
    assert graded["lost"] == [] and graded["leaked"] == []
