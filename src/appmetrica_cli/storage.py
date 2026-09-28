"""Local raw storage: one Parquet file per (table, receive day in app time zone).

Layout: <home>/apps/<app_id>/<table>/day=YYYY-MM-DD.parquet
        <home>/apps/<app_id>/state.json      fetched_at per partition, app time zone
        <home>/apps/<app_id>/analytics.duckdb rebuilt from Parquet by build()

All values are stored as strings exactly as the Logs API returned them; IP columns
are never requested. Rows are sorted by all columns and written with fixed writer
settings, so identical rows give identical bytes.
"""
from __future__ import annotations

import csv
import io
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb


@dataclass(frozen=True)
class TableSpec:
    name: str
    fields: tuple[str, ...]
    device_ts: str   # UTC epoch seconds set by the device clock
    receive_ts: str  # UTC epoch seconds when AppMetrica received the row


_DEVICE = (
    "appmetrica_device_id", "profile_id", "os_name", "os_version", "device_manufacturer",
    "device_model", "device_type", "device_locale", "app_version_name", "app_build_number",
    "app_package_name", "country_iso_code", "city", "connection_type", "operator_name",
    "mcc", "mnc", "google_aid", "ios_ifa", "ios_ifv", "windows_aid",
)

TABLES: dict[str, TableSpec] = {
    t.name: t
    for t in [
        TableSpec(
            "events",
            ("event_name", "event_json", "event_datetime", "event_timestamp",
             "event_receive_datetime", "event_receive_timestamp", "session_id",
             "installation_id", "android_id", "original_device_model", "application_id") + _DEVICE,
            "event_timestamp", "event_receive_timestamp",
        ),
        TableSpec(
            "installations",
            ("install_datetime", "install_timestamp", "install_receive_datetime",
             "install_receive_timestamp", "is_reinstallation", "is_reattribution",
             "publisher_name", "publisher_id", "tracker_name", "tracking_id", "match_type",
             "attributed_touch_type", "click_datetime", "click_timestamp", "click_id",
             "click_url_parameters", "click_user_agent", "installation_id", "android_id",
             "oaid", "application_id") + _DEVICE,
            "install_timestamp", "install_receive_timestamp",
        ),
        TableSpec(
            "sessions_starts",
            ("session_id", "session_start_datetime", "session_start_timestamp",
             "session_start_receive_datetime", "session_start_receive_timestamp",
             "original_device_model", "application_id") + _DEVICE,
            "session_start_timestamp", "session_start_receive_timestamp",
        ),
        TableSpec(
            "revenue_events",
            ("event_name", "event_datetime", "event_timestamp", "event_receive_datetime",
             "event_receive_timestamp", "revenue_quantity", "revenue_price", "revenue_currency",
             "revenue_product_id", "revenue_order_id", "revenue_order_id_source",
             "is_revenue_verified", "session_id", "installation_id", "android_id",
             "appmetrica_sdk_version", "original_device_model") + _DEVICE,
            "event_timestamp", "event_receive_timestamp",
        ),
        TableSpec(
            "ad_revenue_events",
            ("ad_revenue_datetime", "ad_revenue_timestamp", "ad_revenue_receive_datetime",
             "ad_revenue_receive_timestamp", "ad_revenue", "ad_revenue_currency",
             "ad_revenue_type", "ad_revenue_data_source", "ad_revenue_network",
             "ad_revenue_placement_id", "ad_revenue_placement_name", "ad_revenue_unit_id",
             "ad_revenue_unit_name", "ad_revenue_precision", "ad_revenue_payload", "session_id",
             "installation_id", "android_id", "appmetrica_sdk_version",
             "original_device_model") + _DEVICE,
            "ad_revenue_timestamp", "ad_revenue_receive_timestamp",
        ),
    ]
}

PARQUET_OPTIONS = "FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 122880"


class StorageError(Exception):
    pass


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _sql_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


