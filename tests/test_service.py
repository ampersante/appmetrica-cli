from __future__ import annotations

import io
import zipfile
from datetime import date
from decimal import Decimal

import httpx
import pytest
import respx

from appmetrica_cli import service
from appmetrica_cli.config import AppMetricaConfig
from appmetrica_cli.fx import save_cache
from appmetrica_cli.storage import AppStore, StorageError
from tests.helpers import (
    build,
    csv_text,
    epoch,
    event_row,
    install_row,
    revenue_row,
    session_row,
)

REPORT_URL = "https://api.appmetrica.yandex.ru/stat/v1/data"
MANAGEMENT_URL = "https://api.appmetrica.yandex.com/management/v1/applications"
ACTIVE_URL = "https://api.appmetrica.yandex.ru/logs/v1/export/get_active_queries"
EVENTS_CSV_URL = "https://api.appmetrica.yandex.ru/logs/v1/export/events.csv"
DAY = date(2026, 9, 30)


def _config(**overrides) -> AppMetricaConfig:
    values = {"oauth_token": "test-token", "retry_base_delay": 0}
    values.update(overrides)
    return AppMetricaConfig(**values)


def _store(tmp_path) -> AppStore:
    store = AppStore(tmp_path, 1)
    store.set_time_zone("UTC")
    return store


def _report_handler(general, installs):
    def handler(request: httpx.Request) -> httpx.Response:
        metrics = request.url.params["metrics"]
        if metrics == "ym:ge:users,ym:ge:sessions":
            return httpx.Response(200, json={"data": general})
        if metrics == "ym:i:installDevices":
            return httpx.Response(200, json={"data": installs})
        return httpx.Response(400, text=f"unexpected metrics {metrics}")

    return handler


def _row(day: str, *metrics: int | None) -> dict:
    return {"dimensions": [{"name": day}], "metrics": list(metrics)}


@respx.mock
async def test_reconcile_a1_matched(tmp_path):
    store = _store(tmp_path)
    build(
        store,
        events=[event_row(1, epoch(2026, 9, 30, 10)), event_row(2, epoch(2026, 9, 30, 11))],
        sessions_starts=[
            session_row(2, epoch(2026, 9, 30, 12)),
            session_row(3, epoch(2026, 9, 30, 13)),
        ],
        installations=[install_row(1, epoch(2026, 9, 30, 9))],
    )
    route = respx.get(REPORT_URL).mock(
        side_effect=_report_handler([_row("2026-09-30", 3, 2)], [_row("2026-09-30", 1)])
    )

    result = await service.reconcile_a1(_config(), tmp_path, 1, DAY, DAY)

    assert route.call_count == 2  # exactly two Reporting API requests
    assert result.matched_days == 1
    assert result.mismatched_days == 0
    assert result.days[DAY]["users"] == service.MetricComparison(ours=3, appmetrica=3, diff=0)
    assert result.days[DAY]["sessions"] == service.MetricComparison(ours=2, appmetrica=2, diff=0)
    assert result.days[DAY]["installDevices"] == service.MetricComparison(ours=1, appmetrica=1, diff=0)


@respx.mock
async def test_reconcile_a1_mismatch_is_data_not_exception(tmp_path):
    store = _store(tmp_path)
    build(
        store,
        events=[event_row(1, epoch(2026, 9, 30, 10)), event_row(2, epoch(2026, 9, 30, 11))],
        sessions_starts=[session_row(3, epoch(2026, 9, 30, 12))],
        installations=[install_row(1, epoch(2026, 9, 30, 9))],
    )
    respx.get(REPORT_URL).mock(
        side_effect=_report_handler(
            [_row("2026-09-30", 5, 1), _row("2026-10-01", 7, 7)],
            [_row("2026-09-30", 1)],
        )
    )

    result = await service.reconcile_a1(_config(), tmp_path, 1, DAY, date(2026, 10, 1))

    users = result.days[DAY]["users"]
    assert (users.ours, users.appmetrica, users.diff) == (3, 5, -2)
    assert result.days[DAY]["sessions"].diff == 0
    # A day present on one side only is still a comparison row, missing side is None.
    next_day = result.days[date(2026, 10, 1)]
    assert next_day["users"] == service.MetricComparison(ours=None, appmetrica=7, diff=None)
    assert next_day["installDevices"] == service.MetricComparison(ours=None, appmetrica=None, diff=None)
    assert result.matched_days == 0
    assert result.mismatched_days == 2


async def test_reconcile_a1_needs_a_built_store(tmp_path):
    with pytest.raises(StorageError, match="run build"):
        await service.reconcile_a1(_config(), tmp_path, 1, DAY, DAY)


@respx.mock
async def test_sync_app_polls_202_then_stores_csv(tmp_path, monkeypatch):
    monkeypatch.setenv("APPMETRICA_HOME", str(tmp_path))
    respx.get(MANAGEMENT_URL).respond(
        json={"applications": [{"id": 7, "time_zone_name": "UTC"}]}
    )
    respx.get(ACTIVE_URL).respond(json=[])
    row = event_row(1, epoch(2026, 9, 30, 10))
    route = respx.get(EVENTS_CSV_URL).mock(
        side_effect=[
            httpx.Response(202, text="Wait for result."),
            httpx.Response(200, text=csv_text("events", [row])),
        ]
    )
    lines: list[str] = []

    result = await service.sync_app(
        _config(logs_poll_interval=0, logs_poll_timeout=5),
        7,
        DAY,
        DAY,
        tables=["events"],
        progress=lines.append,
    )

    assert route.call_count == 2
    assert result.error is None
    assert result.fetched == [("events", "2026-09-30", 1)]
    assert result.skipped_closed == 0
    assert "events 2026-09-30: waiting 0s" in lines
    assert AppStore(tmp_path, 7).partition_path("events", DAY).exists()


@respx.mock
async def test_iap_report_uses_disk_fx_cache_without_network(tmp_path):
    store = _store(tmp_path)
    build(store, revenue_events=[revenue_row(1, epoch(2026, 9, 30, 10), "10", "EUR")])
    save_cache(tmp_path, _fx_archive(), date(2026, 9, 30))

    [day] = await service.iap_report(tmp_path, 1)

    assert day.day == DAY
    assert day.events == 1
    assert day.gross_usd == Decimal("12.50")  # EUR rate of 2026-09-29
    assert day.excluded == {"quantity": [], "no_fx": [], "invalid": []}


def _fx_archive() -> bytes:
    csv = "Date,USD,\n2026-09-28,1.20,\n2026-09-29,1.25,\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("eurofxref-hist.csv", csv)
    return buffer.getvalue()
