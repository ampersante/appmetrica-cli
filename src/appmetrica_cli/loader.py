"""Logs API loader: partitions = (table, receive day D in the app time zone).

Each partition is requested with date_dimension=receive over D 00:00:00–23:59:59
(app time zone, E-032) and replaces the stored partition whole. A partition is
closed once it was fetched at least 24 h after the end of D; every sync re-fetches
missing and not-closed days plus the last REFRESH_DAYS days.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from appmetrica_cli.client import AppMetricaClient, AppMetricaError
from appmetrica_cli.storage import TABLES, AppStore, StorageError

CLOSE_MARGIN = timedelta(hours=24)
REFRESH_DAYS = 7


def day_end(day: date, tz: str) -> datetime:
    """Start of the next local day, as an aware datetime."""
    return datetime.combine(day + timedelta(days=1), time(0), tzinfo=ZoneInfo(tz))


def is_closed(day: date, fetched_at: datetime, tz: str) -> bool:
    return fetched_at >= day_end(day, tz) + CLOSE_MARGIN


def days_to_fetch(
    start: date, end: date, fetched: dict[date, datetime], tz: str, now: datetime
) -> list[date]:
    today = now.astimezone(ZoneInfo(tz)).date()
    last = min(end, today)
    refresh_from = today - timedelta(days=REFRESH_DAYS - 1)
    out = []
    d = start
    while d <= last:
        f = fetched.get(d)
        if f is None or not is_closed(d, f, tz) or d >= refresh_from:
            out.append(d)
        d += timedelta(days=1)
    return out


@dataclass
class SyncReport:
    fetched: list[tuple[str, str, int]] = field(default_factory=list)  # (table, day, rows)
    skipped_closed: int = 0
    error: str | None = None


async def app_time_zone(client: AppMetricaClient, app_id: int) -> str:
    for app in await client.list_applications():
        if int(app.get("id", -1)) == int(app_id):
            tz = app.get("time_zone_name")
            if not tz:
                raise StorageError(f"App {app_id} has no time_zone_name")
            ZoneInfo(tz)  # fail early on an unknown zone
            return tz
    raise StorageError(f"App {app_id} is not visible with this token")


async def sync(
    client: AppMetricaClient,
    store: AppStore,
    start: date,
    end: date,
    tables: list[str] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    progress: Callable[[str], None] | None = None,
) -> SyncReport:
    """Fetch and replace partitions. Stops at the first API error; done partitions stay."""
    tables = tables or list(TABLES)
    tz = await app_time_zone(client, store.app_id)
    store.set_time_zone(tz)
    report = SyncReport()
    for table in tables:
        spec = TABLES[table]
        fetched = store.fetched(table)
        days = days_to_fetch(start, end, fetched, tz, now())
        span = (min(end, now().astimezone(ZoneInfo(tz)).date()) - start).days + 1
        report.skipped_closed += max(span, 0) - len(days)
        for d in days:
            if progress:
                progress(f"{table} {d}")
            try:
                # AppMetrica prepares exports one after another; adding more behind a stuck
                # one only grows the queue (E-045). Stop and let the next sync retry.
                pending = await client.active_exports(store.app_id)
            except AppMetricaError as e:
                report.error = f"{table} {d}: cannot read the Logs API queue: {e.message}"
                return report
            if pending:
                oldest = min(str(q.get("create_time", "?")) for q in pending)
                report.error = (
                    f"{table} {d}: Logs API still has {len(pending)} unfinished export(s) for this "
                    f"app (oldest created {oldest}); not adding more. Run sync again later."
                )
                return report
            # Taken before the request: the export holds at most what was received by then.
            requested_at = now()
            try:
                text = await client.export_logs_csv(
                    store.app_id, table, list(spec.fields),
                    f"{d} 00:00:00", f"{d} 23:59:59",
                    date_dimension="receive",
                    progress=(lambda s, t=table, d=d: progress(f"{t} {d}: waiting {s}s"))
                    if progress else None,
                )
            except AppMetricaError as e:
                report.error = f"{table} {d}: {e.message}"
                return report
            rows = store.write_partition(table, d, text, requested_at)
            report.fetched.append((table, d.isoformat(), rows))
    return report
