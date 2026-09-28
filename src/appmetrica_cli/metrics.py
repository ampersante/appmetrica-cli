"""Reporting API catalog. Every name here was accepted by the live API (E-038, E-040).

Reporting API rule: metrics and dimensions in one request must share a prefix
(ym:ge:, ym:s:, ym:ce:, ym:cr:, ym:i:); only filters may mix prefixes.
Retention and revenue are not available through the Reporting API; they are
computed from Logs API data locally.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ReportPreset:
    name: str
    description: str
    metrics: list[str]
    dimensions: list[str]
    default_sort: str | None = None
    group_by: dict[str, str] = field(default_factory=dict)


METRICS: dict[str, str] = {
    "ym:ge:users": "Unique devices (DAU when grouped by date); same as ym:ge:devices",
    "ym:ge:devices": "Unique devices",
    "ym:ge:sessions": "Session count",
    "ym:s:sessions": "Session count (sessions prefix)",
    "ym:s:sessionsPerUser": "Average sessions per device",
    "ym:ce:users": "Devices that sent the custom event",
    "ym:ce:devices": "Devices that sent the custom event",
    "ym:ce:clientEvents": "Custom event count",
    "ym:ce:allEvents": "Custom event count",
    "ym:cr:crashes": "Crash count",
    "ym:cr:crashDevices": "Devices with crashes",
    "ym:i:installDevices": "Devices with an install",
}

DIMENSIONS: dict[str, str] = {
    "ym:ge:date": "Date",
    "ym:ge:regionCountry": "Country (ID)",
    "ym:ge:regionCountryName": "Country",
    "ym:ge:regionCityName": "City",
    "ym:ge:appVersion": "App version",
    "ym:ge:operatingSystem": "OS (code)",
    "ym:ge:operatingSystemInfo": "OS name",
    "ym:ge:mobileDeviceBranding": "Device brand",
    "ym:ge:mobileDeviceModel": "Device model",
    "ym:s:date": "Date",
    "ym:ce:date": "Date",
    "ym:ce:eventLabel": "Custom event name",
    "ym:cr:date": "Date",
    "ym:cr:appVersion": "App version",
    "ym:i:date": "Date",
    "ym:i:publisher": "Install source (ID)",
    "ym:i:publisherName": "Install source",
    "ym:i:campaign": "Tracker campaign (ID)",
    "ym:i:campaignName": "Tracker campaign",
    "ym:i:regionCountryName": "Country",
}


PRESETS: dict[str, ReportPreset] = {
    "traffic": ReportPreset(
        name="Traffic Overview",
        description="DAU and sessions",
        metrics=["ym:ge:users", "ym:ge:sessions"],
        dimensions=["ym:ge:date"],
        group_by={
            "date": "ym:ge:date",
            "country": "ym:ge:regionCountryName",
            "version": "ym:ge:appVersion",
        },
    ),
    "installs": ReportPreset(
        name="Installs",
        description="Devices with an install",
        metrics=["ym:i:installDevices"],
        dimensions=["ym:i:date"],
        group_by={
            "date": "ym:i:date",
            "source": "ym:i:publisherName",
            "campaign": "ym:i:campaignName",
            "country": "ym:i:regionCountryName",
        },
    ),
    "crashes": ReportPreset(
        name="Crashes",
        description="Crash count, crash devices",
        metrics=["ym:cr:crashes", "ym:cr:crashDevices"],
        dimensions=["ym:cr:date"],
        group_by={"date": "ym:cr:date", "version": "ym:cr:appVersion"},
    ),
    "geo": ReportPreset(
        name="Geography",
        description="Devices/sessions by country",
        metrics=["ym:ge:users", "ym:ge:sessions"],
        dimensions=["ym:ge:regionCountryName"],
        default_sort="-ym:ge:users",
    ),
    "events_top": ReportPreset(
        name="Top Events",
        description="Custom event counts and devices by event name",
        metrics=["ym:ce:clientEvents", "ym:ce:users"],
        dimensions=["ym:ce:eventLabel"],
        default_sort="-ym:ce:clientEvents",
        group_by={"event": "ym:ce:eventLabel", "date": "ym:ce:date"},
    ),
    "devices": ReportPreset(
        name="Devices",
        description="Devices by OS and device model",
        metrics=["ym:ge:users"],
        dimensions=["ym:ge:operatingSystemInfo", "ym:ge:mobileDeviceModel"],
        default_sort="-ym:ge:users",
        group_by={
            "model": "ym:ge:mobileDeviceModel",
            "brand": "ym:ge:mobileDeviceBranding",
            "os": "ym:ge:operatingSystemInfo",
        },
    ),
    "versions": ReportPreset(
        name="App Versions",
        description="Devices/sessions by app version",
        metrics=["ym:ge:users", "ym:ge:sessions"],
        dimensions=["ym:ge:appVersion"],
        default_sort="-ym:ge:users",
    ),
}


def prefix(name: str) -> str:
    """'ym:ge:users' -> 'ym:ge:'."""
    return name.rsplit(":", 1)[0] + ":"


def resolve_dimensions(preset_key: str, group_by: str | None) -> list[str]:
    preset = PRESETS[preset_key]
    if group_by is None:
        return list(preset.dimensions)
    if group_by not in preset.group_by:
        allowed = ", ".join(preset.group_by) or "none"
        raise ValueError(f"group_by '{group_by}' not supported by '{preset_key}'; allowed: {allowed}")
    return [preset.group_by[group_by]]


def combine_filters(*parts: str | None) -> str | None:
    """AND-combine filter expressions, parenthesising each so OR inside one part stays scoped."""
    present = [p.strip() for p in parts if p and p.strip()]
    if not present:
        return None
    if len(present) == 1:
        return present[0]
    return " AND ".join(f"({p})" for p in present)


def quote_value(value: str) -> str:
    """Quote a literal for a Reporting API filter."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
