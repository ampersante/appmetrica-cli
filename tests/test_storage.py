from __future__ import annotations

import csv
import hashlib
import io
import random
from datetime import date, datetime, timedelta, timezone

import duckdb
import pytest

from appmetrica_cli.client import AppMetricaError
from appmetrica_cli.loader import days_to_fetch, is_closed, sync
from appmetrica_cli.storage import TABLES, AppStore, StorageError

MSK = "Europe/Moscow"
UTC = timezone.utc


def _csv(table: str, rows: list[dict]) -> str:
    fields = TABLES[table].fields
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(fields)
    for r in rows:
        w.writerow([r.get(f, "") for f in fields])
    return buf.getvalue()


def _epoch(y, mo, d, h, mi, s=0) -> str:
    return str(int(datetime(y, mo, d, h, mi, s, tzinfo=UTC).timestamp()))


def _event(dev: str, ts: str, rts: str | None = None, **kw) -> dict:
    return {"appmetrica_device_id": dev, "event_name": "e", "event_timestamp": ts,
            "event_receive_timestamp": rts or ts, **kw}


def _sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def store(tmp_path):
    s = AppStore(tmp_path, 42)
    s.set_time_zone(MSK)
    return s


NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


# ── partitions ──────────────────────────────────────────────────────


def test_partition_bytes_do_not_depend_on_row_order(store, tmp_path):
    rows = [_event(f"{i % 7}", _epoch(2026, 9, 15, 10, i % 60), event_json='{"a":"x,\\"y\\"\nz"}')
            for i in range(300)]
    d = date(2026, 9, 15)
    store.write_partition("events", d, _csv("events", rows), NOW)
    h1 = _sha(store.partition_path("events", d))
    random.Random(1).shuffle(rows)
    store.write_partition("events", d, _csv("events", rows), NOW)
    assert _sha(store.partition_path("events", d)) == h1


def test_replace_is_whole_and_keeps_multiline_cells(store):
    d = date(2026, 9, 15)
    store.write_partition("events", d, _csv("events", [_event("1", "1"), _event("2", "2")]), NOW)
    n = store.write_partition(
        "events", d, _csv("events", [_event("3", "3", event_json="line1\nline2, \"q\"")]), NOW
    )
    assert n == 1
    got = duckdb.sql(
        f"SELECT appmetrica_device_id, event_json FROM '{store.partition_path('events', d)}'"
    ).fetchall()
    assert got == [("3", "line1\nline2, \"q\"")]


def test_header_mismatch_leaves_partition_untouched(store):
    d = date(2026, 9, 15)
    store.write_partition("events", d, _csv("events", [_event("1", "1")]), NOW)
    before = _sha(store.partition_path("events", d))
    with pytest.raises(StorageError):
        store.write_partition("events", d, "event_name\nx\n", NOW)
    assert _sha(store.partition_path("events", d)) == before
    assert not list(store.partition_path("events", d).parent.glob("*.tmp"))


def test_empty_partition_and_bom(store):
    d = date(2026, 9, 15)
    assert store.write_partition("revenue_events", d, "﻿" + _csv("revenue_events", []), NOW) == 0
    assert store.fetched("revenue_events") == {d: NOW}


def test_time_zone_change_rejected(store):
    with pytest.raises(StorageError):
        store.set_time_zone("UTC")


# ── closed rule / what to fetch (A4) ────────────────────────────────


def test_closed_needs_24h_after_local_day_end():
    d = date(2026, 9, 15)  # ends 2026-09-15 21:00 UTC in Moscow
    assert not is_closed(d, datetime(2026, 9, 16, 20, 59, 59, tzinfo=UTC), MSK)
    assert is_closed(d, datetime(2026, 9, 16, 21, 0, tzinfo=UTC), MSK)


def test_midday_fetch_is_refetched_later():
    d = date(2026, 9, 1)
    midday = datetime(2026, 9, 1, 11, 0, tzinfo=UTC)
    later = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
    assert days_to_fetch(d, d, {d: midday}, MSK, later) == [d]
    assert days_to_fetch(d, d, {d: later}, MSK, later) == []


def test_days_to_fetch_missing_recent_and_future():
    fetched_at = datetime(2026, 9, 25, 0, 0, tzinfo=UTC)
    fetched = {date(2026, 9, 1) + timedelta(days=i): fetched_at for i in range(20)}  # 1..20 closed
    got = days_to_fetch(date(2026, 9, 1), date(2026, 10, 5), fetched, MSK, NOW)
    # 21..28: missing; 22..28 would be refreshed anyway; nothing after local today (28th)
    assert got == [date(2026, 9, 21) + timedelta(days=i) for i in range(8)]


def test_last_seven_days_always_refetched():
    fetched = {date(2026, 9, 22): NOW + timedelta(days=5)}
    assert days_to_fetch(date(2026, 9, 22), date(2026, 9, 22), fetched, MSK, NOW) == [date(2026, 9, 22)]
    fetched = {date(2026, 9, 21): NOW + timedelta(days=5)}
    assert days_to_fetch(date(2026, 9, 21), date(2026, 9, 21), fetched, MSK, NOW) == []


# ── build: time rules and day boundaries (A5) ───────────────────────


def _build_one(tmp_path, tz, rows):
    s = AppStore(tmp_path / tz.replace("/", "_"), 1)
    s.set_time_zone(tz)
    s.write_partition("events", date(2026, 9, 15), _csv("events", rows), NOW)
    s.build()
    con = duckdb.connect(str(s.db_path), read_only=True)
    try:
        return con.execute(
            "SELECT appmetrica_device_id, day, time_fixed, ts_utc FROM events ORDER BY appmetrica_device_id"
        ).fetchall()
    finally:
        con.close()


