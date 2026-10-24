from __future__ import annotations

import io
import json
import zipfile
from datetime import date

import httpx
import pytest
import respx
from click.testing import CliRunner

from appmetrica_cli import config as config_module
from appmetrica_cli.cli import main
from appmetrica_cli.fx import save_cache
from appmetrica_cli.storage import AppStore
from tests.helpers import (
    FETCHED_AT,
    ad_row,
    build,
    csv_text,
    epoch,
    event_row,
    install_row,
    make_store,
    revenue_row,
    session_row,
)

REPORT_URL = "https://api.appmetrica.yandex.ru/stat/v1/data"
MANAGEMENT_URL = "https://api.appmetrica.yandex.com/management/v1/applications"
ACTIVE_URL = "https://api.appmetrica.yandex.ru/logs/v1/export/get_active_queries"
EVENTS_CSV_URL = "https://api.appmetrica.yandex.ru/logs/v1/export/events.csv"
DAY = "2026-09-30"


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Built store with data on 2026-09-30, 2026-10-01 and 2026-10-10."""
    monkeypatch.setenv("APPMETRICA_HOME", str(tmp_path / "app-1"))
    monkeypatch.delenv("APPMETRICA_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("APPMETRICA_OAUTH_TOKEN_FILE", raising=False)
    db = make_store(tmp_path)
    build(
        db,
        events=[event_row(1, epoch(2026, 9, 30, 10)), event_row(2, epoch(2026, 9, 30, 11))],
        sessions_starts=[
            session_row(2, epoch(2026, 9, 30, 12)),
            session_row(3, epoch(2026, 9, 30, 13)),
        ],
        installations=[install_row(1, epoch(2026, 9, 30, 9))],
        revenue_events=[revenue_row(1, epoch(2026, 9, 30, 10), "10", "EUR")],
        ad_revenue_events=[ad_row(1, epoch(2026, 9, 30, 10), "0.5", network="admob")],
    )
    db.write_partition(
        "events",
        date(2026, 10, 1),
        csv_text("events", [event_row(1, epoch(2026, 10, 1, 10), payload={"level": 3})]),
        FETCHED_AT,
    )
    db.write_partition(
        "events",
        date(2026, 10, 10),
        csv_text("events", [event_row(9, epoch(2026, 10, 10, 10))]),
        FETCHED_AT,
    )
    db.build()
    save_cache(tmp_path / "app-1", _fx_archive(), date(2026, 9, 30))
    return db


def _fx_archive() -> bytes:
    csv = "Date,USD,\n2026-09-28,1.20,\n2026-09-29,1.25,\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("eurofxref-hist.csv", csv)
    return buffer.getvalue()


def _invoke(*args: str):
    return CliRunner().invoke(main, list(args))


def _json(result) -> object:
    assert result.exit_code == 0, result.stderr + result.stdout
    return json.loads(result.stdout)


def test_report(store):
    data = _json(
        _invoke("report", "--app", "1", "--from", DAY, "--to", DAY)
    )
    assert data == [{"day": DAY, "dau": 3, "sessions": 2, "installs": 1}]


def test_retention(store):
    data = _json(
        _invoke("retention", "--app", "1", "--days", "1", "--from", DAY, "--to", DAY)
    )
    assert data == {
        "days": [1],
        "cohorts": [{"day": DAY, "size": 1, "returned": {"1": 1}}],
        "immature": {"1": 0},
    }


def test_build_counts(store):
    data = _json(_invoke("build", "--app", "1"))
    assert data == {
        "events": 4,
        "installations": 1,
        "sessions_starts": 2,
        "revenue_events": 1,
        "ad_revenue_events": 1,
    }


def test_revenue_iap_and_ad(store):
    data = _json(_invoke("revenue", "--app", "1", "--from", DAY, "--to", DAY, "--ad"))
    assert data["iap"][0]["gross_usd"] == "12.50"
    assert data["iap"][0]["events"] == 1
    assert data["ad"] == [
        {"day": DAY, "currency": "USD", "network": None, "events": 1, "amount": 0.5}
    ]


def test_by_network_requires_ad(store):
    result = _invoke("revenue", "--app", "1", "--by-network")
    assert result.exit_code == 2
    assert "--by-network requires --ad" in result.stderr


def test_funnel(store):
    steps = json.dumps([{"event_name": "step"}])
    data = _json(
        _invoke("funnel", "--app", "1", "--steps", steps, "--window", "7d")
    )
    assert data["entries"] == 2
    assert data["immature_entries"] == 1
    assert data["steps"][0]["event_name"] == "step"
    assert data["steps"][0]["devices"] == 2


def test_funnel_rejects_bad_window(store):
    result = _invoke("funnel", "--app", "1", "--steps", "[]", "--window", "week")
    assert result.exit_code == 2
    assert "--window must look like" in result.stderr


def test_sql(store):
    data = _json(_invoke("sql", "--app", "1", "SELECT count(*) AS n FROM events"))
    assert data == {"columns": ["n"], "rows": [[4]], "truncated": False}


def test_events_list_and_parameters(store):
    listed = _json(_invoke("events", "--app", "1"))
    assert listed == [
        {
            "event_name": "step",
            "rows": 4,
            "devices": 3,
            "first_day": DAY,
            "last_day": "2026-10-10",
        }
    ]
    parameters = _json(_invoke("events", "--app", "1", "--event", "step"))
    assert parameters["event"] == "step"
    [level] = parameters["parameters"]
    assert level["key"] == "level"
    assert level["rows"] == 1
    assert level["top_values"] == [{"value": "3", "count": 1}]


def test_report_without_build_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("APPMETRICA_HOME", str(tmp_path / "empty"))
    result = _invoke("report", "--app", "1")
    assert result.exit_code == 1
    assert "run build" in result.stderr


def test_sync_requires_token(monkeypatch):
    monkeypatch.setattr(config_module, "get_token", lambda: (None, "none"))
    result = _invoke("sync", "--app", "1")
    assert result.exit_code == 2
    assert "No AppMetrica token" in result.stderr


def test_reconcile_requires_token(monkeypatch):
    monkeypatch.setattr(config_module, "get_token", lambda: (None, "none"))
    result = _invoke("reconcile", "--app", "1")
    assert result.exit_code == 2
    assert "No AppMetrica token" in result.stderr


@respx.mock
def test_sync_command(store, monkeypatch):
    monkeypatch.setenv("APPMETRICA_OAUTH_TOKEN", "test-token")
    respx.get(MANAGEMENT_URL).respond(
        json={"applications": [{"id": 1, "time_zone_name": "UTC"}]}
    )
    respx.get(ACTIVE_URL).respond(json=[])
    respx.get(EVENTS_CSV_URL).respond(
        text=csv_text("events", [event_row(1, epoch(2026, 10, 20, 10))])
    )

    data = _json(
        _invoke("sync", "--app", "1", "--from", "2026-10-20", "--to", "2026-10-20", "--tables", "events")
    )

    assert data["fetched"] == [{"table": "events", "day": "2026-10-20", "rows": 1}]
    assert data["skipped_closed"] == 0
    assert data["error"] is None


@respx.mock
def test_reconcile_command(store, monkeypatch):
    monkeypatch.setenv("APPMETRICA_OAUTH_TOKEN", "test-token")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["metrics"] == "ym:ge:users,ym:ge:sessions":
            return httpx.Response(200, json={"data": [{"dimensions": [{"name": DAY}], "metrics": [3, 2]}]})
        return httpx.Response(200, json={"data": [{"dimensions": [{"name": DAY}], "metrics": [1]}]})

    respx.get(REPORT_URL).mock(side_effect=handler)

    data = _json(_invoke("reconcile", "--app", "1", "--from", DAY, "--to", DAY))

    assert data["matched_days"] == 1
    assert data["mismatched_days"] == 0
    assert data["days"][DAY]["users"] == {"ours": 3, "appmetrica": 3, "diff": 0}
    assert data["days"][DAY]["installDevices"] == {"ours": 1, "appmetrica": 1, "diff": 0}
