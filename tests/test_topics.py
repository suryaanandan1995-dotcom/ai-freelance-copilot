"""Offline tests for the weighted post-topic rotation (content/topics.py).

The rotation was previously a bash array inside the LinkedIn workflow: untestable,
un-reusable, and DevOps-only. Fourteen posts published over July 2026 on flat-market
angles while LLM contract demand grew +247% YoY. These tests pin the mix.
"""
from __future__ import annotations

from content.topics import TOPICS, rotation, segments, topic_for_day


def test_every_topic_is_wellformed():
    for topic, segment, weight in TOPICS:
        assert topic and topic == topic.strip()
        assert segment in {"ai-infra", "agents", "fde", "devsecops"}
        assert weight >= 1


def test_no_duplicate_topics():
    """A duplicate would silently double its weight and skew the mix."""
    texts = [t for t, _, _ in TOPICS]
    assert len(texts) == len(set(texts))


def test_all_four_segments_are_represented():
    counts = segments()
    assert set(counts) == {"ai-infra", "agents", "fde", "devsecops"}
    assert all(v > 0 for v in counts.values())


def test_growth_segments_outweigh_the_flat_one():
    """AI-infra + agents must dominate: £550/day and +247% YoY vacancies, versus
    Kubernetes at £535/day with its rank falling 6 places."""
    counts = segments()
    growth = counts["ai-infra"] + counts["agents"]
    assert growth > counts["devsecops"]


def test_devsecops_is_still_present_as_the_credibility_base():
    """It is what buyers search for; dropping it entirely would cost inbound reach."""
    assert segments()["devsecops"] >= 5


def test_fde_segment_is_included():
    assert segments()["fde"] >= 3


def test_rotation_expands_by_weight():
    slots = rotation()
    assert len(slots) == sum(w for _, _, w in TOPICS)
    heavy = [t for t, _, w in TOPICS if w == 3][0]
    assert slots.count(heavy) == 3


def test_topic_for_day_is_deterministic():
    """A re-run of the same day must produce the same post, not a second one — the
    publish path caps at one post/day, so a different topic would just be dropped."""
    assert topic_for_day(200) == topic_for_day(200)


def test_topic_for_day_covers_every_slot_across_a_year():
    produced = {topic_for_day(d) for d in range(1, 367)}
    assert produced == set(rotation())


def test_topic_for_day_handles_boundaries():
    for day in (0, 1, 366, 999):
        assert topic_for_day(day) in set(rotation())
