from __future__ import annotations

from pathlib import Path

import duckdb


EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    application_id BIGINT,
    app_build_number VARCHAR,
    ios_ifa VARCHAR,
    ios_ifv VARCHAR,
    android_id VARCHAR,
    google_aid VARCHAR,
    profile_id VARCHAR,
    os_name VARCHAR,
    os_version VARCHAR,
    device_manufacturer VARCHAR,
    device_model VARCHAR,
    device_type VARCHAR,
    device_locale VARCHAR,
    device_ipv6 VARCHAR,
    app_version_name VARCHAR,
    app_package_name VARCHAR,
    event_name VARCHAR,
    event_json VARCHAR,
    event_datetime VARCHAR,
    event_timestamp BIGINT,
    event_receive_datetime VARCHAR,
    event_receive_timestamp BIGINT,
    connection_type VARCHAR,
    operator_name VARCHAR,
    original_device_model VARCHAR,
    mcc INTEGER,
    mnc INTEGER,
    country_iso_code VARCHAR,
    city VARCHAR,
    appmetrica_device_id UBIGINT,
    installation_id VARCHAR,
    session_id BIGINT,
    windows_aid VARCHAR
)
"""

INSTALLATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS installations (
    application_id BIGINT,
    installation_id VARCHAR,
    publisher_name VARCHAR,
    publisher_id BIGINT,
    tracker_name VARCHAR,
    tracking_id BIGINT,
    click_timestamp BIGINT,
    click_datetime VARCHAR,
    click_ipv6 VARCHAR,
    click_url_parameters VARCHAR,
    click_id VARCHAR,
    click_user_agent VARCHAR,
    match_type VARCHAR,
    install_datetime VARCHAR,
    install_timestamp BIGINT,
    install_receive_datetime VARCHAR,
    install_receive_timestamp BIGINT,
    install_ipv6 VARCHAR,
    is_reinstallation BOOLEAN,
    is_reattribution BOOLEAN,
    ios_ifa VARCHAR,
    ios_ifv VARCHAR,
    android_id VARCHAR,
    google_aid VARCHAR,
    profile_id VARCHAR,
    os_name VARCHAR,
    os_version VARCHAR,
    oaid VARCHAR,
    device_manufacturer VARCHAR,
    device_model VARCHAR,
    device_type VARCHAR,
    device_locale VARCHAR,
    app_version_name VARCHAR,
    app_package_name VARCHAR,
    connection_type VARCHAR,
    operator_name VARCHAR,
    mcc INTEGER,
    mnc INTEGER,
    country_iso_code VARCHAR,
    city VARCHAR,
    appmetrica_device_id UBIGINT,
    attributed_touch_type VARCHAR,
    windows_aid VARCHAR
)
"""

EVENTS_FIELDS = [
    "application_id",
    "app_build_number",
    "ios_ifa",
    "ios_ifv",
    "android_id",
    "google_aid",
    "profile_id",
    "os_name",
    "os_version",
    "device_manufacturer",
    "device_model",
    "device_type",
    "device_locale",
    "device_ipv6",
    "app_version_name",
    "app_package_name",
    "event_name",
    "event_json",
    "event_datetime",
    "event_timestamp",
    "event_receive_datetime",
    "event_receive_timestamp",
    "connection_type",
    "operator_name",
    "original_device_model",
    "mcc",
    "mnc",
    "country_iso_code",
    "city",
    "appmetrica_device_id",
    "installation_id",
    "session_id",
    "windows_aid",
]

INSTALLATIONS_FIELDS = [
    "application_id",
    "installation_id",
    "publisher_name",
    "publisher_id",
    "tracker_name",
    "tracking_id",
    "click_timestamp",
    "click_datetime",
    "click_ipv6",
    "click_url_parameters",
    "click_id",
    "click_user_agent",
    "match_type",
    "install_datetime",
    "install_timestamp",
    "install_receive_datetime",
    "install_receive_timestamp",
    "install_ipv6",
    "is_reinstallation",
    "is_reattribution",
    "ios_ifa",
    "ios_ifv",
    "android_id",
    "google_aid",
    "profile_id",
    "os_name",
    "os_version",
    "oaid",
    "device_manufacturer",
    "device_model",
    "device_type",
    "device_locale",
    "app_version_name",
    "app_package_name",
    "connection_type",
    "operator_name",
    "mcc",
    "mnc",
    "country_iso_code",
    "city",
    "appmetrica_device_id",
    "attributed_touch_type",
    "windows_aid",
]


class EventCache:
    def __init__(self, cache_dir: Path | str, in_memory: bool = False):
        self.cache_dir = Path(cache_dir)
        if not in_memory:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._in_memory = in_memory
        self._connections: dict[int, duckdb.DuckDBPyConnection] = {}

    def _get_db(self, app_id: int) -> duckdb.DuckDBPyConnection:
        if app_id not in self._connections:
            if self._in_memory:
                conn = duckdb.connect(":memory:")
            else:
                db_path = self.cache_dir / f"app_{app_id}.duckdb"
                conn = duckdb.connect(str(db_path))
            conn.execute(EVENTS_SCHEMA)
            conn.execute(INSTALLATIONS_SCHEMA)
            self._connections[app_id] = conn
        return self._connections[app_id]

    def store_events(self, app_id: int, events: list[dict]) -> int:
        if not events:
            return 0
        db = self._get_db(app_id)
        cols = EVENTS_FIELDS
        placeholders = ", ".join(["?"] * len(cols))
        col_names = ", ".join(cols)
        rows = [[row.get(c) for c in cols] for row in events]
        db.executemany(
            f"INSERT INTO events ({col_names}) VALUES ({placeholders})",
            rows,
        )
        return len(rows)

    def store_installations(self, app_id: int, data: list[dict]) -> int:
        if not data:
            return 0
        db = self._get_db(app_id)
        cols = INSTALLATIONS_FIELDS
        placeholders = ", ".join(["?"] * len(cols))
        col_names = ", ".join(cols)
        rows = [[row.get(c) for c in cols] for row in data]
        db.executemany(
            f"INSERT INTO installations ({col_names}) VALUES ({placeholders})",
            rows,
        )
        return len(rows)

    def get_cached_date_range(
        self, app_id: int, table: str
    ) -> tuple[str, str] | None:
        db = self._get_db(app_id)
        dt_col = "event_datetime" if table == "events" else "install_datetime"
        result = db.execute(
            f"SELECT MIN({dt_col})::DATE::VARCHAR, MAX({dt_col})::DATE::VARCHAR FROM {table}"
        ).fetchone()
        if result and result[0]:
            return (result[0], result[1])
        return None

    def query(
        self, app_id: int, sql: str, params: list | None = None
    ) -> list[tuple]:
        db = self._get_db(app_id)
        if params:
            return db.execute(sql, params).fetchall()
        return db.execute(sql).fetchall()

    def close_all(self) -> None:
        for conn in self._connections.values():
            conn.close()
        self._connections.clear()
