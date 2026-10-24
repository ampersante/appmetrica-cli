from __future__ import annotations

from datetime import timedelta

import pytest

from appmetrica_cli.funnel import FunnelStep, funnel
from tests.helpers import build, epoch, event_row, make_store


def _at(day: int, hour: int, minute: int = 0) -> str:
    return epoch(2026, 9, day, hour, minute)


def test_two_step_funnel_with_exact_params(tmp_path):
    store = make_store(tmp_path)
    build(
        store,
        events=[
            event_row(1, _at(15, 10), "open", {"screen": "shop"}),
            event_row(1, _at(15, 11), "buy", {"item": "sword"}),
            event_row(2, _at(15, 10), "open", {"screen": "shop"}),
            event_row(3, _at(15, 9), "buy", {"item": "sword"}),
            event_row(3, _at(15, 10), "open", {"screen": "shop"}),
            event_row(4, _at(15, 10), "open", {"screen": "facebook"}),
            event_row(4, _at(15, 11), "buy", {"item": "sword"}),
            # pushes the data horizon past entry + window (13:00 > 10:00 + 2 h)
            event_row(99, _at(15, 13), "anchor"),
        ],
    )

    result = funnel(
        store,
        [
            {"event_name": "open", "params": {"screen": "shop"}},
            {"event_name": "buy", "params": {"item": "sword"}},
        ],
        window=timedelta(hours=2),
    )

    assert result.entries == 3
    assert result.immature_entries == 0
    assert [step.devices for step in result.steps] == [3, 1]
    assert result.steps[0].step_conversion == 1.0
    assert result.steps[1].step_conversion == pytest.approx(1 / 3)
    assert result.steps[1].entry_conversion == pytest.approx(1 / 3)


def test_window_is_measured_from_entry_not_between_steps(tmp_path):
    store = make_store(tmp_path)
    build(
        store,
        events=[
            # entry day 15, B day 21 (6 d), C day 24 (9 d): pairwise B->C passes, entry window does not
            event_row(1, _at(15, 10), "a"),
            event_row(1, _at(21, 10), "b"),
            event_row(1, _at(24, 10), "c"),
            # exactly at the window limit
            event_row(2, _at(15, 10), "a"),
            event_row(2, _at(21, 10), "b"),
            event_row(2, _at(22, 10), "c"),
            # one minute past the limit
            event_row(3, _at(15, 10), "a"),
            event_row(3, _at(21, 10), "b"),
            event_row(3, _at(22, 10, 1), "c"),
        ],
    )

    result = funnel(
        store, [FunnelStep("a"), FunnelStep("b"), FunnelStep("c")], window=timedelta(days=7)
    )

    assert result.entries == 3
    assert [step.devices for step in result.steps] == [3, 3, 1]


def test_steps_must_be_strictly_ordered(tmp_path):
    store = make_store(tmp_path)
    build(
        store,
        events=[
            event_row(1, _at(15, 10), "a"),
            event_row(1, _at(15, 10), "b"),
            event_row(2, _at(15, 10), "b"),
            event_row(2, _at(15, 11), "a"),
            event_row(3, _at(15, 10), "a"),
            event_row(3, _at(15, 10, 1), "b"),
            event_row(99, _at(15, 23), "anchor"),
        ],
    )

    result = funnel(store, [FunnelStep("a"), FunnelStep("b")], window=timedelta(hours=1))

    assert result.entries == 3
    assert [step.devices for step in result.steps] == [3, 1]


def test_repeated_step_needs_two_distinct_occurrences(tmp_path):
    store = make_store(tmp_path)
    duplicate = event_row(2, _at(15, 10), "a")
    build(
        store,
        events=[
            event_row(1, _at(15, 10), "a"),
            duplicate,
            dict(duplicate),
            event_row(3, _at(15, 10), "a"),
            event_row(3, _at(15, 10, 1), "a"),
            # different rows, same timestamp: equal timestamps are not progress
            event_row(4, _at(15, 10), "a", session_id="s1"),
            event_row(4, _at(15, 10), "a", session_id="s2"),
            event_row(99, _at(15, 23), "anchor"),
        ],
    )

    result = funnel(store, [FunnelStep("a"), FunnelStep("a")], window=timedelta(hours=1))

    assert result.entries == 4
    assert [step.devices for step in result.steps] == [4, 1]


def test_params_are_exact_first_level_values(tmp_path):
    store = make_store(tmp_path)
    build(
        store,
        events=[
            event_row(1, _at(15, 10), "e", {"screen": "Face"}),
            event_row(2, _at(15, 10), "e", {"screen": "Facebook"}),
            event_row(3, _at(15, 10), "e", {"screen": "face"}),
            event_row(4, _at(15, 10), "e", {"screen": {"name": "Face"}}),
            event_row(5, _at(15, 10), "e"),
            event_row(99, _at(15, 23), "anchor"),
        ],
    )

    result = funnel(store, [FunnelStep("e", {"screen": "Face"})], window=timedelta(hours=1))

    assert result.entries == 1
    assert result.steps[0].devices == 1


def test_immature_entries_are_excluded_and_counted(tmp_path):
    store = make_store(tmp_path)
    build(
        store,
        events=[
            event_row(1, _at(15, 10, 30), "a"),
            event_row(1, _at(15, 11), "b"),
            event_row(2, _at(15, 11, 30), "a"),
            event_row(2, _at(15, 11, 45), "b"),
            event_row(99, _at(15, 12), "anchor"),
        ],
    )

    result = funnel(store, [FunnelStep("a"), FunnelStep("b")], window=timedelta(hours=1))

    assert result.entries == 1
    assert result.immature_entries == 1
    assert [step.devices for step in result.steps] == [1, 1]


def test_only_the_first_step_one_event_is_an_entry(tmp_path):
    store = make_store(tmp_path)
    build(
        store,
        events=[
            event_row(1, _at(15, 10), "a"),
            event_row(1, _at(15, 12), "a"),
            event_row(1, _at(15, 12, 1), "b"),
            event_row(99, _at(15, 14), "anchor"),
        ],
    )

    result = funnel(store, [FunnelStep("a"), FunnelStep("b")], window=timedelta(hours=1))

    assert result.entries == 1
    assert [step.devices for step in result.steps] == [1, 0]


def test_no_entries_gives_no_conversions(tmp_path):
    store = make_store(tmp_path)
    build(store, events=[event_row(1, _at(15, 10), "other")])

    result = funnel(store, [FunnelStep("a"), FunnelStep("b")])

    assert result.entries == 0
    assert [step.devices for step in result.steps] == [0, 0]
    assert result.steps[0].step_conversion is None
    assert result.steps[0].entry_conversion is None


def test_invalid_funnel_input(tmp_path):
    store = make_store(tmp_path)
    build(store, events=[])
    with pytest.raises(ValueError):
        funnel(store, [])
    with pytest.raises(ValueError):
        funnel(store, [FunnelStep("a")], window=timedelta(0))
    with pytest.raises(ValueError):
        funnel(store, [FunnelStep("a", {"k": None})])
