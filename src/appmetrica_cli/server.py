from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated

import httpx
from fastmcp import Context, FastMCP
from fastmcp.server.lifespan import lifespan
from mcp.types import ToolAnnotations
from pydantic import Field

from appmetrica_cli.analytics import (
    build_funnel,
    compare_cohorts,
    compute_retention,
)
from appmetrica_cli.cache import (
    EVENTS_FIELDS,
    INSTALLATIONS_FIELDS,
    EventCache,
)
from appmetrica_cli.client import AppMetricaClient, AppMetricaError
from appmetrica_cli.config import AppMetricaConfig
from appmetrica_cli.metrics import (
    DIMENSIONS,
    METRICS,
    PRESETS,
    combine_filters,
    quote_value,
    resolve_dimensions,
)

READ_ONLY = ToolAnnotations(readOnlyHint=True)


# ── Lifespan ────────────────────────────────────────────────────────


@lifespan
async def app_lifespan(server):
    config = AppMetricaConfig.from_env()
    http_client = httpx.AsyncClient(
        headers={"Authorization": f"OAuth {config.oauth_token}"},
        timeout=httpx.Timeout(config.request_timeout),
    )
    client = AppMetricaClient(http_client, config)
    cache = EventCache(config.cache_dir)
    try:
        yield {"client": client, "cache": cache, "config": config}
    finally:
        cache.close_all()
        await http_client.aclose()


mcp = FastMCP(
    name="AppMetrica Analytics",
    instructions=(
        "Game analytics MCP for AppMetrica. Read-only access to metrics, events, "
        "funnels, retention, and cohort analysis. Use list_apps to find app IDs, "
        "then query reports or build funnels. For complex analytics (funnels, deep "
        "retention, cohort comparison), data is cached locally in DuckDB — first "
        "query may take longer while syncing from Logs API."
    ),
    lifespan=app_lifespan,
)


# ── Helpers ─────────────────────────────────────────────────────────


def _default_dates(
    date_from: str | None, date_to: str | None
) -> tuple[str, str]:
    d2 = date_to or (date.today() - timedelta(days=1)).isoformat()
    d1 = date_from or (date.fromisoformat(d2) - timedelta(days=29)).isoformat()
    return d1, d2


async def _ensure_events_cached(
    ctx: Context, app_id: int, date_from: str, date_to: str
) -> None:
    cache: EventCache = ctx.lifespan_context["cache"]
    client: AppMetricaClient = ctx.lifespan_context["client"]
    cached = cache.get_cached_date_range(app_id, "events")
    if not cached or date_from < cached[0] or date_to > cached[1]:
        events = await client.export_logs(
            app_id, "events", EVENTS_FIELDS, date_from, date_to
        )
        cache.store_events(app_id, events)


async def _ensure_installations_cached(
    ctx: Context, app_id: int, date_from: str, date_to: str
) -> None:
    cache: EventCache = ctx.lifespan_context["cache"]
    client: AppMetricaClient = ctx.lifespan_context["client"]
    cached = cache.get_cached_date_range(app_id, "installations")
    if not cached or date_from < cached[0] or date_to > cached[1]:
        data = await client.export_logs(
            app_id, "installations", INSTALLATIONS_FIELDS, date_from, date_to
        )
        cache.store_installations(app_id, data)


async def _run_preset(
    ctx: Context,
    preset_key: str,
    app_id: int,
    date_from: str | None,
    date_to: str | None,
    group_by: str | None,
    filters: str | None,
) -> dict:
    client: AppMetricaClient = ctx.lifespan_context["client"]
    preset = PRESETS[preset_key]
    d1, d2 = _default_dates(date_from, date_to)
    try:
        dims = resolve_dimensions(preset_key, group_by)
    except ValueError as e:
        return {"error": str(e), "status_code": 400}
    try:
        return await client.get_report(
            app_id, preset.metrics, dims, d1, d2, filters,
            limit=100, sort=preset.default_sort,
        )
    except AppMetricaError as e:
        return {"error": e.message, "status_code": e.status_code}


