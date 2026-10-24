"""Device-level funnel over ``events``.

Steps are matched by exact ``event_name`` and exact first-level ``event_json``
parameter values (compared with DuckDB ``json_extract_string``; event JSON is
never parsed in Python). A device enters at its first step-1 event; every later
step must occur strictly after the previous one and no later than the entry
plus ``window``. The window is measured from the entry, never between steps.
Exact duplicate rows count once; a repeated adjacent step needs two distinct
occurrences with different timestamps. Entries whose window extends past the
last event in the data are excluded from conversions and counted separately.
"""
from __future__ import annotations

import bisect
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from appmetrica_cli.db import read_only
from appmetrica_cli.storage import AppStore


@dataclass(frozen=True)
class FunnelStep:
    event_name: str
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StepReach:
    index: int
    event_name: str
    devices: int
    step_conversion: float | None  # to this step from the previous one
    entry_conversion: float | None  # to this step from the entry


@dataclass(frozen=True)
class FunnelResult:
    steps: tuple[StepReach, ...]
    entries: int  # mature entries used in the conversions
    immature_entries: int  # entries excluded because their window is not observable


def funnel(
    store: AppStore,
    steps: Sequence[FunnelStep | Mapping[str, Any]],
    window: timedelta = timedelta(days=7),
) -> FunnelResult:
    if window <= timedelta(0):
        raise ValueError("window must be positive")
    parsed = [_step(spec) for spec in steps]
    if not parsed:
        raise ValueError("funnel needs at least one step")

    with read_only(store) as con:
        horizon = con.execute("SELECT max(ts_utc) FROM events").fetchone()[0]
        matches: list[dict[str, list[datetime]]] = []
        for step in parsed:
            matches.append(_step_matches(con, step))

    entries = 0
    immature = 0
    counts = [0] * len(parsed)
    if horizon is not None:
        for device, times in matches[0].items():
            entry = times[0]
            limit = entry + window
            if limit > horizon:
                immature += 1
                continue
            entries += 1
            reached = 1
            previous = entry
            for index in range(1, len(parsed)):
                candidates = matches[index].get(device)
                if not candidates:
                    break
                following = _next_after(candidates, previous, limit)
                if following is None:
                    break
                previous = following
                reached += 1
            for index in range(reached):
                counts[index] += 1

    reaches: list[StepReach] = []
    for index, step in enumerate(parsed):
        if index == 0:
            step_conversion = 1.0 if entries else None
        else:
            previous_count = counts[index - 1]
            step_conversion = counts[index] / previous_count if previous_count else None
        entry_conversion = counts[index] / entries if entries else None
        reaches.append(
            StepReach(
                index=index,
                event_name=step.event_name,
                devices=counts[index],
                step_conversion=step_conversion,
                entry_conversion=entry_conversion,
            )
        )
    return FunnelResult(steps=tuple(reaches), entries=entries, immature_entries=immature)


def _step_matches(con, step: FunnelStep) -> dict[str, list[datetime]]:
    conditions = ["event_name = ?"]
    params: list[Any] = [step.event_name]
    for key, value in step.params.items():
        conditions.append("json_extract_string(event_json, ?) = ?")
        params.extend([_json_path(key), _param_text(value)])
    rows = con.execute(
        f"""
        WITH dedup AS (SELECT DISTINCT * FROM events)
        SELECT DISTINCT appmetrica_device_id, ts_utc
        FROM dedup
        WHERE ts_utc IS NOT NULL
          AND appmetrica_device_id IS NOT NULL
          AND appmetrica_device_id <> ''
          AND {" AND ".join(conditions)}
        """,
        params,
    ).fetchall()
    matches: dict[str, list[datetime]] = {}
    for device, timestamp in rows:
        matches.setdefault(device, []).append(timestamp)
    for times in matches.values():
        times.sort()
    return matches


def _next_after(candidates: list[datetime], previous: datetime, limit: datetime) -> datetime | None:
    index = bisect.bisect_right(candidates, previous)
    if index >= len(candidates):
        return None
    candidate = candidates[index]
    return candidate if candidate <= limit else None


def _step(spec: FunnelStep | Mapping[str, Any]) -> FunnelStep:
    if isinstance(spec, FunnelStep):
        return spec
    if not isinstance(spec, Mapping):
        raise TypeError("a funnel step must be FunnelStep or a mapping")
    name = spec.get("event_name")
    if not isinstance(name, str) or not name:
        raise ValueError("a funnel step needs a non-empty event_name")
    params = spec.get("params") or {}
    if not isinstance(params, Mapping):
        raise TypeError("step params must be a mapping")
    return FunnelStep(event_name=name, params=dict(params))


def _param_text(value: Any) -> str:
    """Text form of a parameter value as ``json_extract_string`` returns it."""
    if value is None:
        raise ValueError("a funnel parameter value cannot be None")
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _json_path(key: str) -> str:
    if not key:
        raise ValueError("a funnel parameter key cannot be empty")
    escaped = key.replace("\\", "\\\\").replace('"', '\\"')
    return f'$."{escaped}"'
