from __future__ import annotations

from datetime import date

from appmetrica_cli.discover import event_parameters, list_events
from tests.helpers import build, epoch, event_row, make_store


def test_list_events_is_generic_and_sorted(tmp_path):
    store = make_store(tmp_path)
    build(
        store,
        events=[
            event_row(1, epoch(2026, 9, 15, 10), "zeta_custom", {"a": "1"}),
            event_row(1, epoch(2026, 9, 15, 11), "zeta_custom", {"a": "2"}),
            event_row(2, epoch(2026, 9, 16, 10), "zeta_custom", {"a": "3"}),
            event_row(3, epoch(2026, 9, 14, 10), "alpha_custom", {"b": "x"}),
            event_row(4, epoch(2026, 9, 14, 11), ""),
        ],
    )

    summaries = list_events(store)

    assert [s.event_name for s in summaries] == ["alpha_custom", "zeta_custom"]
    assert (summaries[0].rows, summaries[0].devices) == (1, 1)
    assert (summaries[0].first_day, summaries[0].last_day) == (
        date(2026, 9, 14),
        date(2026, 9, 14),
    )
    assert (summaries[1].rows, summaries[1].devices) == (3, 2)
    assert (summaries[1].first_day, summaries[1].last_day) == (
        date(2026, 9, 15),
        date(2026, 9, 16),
    )


def test_event_parameters_types_and_values(tmp_path):
    store = make_store(tmp_path)
    build(
        store,
        events=[
            event_row(1, epoch(2026, 9, 15, 10), "made_up_event", {"level": "1", "mixed": 1, "obj": {"x": 1}}),
            event_row(2, epoch(2026, 9, 15, 11), "made_up_event", {"level": "1", "mixed": "one"}),
            event_row(3, epoch(2026, 9, 15, 12), "made_up_event", {"level": "2", "flag": True}),
            event_row(4, epoch(2026, 9, 15, 13), "made_up_event", "not json"),
            event_row(5, epoch(2026, 9, 15, 14), "made_up_event"),
            event_row(6, epoch(2026, 9, 15, 15), "other_event", {"level": "9"}),
        ],
    )

    parameters = {p.key: p for p in event_parameters(store, "made_up_event")}

    assert sorted(parameters) == ["flag", "level", "mixed", "obj"]
    assert parameters["level"].rows == 3
    assert parameters["level"].types == {"VARCHAR": 3}
    assert parameters["level"].top_values == (("1", 2), ("2", 1))
    assert parameters["mixed"].types == {"UBIGINT": 1, "VARCHAR": 1}
    assert parameters["mixed"].top_values == (("1", 1), ("one", 1))
    assert parameters["flag"].types == {"BOOLEAN": 1}
    assert parameters["flag"].top_values == (("true", 1),)
    assert parameters["obj"].types == {"OBJECT": 1}
    assert parameters["obj"].top_values == (('{"x":1}', 1),)


def test_event_parameters_top_ten_values(tmp_path):
    store = make_store(tmp_path)
    build(
        store,
        events=[
            event_row(index, epoch(2026, 9, 15, 10), "many_values", {"v": str(index)})
            for index in range(12)
        ],
    )

    [parameter] = event_parameters(store, "many_values")
    assert len(parameter.top_values) == 10
    assert [value for value, _count in parameter.top_values] == [
        "0",
        "1",
        "10",
        "11",
        "2",
        "3",
        "4",
        "5",
        "6",
        "7",
    ]


def test_unknown_event_has_no_parameters(tmp_path):
    store = make_store(tmp_path)
    build(store, events=[event_row(1, epoch(2026, 9, 15, 10), "real_event", {"a": "1"})])

    assert event_parameters(store, "no_such_event") == []
