from __future__ import annotations

import io
import zipfile
from datetime import date, datetime, timezone
from decimal import Decimal

import httpx
import pytest
import respx

from appmetrica_cli.fx import (
    FX_URL,
    FxError,
    FxTable,
    load_fx,
    parse_archive,
    save_cache,
)

CSV = (
    "Date,USD,CHF,TRY,\n"
    "2026-09-18,1.10,0.95,40.0,\n"
    "2026-09-17,1.20,0.94,41.0,\n"
    "2026-09-16,1.30,0.93,42.0,\n"
    "2026-09-14,1.40,0.92,43.0,\n"
)

XML = """<?xml version="1.0" encoding="UTF-8"?>
<DataSet>
  <Cube time="2026-09-18">
    <Cube currency="USD" rate="1.10"/>
    <Cube currency="TRY" rate="40"/>
  </Cube>
  <Cube time="2026-09-17">
    <Cube currency="USD" rate="1.20"/>
  </Cube>
</DataSet>
"""


def _zip(member: str, text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(member, text)
    return buffer.getvalue()


def test_parse_csv_with_holiday_and_weekend_fallback():
    table = parse_archive(_zip("eurofxref-hist.csv", CSV))

    assert (table.first_day, table.last_day) == (date(2026, 9, 14), date(2026, 9, 18))
    assert table.publication_day(date(2026, 9, 18)) == date(2026, 9, 17)
    assert table.publication_day(date(2026, 9, 14)) is None
    assert table.usd_rate("USD", date(2026, 9, 20)) == Decimal(1)
    assert table.usd_rate("CHF", date(2026, 9, 15)) == Decimal("1.40") / Decimal("0.92")
    assert table.usd_rate("TRY", date(2026, 9, 20)) == Decimal("1.10") / Decimal("40.0")
    assert table.usd_rate("EUR", date(2026, 9, 20)) == Decimal("1.10")
    assert table.usd_rate("XXX", date(2026, 9, 20)) is None
    assert table.usd_rate("CHF", date(2026, 9, 14)) is None
    assert table.convert_to_usd(Decimal("100"), "TRY", date(2026, 9, 20)) == Decimal("2.75")


def test_parse_xml_member():
    table = parse_archive(_zip("eurofxref-hist.xml", XML))
    assert table.usd_rate("TRY", date(2026, 9, 19)) == Decimal("1.10") / Decimal("40")
    assert table.usd_rate("CHF", date(2026, 9, 19)) is None


def test_parse_rejects_bad_archives():
    with pytest.raises(FxError, match="valid zip"):
        parse_archive(b"not a zip")
    with pytest.raises(FxError, match="neither a CSV nor an XML"):
        parse_archive(_zip("rates.txt", "x"))


@respx.mock
async def test_cache_is_used_without_network(tmp_path):
    route = respx.get(FX_URL).mock(side_effect=AssertionError("network used"))
    save_cache(tmp_path, _zip("eurofxref-hist.csv", CSV), date(2026, 9, 21))

    table = await load_fx(tmp_path, [date(2026, 9, 20)])

    assert not route.called
    assert table.usd_rate("TRY", date(2026, 9, 20)) == Decimal("1.10") / Decimal("40.0")


@respx.mock
async def test_download_once_then_use_cache(tmp_path):
    route = respx.get(FX_URL).mock(
        return_value=httpx.Response(200, content=_zip("eurofxref-hist.csv", CSV))
    )
    now = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)

    first = await load_fx(tmp_path, [date(2026, 9, 20)], now=now)
    second = await load_fx(tmp_path, [date(2026, 9, 20)], now=now)

    assert route.call_count == 1
    assert first.last_day == second.last_day == date(2026, 9, 18)
    assert (tmp_path / "fx" / "eurofxref-hist.zip").exists()


@respx.mock
async def test_stale_cache_is_refetched(tmp_path):
    old = _zip(
        "eurofxref-hist.csv",
        "Date,USD,\n2026-09-18,1.10,\n",
    )
    save_cache(tmp_path, old, date(2026, 9, 18))
    route = respx.get(FX_URL).mock(
        return_value=httpx.Response(200, content=_zip("eurofxref-hist.csv", CSV))
    )

    table = await load_fx(
        tmp_path,
        [date(2026, 9, 22)],
        now=datetime(2026, 9, 23, tzinfo=timezone.utc),
    )

    assert route.call_count == 1
    assert table.usd_rate("TRY", date(2026, 9, 22)) == Decimal("1.10") / Decimal("40.0")


@respx.mock
async def test_empty_needed_days_do_not_download(tmp_path):
    route = respx.get(FX_URL).mock(side_effect=AssertionError("network used"))
    table = await load_fx(tmp_path, [])
    assert table.by_day == {}
    assert not route.called


@respx.mock
async def test_network_error_is_loud(tmp_path):
    respx.get(FX_URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(FxError, match="Cannot download"):
        await load_fx(tmp_path, [date(2026, 9, 20)])


@respx.mock
async def test_format_error_is_loud(tmp_path):
    respx.get(FX_URL).mock(return_value=httpx.Response(200, content=b"broken"))
    with pytest.raises(FxError, match="valid zip"):
        await load_fx(tmp_path, [date(2026, 9, 20)])


def test_usd_rate_table_without_publication_is_none():
    table = FxTable({date(2026, 9, 18): {"USD": Decimal("1.1")}})
    assert table.usd_rate("EUR", date(2026, 9, 18)) is None
    assert table.usd_rate("USD", date(2026, 9, 18)) == Decimal(1)