class AppStore:
    def __init__(self, home: Path, app_id: int):
        self.app_id = int(app_id)
        self.dir = Path(home) / "apps" / str(self.app_id)
        self.state_path = self.dir / "state.json"
        self.db_path = self.dir / "analytics.duckdb"

    # ── state ───────────────────────────────────────────────────────

    def load_state(self) -> dict:
        if not self.state_path.exists():
            return {"time_zone": None, "partitions": {}}
        return json.loads(self.state_path.read_text())

    def _save_state(self, state: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
        os.replace(tmp, self.state_path)

    def set_time_zone(self, tz: str) -> None:
        state = self.load_state()
        if state.get("time_zone") and state["time_zone"] != tz:
            # Partitions are cut by local day; a changed zone invalidates all of them.
            raise StorageError(
                f"App time zone changed from {state['time_zone']} to {tz}; "
                f"delete {self.dir} and sync again"
            )
        state["time_zone"] = tz
        self._save_state(state)

    def fetched(self, table: str) -> dict[date, datetime]:
        parts = self.load_state()["partitions"].get(table, {})
        return {
            date.fromisoformat(d): datetime.fromisoformat(ts)
            for d, ts in parts.items()
            if self.partition_path(table, date.fromisoformat(d)).exists()
        }

    # ── partitions ──────────────────────────────────────────────────

    def partition_path(self, table: str, day: date) -> Path:
        return self.dir / table / f"day={day.isoformat()}.parquet"

    def partition_files(self, table: str) -> list[Path]:
        return sorted((self.dir / table).glob("day=*.parquet"))

    def write_partition(self, table: str, day: date, csv_text: str, fetched_at: datetime) -> int:
        """Replace one partition with the rows of csv_text; returns the row count."""
        spec = TABLES[table]
        text = csv_text.lstrip("﻿")
        header = next(csv.reader(io.StringIO(text)), None)
        if header is None:
            raise StorageError(f"{table} {day}: empty response without a CSV header")
        if tuple(header) != spec.fields:
            raise StorageError(f"{table} {day}: CSV header does not match requested fields")

        dest = self.partition_path(table, day)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_csv = dest.with_suffix(".csv.tmp")
        tmp_parquet = dest.with_suffix(".parquet.tmp")
        tmp_csv.write_text(text, encoding="utf-8")
        try:
            columns = "{" + ", ".join(f"{_sql_str(f)}: 'VARCHAR'" for f in spec.fields) + "}"
            con = duckdb.connect()
            try:
                con.execute("SET threads=1")
                con.execute(
                    f"CREATE TABLE t AS SELECT * FROM read_csv({_sql_str(str(tmp_csv))}, "
                    f"header=true, auto_detect=false, delim=',', quote='\"', escape='\"', "
                    f"columns={columns})"
                )
                rows = con.execute("SELECT count(*) FROM t").fetchone()[0]
                con.execute(
                    f"COPY (SELECT * FROM t ORDER BY ALL) TO {_sql_str(str(tmp_parquet))} "
                    f"({PARQUET_OPTIONS})"
                )
            finally:
                con.close()
            os.replace(tmp_parquet, dest)
        finally:
            tmp_csv.unlink(missing_ok=True)
            tmp_parquet.unlink(missing_ok=True)

        # State is written after the file: a crash in between leaves an older
        # fetched_at, which only causes a re-fetch, never a stale "closed" mark.
        state = self.load_state()
        state["partitions"].setdefault(table, {})[day.isoformat()] = (
            fetched_at.astimezone(timezone.utc).isoformat()
        )
        self._save_state(state)
        return rows

    # ── analytics database ──────────────────────────────────────────

    def build(self) -> dict[str, int]:
        """Rebuild analytics.duckdb from all partitions and publish it atomically.

        Each table gets, besides the raw string columns:
          ts_device_utc, ts_receive_utc  TIMESTAMP (UTC)
          ts_utc      effective time (PRD D2): device time unless it is more than
                      5 minutes ahead of or more than 7 days behind receive time
          time_fixed  true when receive time replaced device time
          day         DATE of ts_utc in the app time zone
          receive_day DATE of ts_receive_utc in the app time zone
        """
        tz = self.load_state().get("time_zone")
        if not tz:
            raise StorageError("App time zone unknown; run sync first")
        tmp = self.db_path.with_suffix(".duckdb.tmp")
        tmp.unlink(missing_ok=True)
        counts: dict[str, int] = {}
        con = duckdb.connect(str(tmp))
        try:
            con.execute("SET threads=1")
            for name, spec in TABLES.items():
                files = self.partition_files(name)
                if files:
                    src = "read_parquet([" + ", ".join(_sql_str(str(f)) for f in files) + "])"
                else:
                    src = (
                        "(SELECT " + ", ".join(f"NULL::VARCHAR AS {_q(f)}" for f in spec.fields)
                        + " WHERE false)"
                    )
                dev = f"to_timestamp(TRY_CAST({_q(spec.device_ts)} AS BIGINT))"
                rcv = f"to_timestamp(TRY_CAST({_q(spec.receive_ts)} AS BIGINT))"
                cols = ", ".join(_q(f) for f in spec.fields)
                con.execute(f"""
                    CREATE TABLE {_q(name)} AS
                    WITH src AS (SELECT {cols}, {dev} AS d, {rcv} AS r FROM {src}),
                    eff AS (
                        SELECT *,
                            CASE WHEN d IS NULL THEN r
                                 WHEN r IS NULL THEN d
                                 WHEN d > r + INTERVAL 5 MINUTE THEN r
                                 WHEN d < r - INTERVAL 7 DAY THEN r
                                 ELSE d END AS e
                        FROM src
                    )
                    SELECT {cols},
                        timezone('UTC', d) AS ts_device_utc,
                        timezone('UTC', r) AS ts_receive_utc,
                        timezone('UTC', e) AS ts_utc,
                        (d IS DISTINCT FROM e) AS time_fixed,
                        CAST(timezone({_sql_str(tz)}, e) AS DATE) AS day,
                        CAST(timezone({_sql_str(tz)}, r) AS DATE) AS receive_day
                    FROM eff
                    ORDER BY ALL
                """)
                counts[name] = con.execute(f"SELECT count(*) FROM {_q(name)}").fetchone()[0]
            con.execute("CREATE TABLE app_meta (app_id BIGINT, time_zone VARCHAR)")
            con.execute("INSERT INTO app_meta VALUES (?, ?)", [self.app_id, tz])
            con.execute("CHECKPOINT")
        finally:
            con.close()
        os.replace(tmp, self.db_path)
        return counts
