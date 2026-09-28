from __future__ import annotations

import pytest


def test_store_and_query_events(cache, sample_events):
    count = cache.store_events(1, sample_events)
    assert count == 4

    rows = cache.query(1, "SELECT COUNT(*) FROM events")
    assert rows[0][0] == 4

    rows = cache.query(
        1, "SELECT event_name, COUNT(*) FROM events GROUP BY event_name ORDER BY event_name"
    )
    names = {r[0]: r[1] for r in rows}
    assert names["tutorial_start"] == 2
    assert names["tutorial_complete"] == 1
    assert names["session_start"] == 1


def test_store_and_query_installations(cache, sample_installations):
    count = cache.store_installations(1, sample_installations)
    assert count == 2

    rows = cache.query(1, "SELECT COUNT(*) FROM installations")
    assert rows[0][0] == 2


def test_empty_store(cache):
    assert cache.store_events(1, []) == 0
    assert cache.store_installations(1, []) == 0


def test_cached_date_range(cache, sample_events, sample_installations):
    cache.store_events(1, sample_events)
    r = cache.get_cached_date_range(1, "events")
    assert r is not None
    assert r[0] == "2026-05-10"
    assert r[1] == "2026-05-11"

    cache.store_installations(1, sample_installations)
    r = cache.get_cached_date_range(1, "installations")
    assert r is not None
    assert r[0] == "2026-05-10"
    assert r[1] == "2026-05-10"


def test_no_cached_range_when_empty(cache):
    assert cache.get_cached_date_range(1, "events") is None


def test_separate_dbs_per_app(cache, sample_events):
    cache.store_events(1, sample_events[:2])
    cache.store_events(2, sample_events[2:])

    rows1 = cache.query(1, "SELECT COUNT(*) FROM events")
    rows2 = cache.query(2, "SELECT COUNT(*) FROM events")
    assert rows1[0][0] == 2
    assert rows2[0][0] == 2


def test_event_json_queryable(cache, sample_events):
    cache.store_events(1, sample_events)

    rows = cache.query(
        1,
        "SELECT json_extract_string(event_json, '$.step') FROM events WHERE event_name = 'tutorial_start' LIMIT 1",
    )
    assert rows[0][0] == "1"
