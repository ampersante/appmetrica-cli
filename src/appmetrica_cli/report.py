"""Daily metrics over ``analytics.duckdb``.

The day of every metric is the ``day`` column: the effective event time
(PRD D2) in the app's time zone. Device-level definitions:

* DAU -- distinct devices with an event or a session start that day.
* sessions -- ``sessions_starts`` rows that day, exact duplicate rows once.
* installs -- ``installations`` rows with ``is_reinstallation = 'false'`` that
  day, exact duplicate rows once.
* retention D-N -- devices installed on a day (non-reinstall), returned when
  an event or a session start falls exactly on day + N.
* IAP revenue -- ``revenue_events`` converted to USD with the ECB rate of the
  last publication strictly before the event day; rows that cannot be
  converted are excluded from the sum and reported in separate buckets.
* ad revenue -- raw amounts, grouped by original currency, never converted.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from appmetrica_cli.db import read_only
from appmetrica_cli.fx import FxTable
from appmetrica_cli.storage import AppStore

MONEY = Decimal("0.01")
QUANTITY_NOT_ONE = "quantity"
NO_FX = "no_fx"
INVALID = "invalid"


# ── results ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DailyMetrics:
    day: date
    dau: int
    sessions: int
    installs: int


@dataclass(frozen=True)
class RetentionCohort:
    day: date
    size: int
    returned: dict[int, int]  # N -> devices active exactly on day + N


@dataclass(frozen=True)
class RetentionReport:
    days: tuple[int, ...]
    cohorts: list[RetentionCohort]
    immature: dict[int, int]  # N -> cohorts excluded because day + N is past the data


@dataclass(frozen=True)
class RevenueBucket:
    currency: str
    events: int
    amount: Decimal  # sum of prices in the original currency


@dataclass(frozen=True)
class IapRevenueDay:
    day: date
    events: int  # converted events
    gross_usd: Decimal
    excluded: dict[str, list[RevenueBucket]]  # QUANTITY_NOT_ONE / NO_FX / INVALID


@dataclass(frozen=True)
class AdRevenueDay:
    day: date
    currency: str
    network: str | None
    events: int
    amount: float


# ── daily metrics ───────────────────────────────────────────────────


def daily_metrics(
    store: AppStore, start: date | None = None, end: date | None = None
) -> list[DailyMetrics]:
    """DAU, sessions and installs for every day present between start and end."""
    condition, params = _range("day", start, end)
    with read_only(store) as con:
        dau = dict(
            con.execute(
                f"""
                WITH ev AS (
                    SELECT day, appmetrica_device_id
                    FROM events
                    WHERE {condition} AND appmetrica_device_id IS NOT NULL
                      AND appmetrica_device_id <> ''
                ), ss AS (
                    SELECT day, appmetrica_device_id
                    FROM sessions_starts
                    WHERE {condition} AND appmetrica_device_id IS NOT NULL
                      AND appmetrica_device_id <> ''
                )
                SELECT day, count(DISTINCT appmetrica_device_id)
                FROM (SELECT * FROM ev UNION ALL SELECT * FROM ss)
                WHERE day IS NOT NULL
                GROUP BY day
                """,
                params + params,
            ).fetchall()
        )
        sessions = dict(
            con.execute(
                f"""
                SELECT day, count(*)
                FROM (SELECT DISTINCT * FROM sessions_starts WHERE {condition})
                WHERE day IS NOT NULL
                GROUP BY day
                """,
                params,
            ).fetchall()
        )
        installs = dict(
            con.execute(
                f"""
                SELECT day, count(*)
                FROM (
                    SELECT DISTINCT * FROM installations
                    WHERE {condition} AND is_reinstallation = 'false'
                )
                WHERE day IS NOT NULL
                GROUP BY day
                """,
                params,
            ).fetchall()
        )
    days = sorted(set(dau) | set(sessions) | set(installs))
    return [
        DailyMetrics(
            day=day,
            dau=dau.get(day, 0),
            sessions=sessions.get(day, 0),
            installs=installs.get(day, 0),
        )
        for day in days
    ]


# ── retention ───────────────────────────────────────────────────────


def retention(
    store: AppStore,
    days: tuple[int, ...] = (1, 7),
    start: date | None = None,
    end: date | None = None,
) -> RetentionReport:
    """Classic D-N retention per non-reinstall install cohort.

    A cohort is excluded from the result for an N (and counted in
    ``immature``) when ``cohort day + N`` is later than the last day present in
    the loaded events, sessions and installations.
    """
    offsets = tuple(days)
    if any(n <= 0 for n in offsets):
        raise ValueError("retention days must be positive integers")
    if not offsets:
        return RetentionReport(days=(), cohorts=[], immature={})

    condition, params = _range("day", start, end)
    with read_only(store) as con:
        horizon = con.execute(
            """
            SELECT max(day) FROM (
                SELECT day FROM events
                UNION ALL SELECT day FROM sessions_starts
                UNION ALL SELECT day FROM installations
            )
            """
        ).fetchone()[0]
        per_n: dict[int, dict[date, tuple[int, int]]] = {}
        for n in offsets:
            rows = con.execute(
                f"""
                WITH cohort AS (
                    SELECT DISTINCT appmetrica_device_id, day
                    FROM installations
                    WHERE is_reinstallation = 'false'
                      AND day IS NOT NULL
                      AND appmetrica_device_id IS NOT NULL
                      AND appmetrica_device_id <> ''
                      AND {condition}
                ), activity AS (
                    SELECT appmetrica_device_id, day FROM events
                    WHERE appmetrica_device_id IS NOT NULL AND day IS NOT NULL
                    UNION
                    SELECT appmetrica_device_id, day FROM sessions_starts
                    WHERE appmetrica_device_id IS NOT NULL AND day IS NOT NULL
                )
                SELECT c.day,
                       count(DISTINCT c.appmetrica_device_id) AS cohort_size,
                       count(DISTINCT a.appmetrica_device_id) AS returned
                FROM cohort c
                LEFT JOIN activity a
                  ON a.appmetrica_device_id = c.appmetrica_device_id
                 AND a.day = c.day + CAST(? AS INTEGER)
                GROUP BY c.day
                ORDER BY c.day
                """,
                params + [n],
            ).fetchall()
            per_n[n] = {row[0]: (row[1], row[2]) for row in rows}

    immature = {n: 0 for n in offsets}
    cohorts: list[RetentionCohort] = []
    for cohort_day, (size, _returned) in per_n[offsets[0]].items():
        returned: dict[int, int] = {}
        for n in offsets:
            if horizon is not None and cohort_day + timedelta(days=n) <= horizon:
                returned[n] = per_n[n][cohort_day][1]
            else:
                immature[n] += 1
        if returned:
            cohorts.append(RetentionCohort(day=cohort_day, size=size, returned=returned))
    return RetentionReport(days=offsets, cohorts=cohorts, immature=immature)


# ── revenue ─────────────────────────────────────────────────────────


def iap_days(
    store: AppStore, start: date | None = None, end: date | None = None
) -> list[date]:
    """Days that need an ECB rate to convert IAP revenue."""
    condition, params = _range("day", start, end)
    with read_only(store) as con:
        rows = con.execute(
            f"""
            SELECT DISTINCT day FROM revenue_events
            WHERE day IS NOT NULL AND {condition}
            ORDER BY day
            """,
            params,
        ).fetchall()
    return [row[0] for row in rows]


def iap_revenue(
    store: AppStore,
    rates: FxTable,
    start: date | None = None,
    end: date | None = None,
) -> list[IapRevenueDay]:
    """Gross IAP revenue in USD per day plus the excluded-row buckets."""
    condition, params = _range("day", start, end)
    with read_only(store) as con:
        rows = con.execute(
            f"""
            SELECT day, revenue_currency, revenue_price, revenue_quantity
            FROM revenue_events
            WHERE day IS NOT NULL AND {condition}
            ORDER BY day
            """,
            params,
        ).fetchall()

    acc: dict[date, dict] = {}
    for day, currency, price, quantity in rows:
        entry = acc.setdefault(
            day,
            {
                "events": 0,
                "usd": Decimal(0),
                QUANTITY_NOT_ONE: {},
                NO_FX: {},
                INVALID: {},
            },
        )
        code = (currency or "").strip().upper()
        amount = _decimal(price)
        if not code or amount is None:
            _add_bucket(entry[INVALID], code, amount)
            continue
        if _decimal(quantity) != Decimal(1):
            _add_bucket(entry[QUANTITY_NOT_ONE], code, amount)
            continue
        rate = rates.usd_rate(code, day)
        if rate is None:
            _add_bucket(entry[NO_FX], code, amount)
            continue
        entry["events"] += 1
        entry["usd"] += amount * rate

    result = []
    for day in sorted(acc):
        entry = acc[day]
        result.append(
            IapRevenueDay(
                day=day,
                events=entry["events"],
                gross_usd=entry["usd"].quantize(MONEY, rounding=ROUND_HALF_UP),
                excluded={
                    bucket: _buckets(entry[bucket])
                    for bucket in (QUANTITY_NOT_ONE, NO_FX, INVALID)
                },
            )
        )
    return result


def ad_revenue(
    store: AppStore,
    start: date | None = None,
    end: date | None = None,
    *,
    by_network: bool = False,
) -> list[AdRevenueDay]:
    """Raw ad revenue per day and currency (optionally split by network)."""
    condition, params = _range("day", start, end)
    network = "ad_revenue_network" if by_network else "NULL"
    grouping = "1, 2, 3" if by_network else "1, 2"
    with read_only(store) as con:
        rows = con.execute(
            f"""
            SELECT day,
                   coalesce(ad_revenue_currency, '') AS currency,
                   {network} AS network,
                   count(*) AS events,
                   sum(TRY_CAST(ad_revenue AS DOUBLE)) AS amount
            FROM ad_revenue_events
            WHERE day IS NOT NULL AND {condition}
            GROUP BY {grouping}
            ORDER BY {grouping}
            """,
            params,
        ).fetchall()
    return [
        AdRevenueDay(
            day=row[0],
            currency=row[1],
            network=row[2] if by_network else None,
            events=row[3],
            amount=float(row[4] or 0.0),
        )
        for row in rows
    ]


# ── internals ───────────────────────────────────────────────────────


def _range(column: str, start: date | None, end: date | None) -> tuple[str, list]:
    conditions: list[str] = []
    params: list = []
    if start is not None:
        conditions.append(f"{column} >= ?")
        params.append(start)
    if end is not None:
        conditions.append(f"{column} <= ?")
        params.append(end)
    return (" AND ".join(conditions) or "TRUE"), params


def _decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _add_bucket(buckets: dict[str, list], currency: str, amount: Decimal | None) -> None:
    entry = buckets.setdefault(currency, [0, Decimal(0)])
    entry[0] += 1
    entry[1] += amount if amount is not None else Decimal(0)


def _buckets(buckets: dict[str, list]) -> list[RevenueBucket]:
    return [
        RevenueBucket(currency=currency, events=buckets[currency][0], amount=buckets[currency][1])
        for currency in sorted(buckets)
    ]
