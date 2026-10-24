from __future__ import annotations

from datetime import date
from decimal import Decimal

from appmetrica_cli.fx import FxTable
from appmetrica_cli.report import (
    INVALID,
    NO_FX,
    QUANTITY_NOT_ONE,
    RetentionCohort,
    RevenueBucket,
    ad_revenue,
    daily_metrics,
    iap_days,
    iap_revenue,
    retention,
)
from tests.helpers import ad_row, build, epoch, event_row, install_row, make_store, revenue_row, session_row


def test_daily_metrics_union_of_events_and_sessions(tmp_path):
    store = make_store(tmp_path)
    build(
        store,
        events=[event_row(1, epoch(2026, 9, 15, 10)), event_row(2, epoch(2026, 9, 15, 11))],
        sessions_starts=[
            session_row(2, epoch(2026, 9, 15, 12)),
            session_row(3, epoch(2026, 9, 15, 13)),
        ],
        installations=[
            install_row(1, epoch(2026, 9, 15, 9)),
            install_row(2, epoch(2026, 9, 15, 9), reinstallation="true"),
        ],
    )

    [row] = daily_metrics(store)
    assert (row.day, row.dau, row.sessions, row.installs) == (date(2026, 9, 15), 3, 2, 1)


def test_daily_metrics_series_and_range(tmp_path):
    store = make_store(tmp_path)
    build(
        store,
        events=[event_row(1, epoch(2026, 9, 15, 10)), event_row(1, epoch(2026, 9, 16, 10))],
        sessions_starts=[session_row(2, epoch(2026, 9, 17, 10))],
    )

    rows = daily_metrics(store)
    assert [(r.day, r.dau, r.sessions) for r in rows] == [
        (date(2026, 9, 15), 1, 0),
        (date(2026, 9, 16), 1, 0),
        (date(2026, 9, 17), 1, 1),
    ]
    [filtered] = daily_metrics(store, start=date(2026, 9, 16), end=date(2026, 9, 16))
    assert (filtered.day, filtered.dau, filtered.sessions) == (date(2026, 9, 16), 1, 0)


def test_sessions_exact_duplicate_rows_count_once(tmp_path):
    store = make_store(tmp_path)
    duplicate = session_row(1, epoch(2026, 9, 15, 10), session_id="s1")
    build(
        store,
        sessions_starts=[
            duplicate,
            dict(duplicate),
            session_row(1, epoch(2026, 9, 15, 11), session_id="s2"),
        ],
    )

    [row] = daily_metrics(store)
    assert row.sessions == 2 and row.dau == 1


def test_installs_only_strict_false_and_duplicates_once(tmp_path):
    store = make_store(tmp_path)
    duplicate = install_row(1, epoch(2026, 9, 15, 9))
    build(
        store,
        installations=[
            duplicate,
            dict(duplicate),
            install_row(2, epoch(2026, 9, 15, 9), reinstallation="False"),
            install_row(3, epoch(2026, 9, 15, 9), reinstallation="true"),
            install_row(4, epoch(2026, 9, 15, 9), reinstallation=""),
            install_row(5, epoch(2026, 9, 15, 9), reinstallation="0"),
        ],
    )

    [row] = daily_metrics(store)
    assert row.installs == 1


def test_retention_d1_and_d7_from_install_cohort(tmp_path):
    store = make_store(tmp_path)
    build(
        store,
        installations=[
            install_row(1, epoch(2026, 9, 1, 9)),
            install_row(2, epoch(2026, 9, 1, 9)),
            install_row(3, epoch(2026, 9, 1, 9)),
            install_row(4, epoch(2026, 9, 1, 9), reinstallation="true"),
        ],
        events=[
            event_row(1, epoch(2026, 9, 2, 10)),
            event_row(1, epoch(2026, 9, 8, 10)),
            event_row(4, epoch(2026, 9, 2, 10)),
        ],
        sessions_starts=[
            session_row(2, epoch(2026, 9, 2, 11)),
            session_row(3, epoch(2026, 9, 3, 11)),
        ],
    )

    report = retention(store, days=(1, 7))
    assert report.days == (1, 7)
    assert report.cohorts == [
        RetentionCohort(day=date(2026, 9, 1), size=3, returned={1: 2, 7: 1})
    ]
    assert report.immature == {1: 0, 7: 0}


def test_retention_immature_cohorts_are_excluded_and_counted(tmp_path):
    store = make_store(tmp_path)
    build(
        store,
        installations=[
            install_row(1, epoch(2026, 9, 8, 9)),
            install_row(2, epoch(2026, 9, 9, 9)),
        ],
        events=[event_row(1, epoch(2026, 9, 9, 10))],
    )

    report = retention(store, days=(1, 7))
    assert report.cohorts == [
        RetentionCohort(day=date(2026, 9, 8), size=1, returned={1: 1})
    ]
    assert report.immature == {1: 1, 7: 2}


def _rates(*days: tuple[date, dict[str, str]]) -> FxTable:
    return FxTable({day: {code: Decimal(value) for code, value in rates.items()} for day, rates in days})


