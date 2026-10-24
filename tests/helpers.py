"""Synthetic partitions for the Slice 1 metric, funnel and discovery tests."""
from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime, timezone
from pathlib import Path

from appmetrica_cli.storage import TABLES, AppStore

UTC = timezone.utc
FETCHED_AT = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
PARTITION_DAY = date(2026, 9, 30)


def epoch(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> str:
    return str(int(datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp()))


def csv_text(table: str, rows: list[dict]) -> str:
    fields = TABLES[table].fields
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(fields)
    for row in rows:
        writer.writerow([row.get(field, "") for field in fields])
    return buffer.getvalue()


def make_store(tmp_path: Path, time_zone: str = "UTC", app_id: int = 1) -> AppStore:
    store = AppStore(tmp_path / f"app-{app_id}", app_id)
    store.set_time_zone(time_zone)
    return store


def build(store: AppStore, **tables: list[dict]) -> None:
    for table, rows in tables.items():
        store.write_partition(table, PARTITION_DAY, csv_text(table, rows), FETCHED_AT)
    store.build()


def event_row(device: str | int, timestamp: str, name: str = "step", payload=None, **overrides) -> dict:
    row = {
        "appmetrica_device_id": str(device),
        "event_name": name,
        "event_timestamp": timestamp,
        "event_receive_timestamp": timestamp,
    }
    if payload is not None:
        row["event_json"] = payload if isinstance(payload, str) else json.dumps(payload)
    row.update(overrides)
    return row


def session_row(device: str | int, timestamp: str, **overrides) -> dict:
    row = {
        "appmetrica_device_id": str(device),
        "session_id": f"s-{device}-{timestamp}",
        "session_start_timestamp": timestamp,
        "session_start_receive_timestamp": timestamp,
    }
    row.update(overrides)
    return row


def install_row(
    device: str | int, timestamp: str, reinstallation: str = "false", **overrides
) -> dict:
    row = {
        "appmetrica_device_id": str(device),
        "installation_id": f"i-{device}",
        "is_reinstallation": reinstallation,
        "install_timestamp": timestamp,
        "install_receive_timestamp": timestamp,
    }
    row.update(overrides)
    return row


def revenue_row(
    device: str | int,
    timestamp: str,
    price: str,
    currency: str,
    quantity: str = "1",
    **overrides,
) -> dict:
    row = {
        "appmetrica_device_id": str(device),
        "event_name": "purchase",
        "event_timestamp": timestamp,
        "event_receive_timestamp": timestamp,
        "revenue_price": price,
        "revenue_currency": currency,
        "revenue_quantity": quantity,
    }
    row.update(overrides)
    return row


def ad_row(
    device: str | int,
    timestamp: str,
    amount: str,
    currency: str = "USD",
    network: str = "",
    **overrides,
) -> dict:
    row = {
        "appmetrica_device_id": str(device),
        "ad_revenue_timestamp": timestamp,
        "ad_revenue_receive_timestamp": timestamp,
        "ad_revenue": amount,
        "ad_revenue_currency": currency,
        "ad_revenue_network": network,
    }
    row.update(overrides)
    return row