def test_moscow_day_boundary(tmp_path):
    rows = [_event("a", _epoch(2026, 9, 15, 20, 59)), _event("b", _epoch(2026, 9, 15, 21, 1))]
    got = _build_one(tmp_path, MSK, rows)
    assert [r[1] for r in got] == [date(2026, 9, 15), date(2026, 9, 16)]


def test_utc_app_uses_utc_days(tmp_path):
    rows = [_event("a", _epoch(2026, 9, 15, 20, 59)), _event("b", _epoch(2026, 9, 15, 23, 59, 59)),
            _event("c", _epoch(2026, 9, 16, 0, 0))]
    got = _build_one(tmp_path, "UTC", rows)
    assert [r[1] for r in got] == [date(2026, 9, 15), date(2026, 9, 15), date(2026, 9, 16)]


def test_effective_time_rule_d2(tmp_path):
    rcv = _epoch(2026, 9, 15, 12, 0)
    rows = [
        _event("a", _epoch(2026, 9, 15, 12, 4), rcv),   # 4 min ahead: device time kept
        _event("b", _epoch(2026, 9, 15, 12, 6), rcv),   # 6 min ahead: receive time
        _event("c", _epoch(2026, 9, 8, 13, 0), rcv),    # < 7 d behind: device time kept
        _event("d", _epoch(2026, 9, 8, 11, 0), rcv),    # > 7 d behind: receive time
        _event("e", "", rcv),                           # no device time: receive time
    ]
    got = {r[0]: (r[2], r[3]) for r in _build_one(tmp_path, "UTC", rows)}
    assert got["a"] == (False, datetime(2026, 9, 15, 12, 4))
    assert got["b"] == (True, datetime(2026, 9, 15, 12, 0))
    assert got["c"] == (False, datetime(2026, 9, 8, 13, 0))
    assert got["d"] == (True, datetime(2026, 9, 15, 12, 0))
    assert got["e"] == (True, datetime(2026, 9, 15, 12, 0))


def test_build_creates_all_tables_even_without_data(store):
    counts = store.build()
    assert counts == {t: 0 for t in TABLES}
    con = duckdb.connect(str(store.db_path), read_only=True)
    assert con.execute("SELECT * FROM app_meta").fetchall() == [(42, MSK)]
    con.close()


# ── sync with a fake API ────────────────────────────────────────────


class FakeClient:
    def __init__(self, tz=MSK, fail_on: tuple[str, date] | None = None):
        self.tz = tz
        self.fail_on = fail_on
        self.calls: list[tuple] = []
        self.pending: list[dict] = []

    async def list_applications(self):
        return [{"id": 42, "time_zone_name": self.tz}]

    async def active_exports(self, app_id):
        return self.pending

    async def export_logs_csv(self, app_id, table, fields, since, until, date_dimension, progress=None):
        self.calls.append((table, since, until, date_dimension, tuple(fields)))
        if self.fail_on and (table, since[:10]) == (self.fail_on[0], self.fail_on[1].isoformat()):
            raise AppMetricaError(420, "Rate limit exceeded", "x")
        return _csv(table, [])


async def test_sync_windows_abut_and_use_receive_dimension(tmp_path):
    client = FakeClient()
    store = AppStore(tmp_path, 42)
    rep = await sync(client, store, date(2026, 9, 1), date(2026, 9, 2), ["events"], now=lambda: NOW)
    assert rep.error is None
    assert [c[1:4] for c in client.calls] == [
        ("2026-09-01 00:00:00", "2026-09-01 23:59:59", "receive"),
        ("2026-09-02 00:00:00", "2026-09-02 23:59:59", "receive"),
    ]
    assert client.calls[0][4] == TABLES["events"].fields
    # second run: both days closed and older than 7 days -> no requests
    client.calls.clear()
    rep = await sync(client, store, date(2026, 9, 1), date(2026, 9, 2), ["events"], now=lambda: NOW)
    assert client.calls == [] and rep.skipped_closed == 2


async def test_sync_stops_on_error_and_keeps_done_partitions(tmp_path):
    client = FakeClient(fail_on=("events", date(2026, 9, 2)))
    store = AppStore(tmp_path, 42)
    rep = await sync(client, store, date(2026, 9, 1), date(2026, 9, 3), ["events"], now=lambda: NOW)
    assert rep.error and "Rate limit" in rep.error
    assert set(store.fetched("events")) == {date(2026, 9, 1)}
    client.fail_on = None
    await sync(client, store, date(2026, 9, 1), date(2026, 9, 3), ["events"], now=lambda: NOW)
    assert set(store.fetched("events")) == {date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)}


async def test_sync_does_not_queue_behind_unfinished_export(tmp_path):
    client = FakeClient()
    client.pending = [{"request_id": 1, "status": 0, "create_time": "2026-09-28 17:36:16"}]
    store = AppStore(tmp_path, 42)
    rep = await sync(client, store, date(2026, 9, 1), date(2026, 9, 2), ["events"], now=lambda: NOW)
    assert client.calls == []
    assert rep.error and "unfinished export" in rep.error and "2026-09-28 17:36:16" in rep.error
    assert store.fetched("events") == {}


async def test_sync_rejects_app_not_visible(tmp_path):
    with pytest.raises(StorageError):
        await sync(FakeClient(), AppStore(tmp_path, 7), date(2026, 9, 1), date(2026, 9, 1), now=lambda: NOW)
