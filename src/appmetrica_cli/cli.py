"""`appmetrica` command line entry point."""
from __future__ import annotations

import asyncio
import getpass
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

import click
import httpx

from appmetrica_cli import auth, service
from appmetrica_cli.client import AppMetricaClient, AppMetricaError
from appmetrica_cli.config import AppMetricaConfig, home_dir
from appmetrica_cli.fx import FxError
from appmetrica_cli.sandbox import SandboxError
from appmetrica_cli.service import ServiceError
from appmetrica_cli.storage import TABLES, StorageError


def _out(data) -> None:
    click.echo(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _err(msg: str, code: int = 1) -> None:
    click.echo(msg, err=True)
    sys.exit(code)


def _message(exc: BaseException) -> str:
    return getattr(exc, "message", None) or str(exc)


def _run(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except (StorageError, ServiceError, FxError, SandboxError, AppMetricaError, ValueError, TypeError) as e:
        _err(_message(e))


def _arun(fn, *args, **kwargs):
    try:
        return asyncio.run(fn(*args, **kwargs))
    except (StorageError, ServiceError, FxError, SandboxError, AppMetricaError, ValueError, TypeError) as e:
        _err(_message(e))


def _today() -> date:
    # Heatmaps and Logs API days are app-time-zone days; for CLI defaults a UTC
    # date is enough and avoids a Management API request just for the time zone.
    return datetime.now(timezone.utc).date()


def _range(start: datetime | None, end: datetime | None) -> tuple[date, date]:
    last = end.date() if end else _today()
    first = start.date() if start else last - timedelta(days=6)
    if first > last:
        _err("--from must not be later than --to", 2)
    return first, last


def _int_list(text: str, option: str) -> tuple[int, ...]:
    try:
        values = tuple(int(part) for part in text.split(",") if part.strip())
    except ValueError:
        _err(f"{option} must be a comma-separated list of integers, got {text!r}", 2)
    if not values or any(value <= 0 for value in values):
        _err(f"{option} must contain positive integers, got {text!r}", 2)
    return values


def _window(text: str) -> timedelta:
    suffix = text[-1:]
    unit = {"d": "days", "h": "hours", "m": "minutes", "s": "seconds"}.get(suffix)
    if unit is None or not text[:-1].isdigit() or int(text[:-1]) <= 0:
        _err(f"--window must look like 7d, 24h, 30m or 45s, got {text!r}", 2)
    return timedelta(**{unit: int(text[:-1])})


@click.group()
@click.version_option(package_name="appmetrica-cli")
def main() -> None:
    """Read-only AppMetrica analytics CLI (unofficial)."""
    os.umask(0o077)


@main.group(name="auth")
def auth_group() -> None:
    """Manage the AppMetrica OAuth token (stored in the OS credential store)."""


async def _list_apps(token: str) -> list[dict]:
    config = AppMetricaConfig(oauth_token=token)
    async with httpx.AsyncClient(
        headers={"Authorization": f"OAuth {token}"}, timeout=config.request_timeout
    ) as http:
        return await AppMetricaClient(http, config).list_applications()


@auth_group.command("login")
@click.option("--stdin", "from_stdin", is_flag=True, help="Read the token from stdin instead of a hidden prompt.")
def auth_login(from_stdin: bool) -> None:
    """Store the token in the OS credential store after checking it against the API."""
    try:
        if from_stdin:
            # Raw bytes: text-mode reading would treat a stray \r as end of line and truncate.
            raw = click.get_binary_stream("stdin").read(auth.MAX_FILE_BYTES + 1)
            token = auth.token_from_bytes(raw, "stdin")
        else:
            token = auth.validate_token(getpass.getpass("AppMetrica OAuth token: "))
        store = auth.require_store()  # before any network call: never send a token we cannot keep
    except auth.AuthError as e:
        _err(e.message, e.exit_code)
    try:
        apps = asyncio.run(_list_apps(token))
    except AppMetricaError as e:
        _err(f"Token check failed: {e.message}", auth.EXIT_API_REJECTED)
    try:
        store.set(token)
    except auth.AuthError as e:
        _err(e.message, e.exit_code)
    _out({"stored_in": store.name, "apps_visible": len(apps)})


@auth_group.command("status")
def auth_status() -> None:
    """Show where the token comes from and whether it works (never the token itself)."""
    try:
        token, source = auth.get_token()
    except auth.AuthError as e:
        _out({"source": "error", "valid": False, "error": e.message})
        sys.exit(e.exit_code)
    if not token:
        _out({"source": "none", "valid": False})
        sys.exit(auth.EXIT_API_REJECTED)
    try:
        apps = asyncio.run(_list_apps(token))
        _out({"source": source, "valid": True, "apps_visible": len(apps)})
    except AppMetricaError as e:
        _out({"source": source, "valid": False, "error": e.message})
        sys.exit(auth.EXIT_API_REJECTED)


@auth_group.command("logout")
def auth_logout() -> None:
    """Delete the token from the OS credential store."""
    try:
        deleted = auth.delete_token()
    except auth.AuthError as e:
        _err(e.message, e.exit_code)
    result: dict = {"deleted": deleted}
    still = auth.external_source()
    if still:
        result["warning"] = f"{still} is still set and will keep supplying a token"
    _out(result)


@main.command("sync")
@click.option("--app", "app_id", type=int, required=True, help="AppMetrica application id.")
@click.option(
    "--from", "start", type=click.DateTime(formats=["%Y-%m-%d"]),
    help="First receive day as a UTC date (default: today-6).",
)
@click.option(
    "--to", "end", type=click.DateTime(formats=["%Y-%m-%d"]),
    help="Last receive day as a UTC date (default: today).",
)
@click.option("--tables", "tables", help="Comma-separated tables to load (default: all).")
def sync_command(app_id: int, start, end, tables: str | None) -> None:
    """Fetch Logs API partitions and store them locally (needs a token).

    Days are receive days in the app time zone; the default range uses UTC
    dates, so pass explicit dates if the app zone matters near midnight.
    """
    first, last = _range(start, end)
    selected = None
    if tables:
        selected = [name.strip() for name in tables.split(",") if name.strip()]
        unknown = [name for name in selected if name not in TABLES]
        if unknown:
            _err(f"unknown table(s): {', '.join(unknown)}; known: {', '.join(TABLES)}", 2)
    try:
        config = AppMetricaConfig.from_env()
    except auth.AuthError as e:
        _err(e.message, e.exit_code)
    report = _arun(
        service.sync_app, config, app_id, first, last,
        tables=selected, progress=lambda line: click.echo(line, err=True),
    )
    _out(
        {
            "app_id": app_id,
            "from": first.isoformat(),
            "to": last.isoformat(),
            "fetched": [
                {"table": table, "day": day, "rows": rows}
                for table, day, rows in report.fetched
            ],
            "skipped_closed": report.skipped_closed,
            "error": report.error,
        }
    )
    if report.error:
        _err(report.error, 1)


@main.command("build")
@click.option("--app", "app_id", type=int, required=True, help="AppMetrica application id.")
def build_command(app_id: int) -> None:
    """Rebuild the local analytics database from the stored partitions."""
    _out(_run(service.build_app, home_dir(), app_id))


@main.command("report")
@click.option("--app", "app_id", type=int, required=True, help="AppMetrica application id.")
@click.option(
    "--from", "start", type=click.DateTime(formats=["%Y-%m-%d"]),
    help="First day (default: today-6).",
)
@click.option(
    "--to", "end", type=click.DateTime(formats=["%Y-%m-%d"]),
    help="Last day (default: today).",
)
def report_command(app_id: int, start, end) -> None:
    """DAU, sessions and installs per day."""
    first, last = _range(start, end)
    rows = _run(service.daily_report, home_dir(), app_id, first, last)
    _out(
        [
            {
                "day": row.day.isoformat(),
                "dau": row.dau,
                "sessions": row.sessions,
                "installs": row.installs,
            }
            for row in rows
        ]
    )


@main.command("retention")
@click.option("--app", "app_id", type=int, required=True, help="AppMetrica application id.")
@click.option("--days", "days", default="1,7", show_default=True, help="Retention offsets, e.g. 1,7,30.")
@click.option(
    "--from", "start", type=click.DateTime(formats=["%Y-%m-%d"]),
    help="First cohort day (default: today-6).",
)
@click.option(
    "--to", "end", type=click.DateTime(formats=["%Y-%m-%d"]),
    help="Last cohort day (default: today).",
)
def retention_command(app_id: int, days: str, start, end) -> None:
    """Classic D-N retention per non-reinstall install cohort."""
    offsets = _int_list(days, "--days")
    first, last = _range(start, end)
    result = _run(service.retention_report, home_dir(), app_id, offsets, first, last)
    _out(
        {
            "days": list(result.days),
            "cohorts": [
                {
                    "day": cohort.day.isoformat(),
                    "size": cohort.size,
                    "returned": {str(n): value for n, value in cohort.returned.items()},
                }
                for cohort in result.cohorts
            ],
            "immature": {str(n): value for n, value in result.immature.items()},
        }
    )


@main.command("revenue")
@click.option("--app", "app_id", type=int, required=True, help="AppMetrica application id.")
@click.option(
    "--from", "start", type=click.DateTime(formats=["%Y-%m-%d"]),
    help="First day (default: today-6).",
)
@click.option(
    "--to", "end", type=click.DateTime(formats=["%Y-%m-%d"]),
    help="Last day (default: today).",
)
@click.option("--ad", "ad", is_flag=True, help="Also add ad revenue (raw amounts per currency).")
@click.option("--by-network", is_flag=True, help="Split ad revenue by network; requires --ad.")
def revenue_command(app_id: int, start, end, ad: bool, by_network: bool) -> None:
    """IAP revenue in USD; --ad adds ad revenue."""
    if by_network and not ad:
        _err("--by-network requires --ad", 2)
    first, last = _range(start, end)
    home = home_dir()
    iap = _arun(service.iap_report, home, app_id, first, last)
    result: dict = {
        "iap": [
            {
                "day": row.day.isoformat(),
                "events": row.events,
                "gross_usd": str(row.gross_usd),
                "excluded": {
                    bucket: [
                        {"currency": b.currency, "events": b.events, "amount": str(b.amount)}
                        for b in buckets
                    ]
                    for bucket, buckets in row.excluded.items()
                },
            }
            for row in iap
        ]
    }
    if ad:
        ad_rows = _run(service.ad_report, home, app_id, first, last, by_network=by_network)
        result["ad"] = [
            {
                "day": row.day.isoformat(),
                "currency": row.currency,
                "network": row.network,
                "events": row.events,
                "amount": row.amount,
            }
            for row in ad_rows
        ]
    _out(result)


@main.command("funnel")
@click.option("--app", "app_id", type=int, required=True, help="AppMetrica application id.")
@click.option(
    "--steps", "steps", required=True,
    help='JSON list of steps, e.g. [{"event_name": "open"}, {"event_name": "buy"}]',
)
@click.option("--window", "window", default="7d", show_default=True, help="Entry window: Nd, Nh, Nm or Ns.")
def funnel_command(app_id: int, steps: str, window: str) -> None:
    """Device-level funnel over loaded events; window is measured from the entry."""
    try:
        parsed = json.loads(steps)
    except json.JSONDecodeError as e:
        _err(f"--steps is not valid JSON: {e.msg}", 2)
    if not isinstance(parsed, list):
        _err("--steps must be a JSON list of steps", 2)
    result = _run(service.funnel_report, home_dir(), app_id, parsed, _window(window))
    _out(
        {
            "entries": result.entries,
            "immature_entries": result.immature_entries,
            "steps": [
                {
                    "index": row.index,
                    "event_name": row.event_name,
                    "devices": row.devices,
                    "step_conversion": row.step_conversion,
                    "entry_conversion": row.entry_conversion,
                }
                for row in result.steps
            ],
        }
    )


@main.command("sql")
@click.option("--app", "app_id", type=int, required=True, help="AppMetrica application id.")
@click.argument("query")
def sql_command(app_id: int, query: str) -> None:
    """Run one read-only SELECT/WITH statement over the analytics database."""
    result = _run(service.sql_query, home_dir(), app_id, query)
    _out(
        {
            "columns": list(result.columns),
            "rows": [list(row) for row in result.rows],
            "truncated": result.truncated,
        }
    )


@main.command("events")
@click.option("--app", "app_id", type=int, required=True, help="AppMetrica application id.")
@click.option("--event", "event_name", help="Show parameters of this event instead of the list.")
def events_command(app_id: int, event_name: str | None) -> None:
    """List loaded events, or the parameters of one event."""
    home = home_dir()
    if event_name is None:
        events = _run(service.events_overview, home, app_id)
        _out(
            [
                {
                    "event_name": row.event_name,
                    "rows": row.rows,
                    "devices": row.devices,
                    "first_day": row.first_day.isoformat() if row.first_day else None,
                    "last_day": row.last_day.isoformat() if row.last_day else None,
                }
                for row in events
            ]
        )
        return
    parameters = _run(service.event_params, home, app_id, event_name)
    _out(
        {
            "event": event_name,
            "parameters": [
                {
                    "key": row.key,
                    "rows": row.rows,
                    "types": row.types,
                    "top_values": [{"value": value, "count": count} for value, count in row.top_values],
                }
                for row in parameters
            ],
        }
    )


@main.command("reconcile")
@click.option("--app", "app_id", type=int, required=True, help="AppMetrica application id.")
@click.option(
    "--from", "start", type=click.DateTime(formats=["%Y-%m-%d"]),
    help="First day (default: today-6).",
)
@click.option(
    "--to", "end", type=click.DateTime(formats=["%Y-%m-%d"]),
    help="Last day (default: today).",
)
def reconcile_command(app_id: int, start, end) -> None:
    """Compare local DAU, sessions and installs with the Reporting API (needs a token)."""
    first, last = _range(start, end)
    try:
        config = AppMetricaConfig.from_env()
    except auth.AuthError as e:
        _err(e.message, e.exit_code)
    result = _arun(service.reconcile_a1, config, home_dir(), app_id, first, last)
    _out(
        {
            "from": first.isoformat(),
            "to": last.isoformat(),
            "days": {
                day.isoformat(): {
                    metric: {
                        "ours": comparison.ours,
                        "appmetrica": comparison.appmetrica,
                        "diff": comparison.diff,
                    }
                    for metric, comparison in comparisons.items()
                }
                for day, comparisons in result.days.items()
            },
            "matched_days": result.matched_days,
            "mismatched_days": result.mismatched_days,
        }
    )


@main.command("mcp")
def mcp_command() -> None:
    """Run the MCP server over stdio (for Claude Code / Desktop / Cursor)."""
    from appmetrica_cli.server import mcp

    mcp.run()
