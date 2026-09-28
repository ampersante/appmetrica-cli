from __future__ import annotations

import pytest

from appmetrica_cli.metrics import (
    DIMENSIONS,
    METRICS,
    PRESETS,
    combine_filters,
    prefix,
    quote_value,
    resolve_dimensions,
)

# Rejected by the live API (E-038, E-040); must never come back into the catalog.
KNOWN_INVALID = {
    "ym:ge:newUsers", "ym:ge:crashes", "ym:ge:retention1Day", "ym:ge:revenue", "ym:r:revenue",
    "ym:ge:trafficSource", "ym:ge:campaignName", "ym:ge:eventLabel",
    "ym:s:avgSessionDurationSeconds", "ym:s:sessionDuration", "ym:i:installations",
}


def test_all_presets_have_valid_structure():
    for key, preset in PRESETS.items():
        assert preset.name and preset.description, key
        assert preset.metrics and preset.dimensions, key
        names = preset.metrics + preset.dimensions + list(preset.group_by.values())
        for n in names:
            assert n in METRICS or n in DIMENSIONS, f"{key}: {n} not in catalog"
        # Reporting API rejects mixed prefixes in metrics/dimensions
        assert len({prefix(n) for n in names}) == 1, f"{key} mixes prefixes: {names}"
        if preset.default_sort:
            assert preset.default_sort.lstrip("-") in preset.metrics, key


def test_catalog_excludes_known_invalid_names():
    assert not KNOWN_INVALID & (METRICS.keys() | DIMENSIONS.keys())


def test_retention_and_revenue_not_served_by_reporting():
    assert "retention" not in PRESETS
    assert "revenue" not in PRESETS


def test_resolve_dimensions():
    assert resolve_dimensions("installs", None) == ["ym:i:date"]
    assert resolve_dimensions("installs", "source") == ["ym:i:publisherName"]
    with pytest.raises(ValueError, match="allowed"):
        resolve_dimensions("traffic", "source")  # ym:ge: has no traffic source (E-040)


def test_combine_filters_scopes_or():
    user = "ym:ge:regionCountryName=='US' OR ym:ge:regionCountryName=='TH'"
    event = "ym:ce:eventLabel=='x'"
    assert combine_filters(user, event) == f"({user}) AND ({event})"
    assert combine_filters(None, event) == event
    assert combine_filters("  ", None) is None


def test_quote_value_escapes():
    assert quote_value("a'b") == "'a\\'b'"
    assert quote_value("a\\b") == "'a\\\\b'"
