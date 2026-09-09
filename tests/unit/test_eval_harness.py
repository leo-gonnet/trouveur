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