# ══════════════════════════════════════════════════════════════════════
#  DISCOVERY TOOLS
# ══════════════════════════════════════════════════════════════════════


@mcp.tool(annotations=READ_ONLY)
async def list_apps(ctx: Context) -> list[dict]:
    """List all AppMetrica applications accessible with the configured token."""
    client: AppMetricaClient = ctx.lifespan_context["client"]
    try:
        return await client.list_applications()
    except AppMetricaError as e:
        return [{"error": e.message}]


@mcp.tool(annotations=READ_ONLY)
async def list_metrics() -> dict:
    """Show Reporting API metrics and dimensions accepted by the live API.

    Metrics and dimensions in one request must share a prefix (ym:ge:, ym:s:,
    ym:ce:, ym:cr:, ym:i:); only filters may mix prefixes. Retention and revenue
    are not available in the Reporting API.
    """
    return {
        "metrics": METRICS,
        "dimensions": DIMENSIONS,
        "group_by": {k: p.group_by for k, p in PRESETS.items() if p.group_by},
    }


# ══════════════════════════════════════════════════════════════════════
#  UNIVERSAL QUERY TOOLS
# ══════════════════════════════════════════════════════════════════════


@mcp.tool(annotations=READ_ONLY)
async def get_report(
    app_id: Annotated[int, "AppMetrica application ID"],
    metrics: Annotated[list[str], "Metrics sharing one prefix, e.g. ['ym:ge:users']"],
    dimensions: Annotated[list[str] | None, "Dimensions with the same prefix as metrics"] = None,
    date_from: Annotated[str | None, "Start date YYYY-MM-DD (default: 30 days ago)"] = None,
    date_to: Annotated[str | None, "End date YYYY-MM-DD (default: yesterday)"] = None,
    filters: Annotated[str | None, "Filter expression, e.g. ym:ge:regionCountryName=='US'"] = None,
    limit: Annotated[int, Field(description="Max rows", ge=1, le=100000)] = 100,
    sort: Annotated[str | None, "Sort metric, prefix with - for desc"] = None,
    ctx: Context = None,
) -> dict:
    """Custom Reporting API query — any metrics, dimensions, filters, dates.

    Use for any combination not covered by preset tools.
    Call list_metrics first to see available metric/dimension names.
    """
    client: AppMetricaClient = ctx.lifespan_context["client"]
    d1, d2 = _default_dates(date_from, date_to)
    try:
        return await client.get_report(
            app_id, metrics, dimensions, d1, d2, filters, limit, sort
        )
    except AppMetricaError as e:
        return {"error": e.message, "status_code": e.status_code}


@mcp.tool(annotations=READ_ONLY)
async def get_drilldown(
    app_id: Annotated[int, "AppMetrica application ID"],
    metrics: Annotated[list[str], "Metrics with ym:ge: prefix"],
    dimensions: Annotated[list[str], "Dimensions for tree hierarchy"],
    date_from: Annotated[str | None, "Start date YYYY-MM-DD"] = None,
    date_to: Annotated[str | None, "End date YYYY-MM-DD"] = None,
    filters: Annotated[str | None, "Filter expression"] = None,
    parent_id: Annotated[str | None, "Parent row ID to drill into"] = None,
    limit: Annotated[int, Field(description="Max rows", ge=1, le=100000)] = 100,
    ctx: Context = None,
) -> dict:
    """Hierarchical dimension drill — explore country→city, device→model, etc."""
    client: AppMetricaClient = ctx.lifespan_context["client"]
    d1, d2 = _default_dates(date_from, date_to)
    try:
        return await client.get_drilldown(
            app_id, metrics, dimensions, d1, d2, filters, parent_id, limit
        )
    except AppMetricaError as e:
        return {"error": e.message, "status_code": e.status_code}


# ══════════════════════════════════════════════════════════════════════
#  PRESET REPORT TOOLS
# ══════════════════════════════════════════════════════════════════════


