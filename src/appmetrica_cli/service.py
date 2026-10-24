"""Thin orchestration for the command line: no analytics logic lives here.

Local reports open the analytics database built by :func:`build_app` and call
the step 4 modules. Only :func:`sync_app`, :func:`iap_report` (when the ECB
cache does not cover the needed days) and :func:`reconcile_a1` touch the
network. Errors of the underlying modules propagate unchanged; this layer only
adds the A1 reconciliation result type.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx

from appmetrica_cli import discover, funnel, loader, report, sandbox
from appmetrica_cli.client import AppMetricaClient
from appmetrica_cli.config import AppMetricaConfig, home_dir
from appmetrica_cli.fx import load_fx
from appmetrica_cli.storage import AppStore


class ServiceError(Exception):
    """Invalid input or an unexpected API answer, worth showing as is."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def _store(home: Path, app_id: int) -> AppStore:
    return AppStore(Path(home), app_id)


def _client(config: AppMetricaConfig) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"Authorization": f"OAuth {config.oauth_token}"},
        timeout=config.request_timeout,
    )


# ── sync and build ──────────────────────────────────────────────────


async def sync_app(
    config: AppMetricaConfig,
    app_id: int,
    start: date,
    end: date,
    tables: list[str] | None = None,
    progress: Callable[[str], None] | None = None,
) -> loader.SyncReport:
    """Fetch Logs API partitions into the store under the configured home."""
    store = AppStore(home_dir(), app_id)
    async with _client(config) as http:
        return await loader.sync(
            AppMetricaClient(http, config), store, start, end,
            tables=tables, progress=progress,
        )


def build_app(home: Path, app_id: int) -> dict[str, int]:
    """Rebuild ``analytics.duckdb``; returns row counts per table."""
    return _store(home, app_id).build()


# ── local reports ───────────────────────────────────────────────────


def daily_report(
    home: Path, app_id: int, start: date | None = None, end: date | None = None
) -> list[report.DailyMetrics]:
    return report.daily_metrics(_store(home, app_id), start, end)


def retention_report(
    home: Path,
    app_id: int,
    days: Sequence[int] = (1, 7),
    start: date | None = None,
    end: date | None = None,
) -> report.RetentionReport:
    return report.retention(_store(home, app_id), tuple(days), start, end)


def ad_report(
    home: Path,
    app_id: int,
    start: date | None = None,
    end: date | None = None,
    by_network: bool = False,
) -> list[report.AdRevenueDay]:
    return report.ad_revenue(_store(home, app_id), start, end, by_network=by_network)


def funnel_report(
    home: Path,
    app_id: int,
    steps: Sequence[funnel.FunnelStep | dict[str, Any]],
    window: timedelta,
) -> funnel.FunnelResult:
    return funnel.funnel(_store(home, app_id), steps, window)


def sql_query(
    home: Path, app_id: int, sql: str, *, timeout: float = sandbox.DEFAULT_TIMEOUT
) -> sandbox.SandboxResult:
    return sandbox.run_query(_store(home, app_id), sql, timeout=timeout)


def events_overview(home: Path, app_id: int) -> list[discover.EventSummary]:
    return discover.list_events(_store(home, app_id))


def event_params(home: Path, app_id: int, event_name: str) -> list[discover.ParameterSummary]:
    return discover.event_parameters(_store(home, app_id), event_name)


async def iap_report(
    home: Path, app_id: int, start: date | None = None, end: date | None = None
) -> list[report.IapRevenueDay]:
    """IAP revenue in USD; ECB rates come from the cache unless it is insufficient."""
    store = _store(home, app_id)
    needed = report.iap_days(store, start, end)
    rates = await load_fx(Path(home), needed)
    return report.iap_revenue(store, rates, start, end)


# ── reconciliation A1 ───────────────────────────────────────────────

A1_METRICS = ("users", "sessions", "installDevices")


@dataclass(frozen=True)
class MetricComparison:
    ours: int | None
    appmetrica: int | None
    diff: int | None  # ours - appmetrica; None when either side has no value


@dataclass(frozen=True)
class ReconcileResult:
    days: dict[date, dict[str, MetricComparison]]
    matched_days: int
    mismatched_days: int


async def reconcile_a1(
    config: AppMetricaConfig,
    home: Path,
    app_id: int,
    start: date,
    end: date,
) -> ReconcileResult:
    """Compare our DAU, sessions and installs per day with the Reporting API.

    Exactly two requests are made, each with one metric prefix: general
    (``ym:ge:users`` and ``ym:ge:sessions``) and install (``ym:i:installDevices``).
    A day absent on one side is still a comparison row with ``None`` there;
    a mismatch is returned as data, never raised.
    """
    ours = {row.day: row for row in report.daily_metrics(_store(home, app_id), start, end)}
    date1, date2 = start.isoformat(), end.isoformat()
    async with _client(config) as http:
        client = AppMetricaClient(http, config)
        general = await client.get_report(
            app_id,
            ["ym:ge:users", "ym:ge:sessions"],
            ["ym:ge:date"],
            date1=date1,
            date2=date2,
            limit=10_000,
        )
        installs = await client.get_report(
            app_id,
            ["ym:i:installDevices"],
            ["ym:i:date"],
            date1=date1,
            date2=date2,
            limit=10_000,
        )
    api_users = _series(general, 0)
    api_sessions = _series(general, 1)
    api_installs = _series(installs, 0)

    all_days = sorted(set(ours) | set(api_users) | set(api_installs))
    days: dict[date, dict[str, MetricComparison]] = {}
    matched = 0
    for day in all_days:
        own = ours.get(day)
        comparisons = {
            "users": _compare(own.dau if own else None, _value(api_users, day)),
            "sessions": _compare(own.sessions if own else None, _value(api_sessions, day)),
            "installDevices": _compare(own.installs if own else None, _value(api_installs, day)),
        }
        days[day] = comparisons
        if all(c.ours is not None and c.appmetrica is not None and c.diff == 0 for c in comparisons.values()):
            matched += 1
    return ReconcileResult(
        days=days,
        matched_days=matched,
        mismatched_days=len(days) - matched,
    )


def _series(payload: dict, index: int) -> dict[date, int | None]:
    """One metric column of a Reporting API answer, keyed by its date dimension."""
    result: dict[date, int | None] = {}
    for row in payload.get("data") or []:
        dimensions = row.get("dimensions") or []
        name = (dimensions[0] or {}).get("name") if dimensions else None
        try:
            day = date.fromisoformat(str(name))
        except ValueError:
            raise ServiceError(f"Reporting API returned an unexpected date {name!r}") from None
        metrics = row.get("metrics") or []
        value = metrics[index] if index < len(metrics) else None
        result[day] = None if value is None else int(value)
    return result


def _value(series: dict[date, int | None], day: date) -> int | None:
    return series.get(day)


def _compare(ours: int | None, appmetrica: int | None) -> MetricComparison:
    diff = ours - appmetrica if ours is not None and appmetrica is not None else None
    return MetricComparison(ours=ours, appmetrica=appmetrica, diff=diff)
