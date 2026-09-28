from __future__ import annotations

import pytest

from appmetrica_cli.analytics import build_funnel, compute_retention, compare_cohorts
from appmetrica_cli.cache import EventCache


@pytest.fixture
def populated_cache(cache, sample_events, sample_installations):
    cache.store_events(1, sample_events)
    cache.store_installations(1, sample_installations)
    return cache


class TestBuildFunnel:
    def test_basic_funnel(self, populated_cache):
        db = populated_cache._get_db(1)
        result = build_funnel(
            db,
            [{"event_name": "tutorial_start"}, {"event_name": "tutorial_complete"}],
            "2026-05-10",
            "2026-05-11",
        )

        assert result["total_entered"] == 2
        assert result["total_completed"] == 1
        assert result["overall_conversion"] == 50.0
        assert len(result["steps"]) == 2
        assert result["steps"][0]["users"] == 2
        assert result["steps"][1]["users"] == 1

    def test_funnel_with_country_filter(self, populated_cache):
        db = populated_cache._get_db(1)
        result = build_funnel(
            db,
            [{"event_name": "tutorial_start"}, {"event_name": "tutorial_complete"}],
            "2026-05-10",
            "2026-05-11",
            country="US",
        )

        assert result["total_entered"] == 1
        assert result["total_completed"] == 1
        assert result["overall_conversion"] == 100.0

    def test_funnel_with_source_filter(self, populated_cache):
        db = populated_cache._get_db(1)
        result = build_funnel(
            db,
            [{"event_name": "tutorial_start"}],
            "2026-05-10",
            "2026-05-11",
            source="facebook",
        )

        assert result["total_entered"] == 1

    def test_empty_funnel(self, populated_cache):
        db = populated_cache._get_db(1)
        result = build_funnel(
            db,
            [{"event_name": "nonexistent_event"}],
            "2026-05-10",
            "2026-05-11",
        )
        assert result["total_entered"] == 0

    def test_empty_steps(self, populated_cache):
        db = populated_cache._get_db(1)
        result = build_funnel(db, [], "2026-05-10", "2026-05-11")
        assert result["steps"] == []

    def test_funnel_conversion_rates(self, populated_cache):
        db = populated_cache._get_db(1)
        result = build_funnel(
            db,
            [{"event_name": "tutorial_start"}, {"event_name": "tutorial_complete"}],
            "2026-05-10",
            "2026-05-11",
        )

        step1 = result["steps"][0]
        assert step1["conversion_from_start"] == 100.0
        assert step1["conversion_from_prev"] == 100.0
        assert step1["drop_off"] == 0

        step2 = result["steps"][1]
        assert step2["conversion_from_start"] == 50.0
        assert step2["conversion_from_prev"] == 50.0
        assert step2["drop_off"] == 1


class TestComputeRetention:
    def test_basic_retention(self, populated_cache):
        db = populated_cache._get_db(1)
        result = compute_retention(
            db,
            days=[1],
            cohort_from="2026-05-10",
            cohort_to="2026-05-10",
        )

        assert len(result) == 1
        assert result[0]["cohort_date"] == "2026-05-10"
        assert result[0]["cohort_size"] == 2
        assert 1 in result[0]["retention"]
        assert result[0]["retention"][1] == 50.0

    def test_retention_with_country_filter(self, populated_cache):
        db = populated_cache._get_db(1)
        result = compute_retention(
            db,
            days=[1],
            cohort_from="2026-05-10",
            cohort_to="2026-05-10",
            country="US",
        )

        assert len(result) == 1
        assert result[0]["cohort_size"] == 1
        assert result[0]["retention"][1] == 100.0

    def test_retention_with_source_filter(self, populated_cache):
        db = populated_cache._get_db(1)
        result = compute_retention(
            db,
            days=[1],
            cohort_from="2026-05-10",
            cohort_to="2026-05-10",
            source="facebook",
        )

        assert len(result) == 1
        assert result[0]["cohort_size"] == 1


class TestCompareCohorts:
    def test_compare_user_count(self, populated_cache):
        db = populated_cache._get_db(1)
        result = compare_cohorts(
            db,
            {"source": "facebook"},
            {"source": "applovin"},
            ["user_count"],
            "2026-05-10",
            "2026-05-10",
        )

        assert len(result) == 1
        assert result[0]["metric"] == "user_count"
        assert result[0]["cohort_a_value"] == 1.0
        assert result[0]["cohort_b_value"] == 1.0

    def test_compare_retention(self, populated_cache):
        db = populated_cache._get_db(1)
        result = compare_cohorts(
            db,
            {"source": "facebook"},
            {"source": "applovin"},
            ["retention_d1"],
            "2026-05-10",
            "2026-05-10",
        )

        assert len(result) == 1
        assert result[0]["metric"] == "retention_d1"
        assert result[0]["cohort_a_value"] == 100.0
        assert result[0]["cohort_b_value"] == 0.0