@mcp.tool(annotations=READ_ONLY)
async def get_traffic(
    app_id: Annotated[int, "AppMetrica application ID"],
    date_from: Annotated[str | None, "Start date YYYY-MM-DD"] = None,
    date_to: Annotated[str | None, "End date YYYY-MM-DD"] = None,
    group_by: Annotated[str | None, "Group by: date (default), country, version"] = None,
    filters: Annotated[str | None, "Filter expression"] = None,
    ctx: Context = None,
) -> dict:
    """DAU and sessions. Group by date, country, or version. Installs: get_installs."""
    return await _run_preset(ctx, "traffic", app_id, date_from, date_to, group_by, filters)


@mcp.tool(annotations=READ_ONLY)
async def get_installs(
    app_id: Annotated[int, "AppMetrica application ID"],
    date_from: Annotated[str | None, "Start date YYYY-MM-DD"] = None,
    date_to: Annotated[str | None, "End date YYYY-MM-DD"] = None,
    group_by: Annotated[str | None, "Group by: date (default), source, campaign, country"] = None,
    filters: Annotated[str | None, "Filter expression"] = None,
    ctx: Context = None,
) -> dict:
    """Devices with an install (ym:i:installDevices). Group by date, source, campaign, or country."""
    return await _run_preset(ctx, "installs", app_id, date_from, date_to, group_by, filters)


@mcp.tool(annotations=READ_ONLY)
async def get_crashes(
    app_id: Annotated[int, "AppMetrica application ID"],
    date_from: Annotated[str | None, "Start date YYYY-MM-DD"] = None,
    date_to: Annotated[str | None, "End date YYYY-MM-DD"] = None,
    group_by: Annotated[str | None, "Group by: date (default), version"] = None,
    filters: Annotated[str | None, "Filter expression"] = None,
    ctx: Context = None,
) -> dict:
    """Crash count and crash devices."""
    return await _run_preset(ctx, "crashes", app_id, date_from, date_to, group_by, filters)


@mcp.tool(annotations=READ_ONLY)
async def get_geo(
    app_id: Annotated[int, "AppMetrica application ID"],
    date_from: Annotated[str | None, "Start date YYYY-MM-DD"] = None,
    date_to: Annotated[str | None, "End date YYYY-MM-DD"] = None,
    filters: Annotated[str | None, "Filter expression"] = None,
    ctx: Context = None,
) -> dict:
    """Users and sessions by country."""
    return await _run_preset(ctx, "geo", app_id, date_from, date_to, None, filters)


@mcp.tool(annotations=READ_ONLY)
async def get_events(
    app_id: Annotated[int, "AppMetrica application ID"],
    event_name: Annotated[str | None, "Specific event name, or None for top events"] = None,
    date_from: Annotated[str | None, "Start date YYYY-MM-DD"] = None,
    date_to: Annotated[str | None, "End date YYYY-MM-DD"] = None,
    group_by: Annotated[str | None, "Group by: event (default), date"] = None,
    filters: Annotated[str | None, "Filter expression"] = None,
    ctx: Context = None,
) -> dict:
    """Custom event analytics. Without event_name: top events. With event_name: that event only."""
    event_filter = f"ym:ce:eventLabel=={quote_value(event_name)}" if event_name else None
    return await _run_preset(
        ctx, "events_top", app_id, date_from, date_to, group_by,
        combine_filters(filters, event_filter),
    )


@mcp.tool(annotations=READ_ONLY)
async def get_devices(
    app_id: Annotated[int, "AppMetrica application ID"],
    date_from: Annotated[str | None, "Start date YYYY-MM-DD"] = None,
    date_to: Annotated[str | None, "End date YYYY-MM-DD"] = None,
    filters: Annotated[str | None, "Filter expression"] = None,
    ctx: Context = None,
) -> dict:
    """Users by OS and device model."""
    return await _run_preset(ctx, "devices", app_id, date_from, date_to, None, filters)


