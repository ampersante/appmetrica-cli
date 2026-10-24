"""Generic event and event-parameter discovery.

Nothing here knows any event name: both functions read whatever the loaded data
contains. Parameter keys are first-level ``event_json`` keys; types and top
values are computed with DuckDB JSON functions, never by parsing JSON in Python.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from appmetrica_cli.db import read_only
from appmetrica_cli.storage import AppStore

TOP_VALUES = 10


@dataclass(frozen=True)
class EventSummary:
    event_name: str
    rows: int
    devices: int
    first_day: date | None
    last_day: date | None


@dataclass(frozen=True)
class ParameterSummary:
    key: str
    rows: int
    types: dict[str, int]  # DuckDB JSON type -> number of values
    top_values: tuple[tuple[str, int], ...]  # value text -> frequency, top 10


def list_events(store: AppStore) -> list[EventSummary]:
    """Every non-empty event name with volume, reach and day span."""
    with read_only(store) as con:
        rows = con.execute(
            """
            SELECT event_name,
                   count(*) AS rows,
                   count(DISTINCT appmetrica_device_id) AS devices,
                   min(day) AS first_day,
                   max(day) AS last_day
            FROM events
            WHERE event_name IS NOT NULL AND event_name <> ''
            GROUP BY event_name
            ORDER BY event_name
            """
        ).fetchall()
    return [
        EventSummary(
            event_name=row[0],
            rows=row[1],
            devices=row[2],
            first_day=row[3],
            last_day=row[4],
        )
        for row in rows
    ]


def event_parameters(store: AppStore, event_name: str) -> list[ParameterSummary]:
    """First-level parameters of one event: value types and top values."""
    with read_only(store) as con:
        keys = [
            row[0]
            for row in con.execute(
                """
                SELECT DISTINCT unnest(json_keys(event_json)) AS key
                FROM events
                WHERE event_name = ? AND json_valid(event_json)
                ORDER BY key
                """,
                [event_name],
            ).fetchall()
            if row[0] is not None
        ]
        summaries = []
        for key in keys:
            path = _json_path(key)
            types = {
                row[0]: row[1]
                for row in con.execute(
                    """
                    SELECT json_type(json_extract(event_json, ?)) AS type,
                           count(*) AS n
                    FROM events
                    WHERE event_name = ?
                      AND json_valid(event_json)
                      AND json_type(json_extract(event_json, ?)) IS NOT NULL
                    GROUP BY type
                    ORDER BY n DESC, type
                    """,
                    [path, event_name, path],
                ).fetchall()
            }
            top_values = tuple(
                (row[0], row[1])
                for row in con.execute(
                    f"""
                    SELECT json_extract_string(event_json, ?) AS value,
                           count(*) AS n
                    FROM events
                    WHERE event_name = ?
                      AND json_valid(event_json)
                      AND json_extract_string(event_json, ?) IS NOT NULL
                    GROUP BY value
                    ORDER BY n DESC, value
                    LIMIT {TOP_VALUES}
                    """,
                    [path, event_name, path],
                ).fetchall()
            )
            summaries.append(
                ParameterSummary(
                    key=key,
                    rows=sum(types.values()),
                    types=types,
                    top_values=top_values,
                )
            )
    return summaries


def _json_path(key: str) -> str:
    escaped = key.replace("\\", "\\\\").replace('"', '\\"')
    return f'$."{escaped}"'