def test_iap_uses_last_publication_strictly_before_event_day(tmp_path):
    store = make_store(tmp_path)
    build(
        store,
        revenue_events=[
            revenue_row(1, epoch(2026, 9, 18, 10), "10", "EUR"),
            revenue_row(2, epoch(2026, 9, 19, 10), "10", "EUR"),
            revenue_row(3, epoch(2026, 9, 20, 10), "10", "EUR"),
            revenue_row(4, epoch(2026, 9, 20, 11), "5", "USD"),
        ],
    )
    rates = _rates(
        (date(2026, 9, 17), {"USD": "1.10"}),
        (date(2026, 9, 18), {"USD": "1.20"}),
    )

    rows = iap_revenue(store, rates)
    assert [(r.day, r.events, r.gross_usd) for r in rows] == [
        (date(2026, 9, 18), 1, Decimal("11.00")),  # uses 17 Sep, not the 18 Sep rate
        (date(2026, 9, 19), 1, Decimal("12.00")),
        (date(2026, 9, 20), 2, Decimal("17.00")),  # weekend falls back to Friday
    ]
    assert iap_days(store) == [date(2026, 9, 18), date(2026, 9, 19), date(2026, 9, 20)]


def test_iap_cross_currency_through_eur(tmp_path):
    store = make_store(tmp_path)
    build(store, revenue_events=[revenue_row(1, epoch(2026, 9, 18, 10), "100", "TRY")])
    rates = _rates((date(2026, 9, 17), {"USD": "1.10", "TRY": "40"}))

    [row] = iap_revenue(store, rates)
    assert row.events == 1 and row.gross_usd == Decimal("2.75")


def test_iap_usd_needs_no_rate_and_eur_without_rates_is_no_fx(tmp_path):
    store = make_store(tmp_path)
    build(
        store,
        revenue_events=[
            revenue_row(1, epoch(2026, 9, 18, 10), "5", "USD"),
            revenue_row(2, epoch(2026, 9, 18, 11), "10", "EUR"),
        ],
    )

    [row] = iap_revenue(store, FxTable({}))
    assert row.events == 1 and row.gross_usd == Decimal("5.00")
    assert row.excluded[NO_FX] == [
        RevenueBucket(currency="EUR", events=1, amount=Decimal("10"))
    ]


def test_iap_excluded_buckets_and_original_currency_sums(tmp_path):
    store = make_store(tmp_path)
    build(
        store,
        revenue_events=[
            revenue_row(1, epoch(2026, 9, 18, 10), "5", "EUR", quantity="2"),
            revenue_row(2, epoch(2026, 9, 18, 11), "7", "EUR", quantity="many"),
            revenue_row(3, epoch(2026, 9, 18, 12), "3", "XYZ"),
            revenue_row(4, epoch(2026, 9, 18, 13), "", "EUR"),
            revenue_row(5, epoch(2026, 9, 18, 14), "oops", "GBP"),
            revenue_row(6, epoch(2026, 9, 18, 15), "4", ""),
            revenue_row(7, epoch(2026, 9, 18, 16), "10", "EUR"),
        ],
    )
    rates = _rates((date(2026, 9, 17), {"USD": "1.10"}))

    [row] = iap_revenue(store, rates)
    assert row.events == 1 and row.gross_usd == Decimal("11.00")
    assert row.excluded[QUANTITY_NOT_ONE] == [
        RevenueBucket(
            currency="EUR", events=2, amount=Decimal("12")
        )
    ]
    assert row.excluded[NO_FX] == [
        RevenueBucket(currency="XYZ", events=1, amount=Decimal("3"))
    ]
    assert row.excluded[INVALID] == [
        RevenueBucket(currency="", events=1, amount=Decimal("4")),
        RevenueBucket(currency="EUR", events=1, amount=Decimal("0")),
        RevenueBucket(currency="GBP", events=1, amount=Decimal("0")),
    ]


def test_ad_revenue_is_raw_and_optionally_split_by_network(tmp_path):
    store = make_store(tmp_path)
    build(
        store,
        ad_revenue_events=[
            ad_row(1, epoch(2026, 9, 15, 10), "1.5", "USD", network="admob"),
            ad_row(2, epoch(2026, 9, 15, 11), "2", "USD", network="unity"),
            ad_row(3, epoch(2026, 9, 15, 12), "3", "EUR", network="admob"),
            ad_row(4, epoch(2026, 9, 15, 13), "not-a-number", "USD", network="admob"),
        ],
    )

    totals = ad_revenue(store)
    assert [(r.day, r.currency, r.network, r.events, r.amount) for r in totals] == [
        (date(2026, 9, 15), "EUR", None, 1, 3.0),
        (date(2026, 9, 15), "USD", None, 3, 3.5),
    ]

    split = ad_revenue(store, by_network=True)
    assert [(r.currency, r.network, r.events, r.amount) for r in split] == [
        ("EUR", "admob", 1, 3.0),
        ("USD", "admob", 2, 1.5),
        ("USD", "unity", 1, 2.0),
    ]