@mcp.tool(annotations=READ_ONLY)
async def get_versions(
    app_id: Annotated[int, "AppMetrica application ID"],
    date_from: Annotated[str | None, "Start date YYYY-MM-DD"] = None,
    date_to: Annotated[str | None, "End date YYYY-MM-DD"] = None,
    filters: Annotated[str | None, "Filter expression"] = None,
    ctx: Context = None,
) -> dict:
    """Users and sessions by app version."""
    return await _run_preset(ctx, "versions", app_id, date_from, date_to, None, filters)


# ══════════════════════════════════════════════════════════════════════
#  ADVANCED ANALYTICS TOOLS (Logs API + DuckDB)
# ══════════════════════════════════════════════════════════════════════


@mcp.tool(annotations=READ_ONLY)
async def build_funnel_report(
    app_id: Annotated[int, "AppMetrica application ID"],
    steps: Annotated[
        list[str],
        "Event names in funnel order, e.g. ['tutorial_start', 'tutorial_complete', 'first_purchase']",
    ],
    date_from: Annotated[str, "Start date YYYY-MM-DD"],
    date_to: Annotated[str, "End date YYYY-MM-DD"],
    country: Annotated[str | None, "Filter by country ISO code, e.g. 'US'"] = None,
    source: Annotated[str | None, "Filter by traffic source/tracker name"] = None,
    time_window_hours: Annotated[
        int, Field(description="Max hours between first and last step", ge=1, le=720)
    ] = 48,
    ctx: Context = None,
) -> dict:
    """Build event funnel from raw data. First call syncs events from Logs API (may take time).

    Returns users per step, conversion rates, and drop-off.
    """
    try:
        await _ensure_events_cached(ctx, app_id, date_from, date_to)
        if source:
            await _ensure_installations_cached(ctx, app_id, date_from, date_to)
    except AppMetricaError as e:
        return {"error": f"Failed to sync data: {e.message}", "status_code": e.status_code}

    cache: EventCache = ctx.lifespan_context["cache"]
    db = cache._get_db(app_id)
    step_dicts = [{"event_name": s} for s in steps]
    return build_funnel(db, step_dicts, date_from, date_to, country, source, time_window_hours)


@mcp.tool(annotations=READ_ONLY)
async def compare_cohorts_report(
    app_id: Annotated[int, "AppMetrica application ID"],
    cohort_a: Annotated[
        dict, "Filters for cohort A, e.g. {'country': 'US', 'source': 'facebook'}"
    ],
    cohort_b: Annotated[
        dict, "Filters for cohort B, e.g. {'country': 'US', 'source': 'applovin'}"
    ],
    metrics: Annotated[
        list[str],
        "Metrics to compare: retention_d1, retention_d7, avg_sessions, total_events, user_count",
    ],
    date_from: Annotated[str, "Cohort install date start YYYY-MM-DD"],
    date_to: Annotated[str, "Cohort install date end YYYY-MM-DD"],
    ctx: Context = None,
) -> list[dict]:
    """Compare two user cohorts side-by-side. Syncs raw data on first call."""
    try:
        await _ensure_events_cached(ctx, app_id, date_from, date_to)
        await _ensure_installations_cached(ctx, app_id, date_from, date_to)
    except AppMetricaError as e:
        return [{"error": f"Failed to sync data: {e.message}"}]

    cache: EventCache = ctx.lifespan_context["cache"]
    db = cache._get_db(app_id)
    return compare_cohorts(db, cohort_a, cohort_b, metrics, date_from, date_to)


