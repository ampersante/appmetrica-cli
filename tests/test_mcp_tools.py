"""MCP local tools over the service layer (thin adapters: param mapping only)."""
from __future__ import annotations

from datetime import date

import pytest

from appmetrica_cli import service
from appmetrica_cli.loader import SyncReport
from appmetrica_cli.server import (
    build,
    build_funnel,
    explore_event_params,
    get_ad_revenue,
    get_daily_metrics,
    get_iap_revenue,
    get_retention,
    list_events,
    reconcile,
    run_sql,
    sync,
)
from appmetrica_cli.storage import AppStore
from tests.helpers import build as build_store
from tests.helpers import epoch, event_row, session_row

APP = 123


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.delenv("APPMETRICA_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("APPMETRICA_OAUTH_TOKEN_FILE", raising=False)
    monkeypatch.setenv("APPMETRICA_HOME", str(tmp_path))
    store = AppStore(tmp_path, APP)
    store.set_time_zone("UTC")
    build_store(
        store,
        events=[
            event_row(1, epoch(2026, 9, 30, 10), "open", {"screen": "shop"}),
            event_row(1, epoch(2026, 9, 30, 11), "buy", {"item": "sword"}),
            event_row(2, epoch(2026, 9, 30, 12), "open", {"screen": "shop"}),
        ],
        sessions_starts=[session_row(1, epoch(2026, 9, 30, 10))],
    )
    return store


async def test_local_tools_work_without_a_token(home):
    counts = await build(APP)
    assert counts["events"] == 3
    days = await get_daily_metrics(APP)
    assert days == [
        {"day": "2026-09-30", "dau": 2, "sessions": 1, "installs": 0}
    ]


async def test_run_sql_returns_rows_and_rejects_writes(home):
    ok = await run_sql(APP, "SELECT count(*) AS n FROM events")
    assert ok == {"columns": ["n"], "rows": [[3]], "truncated": False}
    rejected = await run_sql(APP, "DELETE FROM events")
    assert "error" in rejected


async def test_funnel_steps_and_window(home):
    result = await build_funnel(
        APP,
        '[{"event_name": "open", "params": {"screen": "shop"}}, {"event_name": "buy"}]',
        "1h",
    )
    assert result["entries"] == 1  # device 2: entry + 1 h past the horizon
    assert result["immature_entries"] == 1
    assert [step["devices"] for step in result["steps"]] == [1, 1]


async def test_events_and_params_discovery(home):
    events = await list_events(APP)
    assert {(row["event_name"], row["rows"]) for row in events} == {("buy", 1), ("open", 2)}
    params = await explore_event_params(APP, "open")
    assert params["parameters"] == [
        {
            "key": "screen",
            "rows": 2,
            "types": {"VARCHAR": 2},
            "top_values": [{"value": "shop", "count": 2}],
        }
    ]


async def test_empty_revenue_and_retention_reports(home):
    assert await get_iap_revenue(APP) == []
    assert await get_ad_revenue(APP) == []
    retention = await get_retention(APP, [1])
    assert retention["days"] == [1]
    assert retention["cohorts"] == []
    assert retention["immature"] == {"1": 0}


async def test_sync_and_reconcile_delegate_to_service(home, monkeypatch):
    monkeypatch.setenv("APPMETRICA_OAUTH_TOKEN", "test-token-123")

    async def fake_sync(config, app_id, start, end, tables=None, progress=None):
        return SyncReport(fetched=[("events", "2026-09-30", 3)], skipped_closed=2)

    monkeypatch.setattr(service, "sync_app", fake_sync)
    synced = await sync(APP, "2026-09-30", "2026-09-30")
    assert synced == {
        "fetched": [{"table": "events", "day": "2026-09-30", "rows": 3}],
        "skipped_closed": 2,
        "error": None,
    }

    monkeypatch.setattr(
        service,
        "reconcile_a1",
        lambda config, home_dir_value, app_id, start, end: _fake_reconcile(),
    )
    result = await reconcile(APP, "2026-09-30", "2026-09-30")
    assert result["matched_days"] == 1
    assert result["days"]["2026-09-30"]["users"] == {"ours": 2, "appmetrica": 2, "diff": 0}


async def _fake_reconcile():
    return service.ReconcileResult(
        days={date(2026, 9, 30): {"users": service.MetricComparison(2, 2, 0)}},
        matched_days=1,
        mismatched_days=0,
    )