@mcp.tool(annotations=READ_ONLY)
async def get_deep_retention(
    app_id: Annotated[int, "AppMetrica application ID"],
    days: Annotated[list[int], "Retention days, e.g. [1, 3, 7, 14, 28]"],
    cohort_from: Annotated[str, "Cohort start date YYYY-MM-DD"],
    cohort_to: Annotated[str, "Cohort end date YYYY-MM-DD"],
    country: Annotated[str | None, "Filter by country ISO code"] = None,
    source: Annotated[str | None, "Filter by traffic source/tracker name"] = None,
    ratio: Annotated[
        str | None,
        "Compute ratio between two days, e.g. 'd3/d1' → D3 retention / D1 retention",
    ] = None,
    ctx: Context = None,
) -> dict:
    """Deep retention from raw data. Supports any day combo, source/geo filters, ratios.

    First call syncs events + installations from Logs API.
    """
    try:
        await _ensure_events_cached(ctx, app_id, cohort_from, cohort_to)
        await _ensure_installations_cached(ctx, app_id, cohort_from, cohort_to)
    except AppMetricaError as e:
        return {"error": f"Failed to sync data: {e.message}"}

    cache: EventCache = ctx.lifespan_context["cache"]
    db = cache._get_db(app_id)
    retention_data = compute_retention(db, days, cohort_from, cohort_to, country, source)

    result: dict = {"retention": retention_data}

    if ratio:
        parts = ratio.lower().replace(" ", "").split("/")
        if len(parts) == 2:
            num_day = int(parts[0].lstrip("d"))
            den_day = int(parts[1].lstrip("d"))
            ratios = []
            for row in retention_data:
                num_val = row["retention"].get(num_day, 0.0)
                den_val = row["retention"].get(den_day, 0.0)
                r = (num_val / den_val) if den_val > 0 else 0.0
                ratios.append({
                    "cohort_date": row["cohort_date"],
                    "ratio": round(r, 4),
                    f"d{num_day}": num_val,
                    f"d{den_day}": den_val,
                })
            result["ratio"] = {
                "formula": ratio,
                "data": ratios,
                "average": round(
                    sum(r["ratio"] for r in ratios) / len(ratios), 4
                )
                if ratios
                else 0.0,
            }

    return result


@mcp.tool(annotations=READ_ONLY)
async def sync_events(
    app_id: Annotated[int, "AppMetrica application ID"],
    date_from: Annotated[str, "Start date YYYY-MM-DD"],
    date_to: Annotated[str, "End date YYYY-MM-DD"],
    tables: Annotated[
        list[str],
        "Tables to sync: events, installations",
    ] = ["events", "installations"],
    ctx: Context = None,
) -> dict:
    """Sync raw data from Logs API into local DuckDB cache.

    Run before multiple funnel/retention queries to avoid repeated syncs.
    """
    synced = []
    errors = []
    for table in tables:
        try:
            if table == "events":
                await _ensure_events_cached(ctx, app_id, date_from, date_to)
            elif table == "installations":
                await _ensure_installations_cached(ctx, app_id, date_from, date_to)
            synced.append(table)
        except AppMetricaError as e:
            errors.append({"table": table, "error": e.message})

    cache: EventCache = ctx.lifespan_context["cache"]
    ranges = {}
    for table in tables:
        r = cache.get_cached_date_range(app_id, table)
        ranges[table] = {"from": r[0], "to": r[1]} if r else None

    return {"synced": synced, "errors": errors, "cached_ranges": ranges}


# ══════════════════════════════════════════════════════════════════════
#  PROMPTS
# ══════════════════════════════════════════════════════════════════════


@mcp.prompt()
def daily_analytics_check(app_id: int) -> str:
    """Template for a daily game analytics check."""
    return (
        f"Run a daily analytics check for AppMetrica app {app_id}. "
        "Get traffic, installs, and crashes reports for the last 7 days. "
        "Summarize key trends. Flag anomalies: DAU or install drops >20%, crash "
        "rate spikes. Provide actionable recommendations."
    )


@mcp.prompt()
def compare_periods(app_id: int, focus: str = "traffic") -> str:
    """Template for comparing two time periods."""
    return (
        f"Compare the last 7 days vs the previous 7 days for app {app_id}, "
        f"focusing on {focus}. Calculate percentage changes for each metric "
        "and highlight significant shifts."
    )
