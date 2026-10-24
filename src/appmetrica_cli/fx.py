"""ECB reference rates: archive download, disk cache and USD conversion.

The European Central Bank publishes one row per business day (EUR base) in
https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip. The archive currently
holds ``eurofxref-hist.csv``; an XML member with the same daily cubes is accepted
too. Weekend and holiday days have no row, so the rate of a day is the last
publication strictly before it.

Rates are stored as units of a currency per 1 EUR. ``usd_rate`` converts that to
a multiplier from the currency into USD through EUR. Network failures and
malformed archives raise :class:`FxError`; an empty or made-up table is never
returned silently.
"""
from __future__ import annotations

import bisect
import csv
import io
import json
import os
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.etree import ElementTree

import httpx

FX_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip"
CACHE_ARCHIVE = "eurofxref-hist.zip"
CACHE_META = "meta.json"


class FxError(Exception):
    """ECB rates are unavailable or the archive cannot be understood."""


@dataclass(frozen=True)
class FxTable:
    """Daily ECB rates: ``by_day[day][currency] = units per 1 EUR``."""

    by_day: dict[date, dict[str, Decimal]] = field(default_factory=dict)
    _days: tuple[date, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_days", tuple(sorted(self.by_day)))

    @property
    def first_day(self) -> date | None:
        return self._days[0] if self._days else None

    @property
    def last_day(self) -> date | None:
        return self._days[-1] if self._days else None

    def publication_day(self, day: date) -> date | None:
        """Last publication day strictly before ``day``."""
        index = bisect.bisect_left(self._days, day) - 1
        return self._days[index] if index >= 0 else None

    def usd_rate(self, currency: str, day: date) -> Decimal | None:
        """Multiplier that converts ``currency`` into USD on ``day``.

        USD is 1 without consulting the table. EUR uses the USD rate of the
        publication day. Any other currency uses the USD/EUR and currency/EUR
        rates of the last publication strictly before ``day``.
        """
        code = currency.strip().upper()
        if not code:
            return None
        if code == "USD":
            return Decimal(1)
        published = self.publication_day(day)
        if published is None:
            return None
        rates = self.by_day[published]
        usd = rates.get("USD")
        if usd is None or usd <= 0:
            return None
        if code == "EUR":
            return usd
        rate = rates.get(code)
        if rate is None or rate <= 0:
            return None
        return usd / rate

    def convert_to_usd(self, amount: Decimal, currency: str, day: date) -> Decimal | None:
        rate = self.usd_rate(currency, day)
        return None if rate is None else amount * rate


# ── parsing ─────────────────────────────────────────────────────────


def parse_archive(data: bytes) -> FxTable:
    """Parse the ECB history zip (CSV or XML member) into a table."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise FxError("ECB history archive is not a valid zip file") from None
    names = [n for n in archive.namelist() if not n.endswith("/")]
    member = _member(names, ".csv") or _member(names, ".xml")
    if member is None:
        raise FxError("ECB history archive contains neither a CSV nor an XML history file")
    try:
        raw = archive.read(member)
    except (KeyError, zipfile.BadZipFile, OSError):
        raise FxError("ECB history file cannot be read from the archive") from None
    if member.lower().endswith(".xml"):
        return _parse_xml(raw)
    return _parse_csv(raw)


def _member(names: list[str], suffix: str) -> str | None:
    preferred = [n for n in names if n.lower().endswith(f"hist{suffix}")]
    if preferred:
        return preferred[0]
    return next((n for n in names if n.lower().endswith(suffix)), None)


def _parse_csv(raw: bytes) -> FxTable:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise FxError("ECB history CSV is not UTF-8") from None
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise FxError("ECB history CSV is empty") from None
    codes = [cell.strip().upper() for cell in header[1:]]
    by_day: dict[date, dict[str, Decimal]] = {}
    for lineno, row in enumerate(reader, start=2):
        if not row or not row[0].strip():
            continue
        day = _parse_day(row[0], f"ECB history CSV row {lineno}")
        rates: dict[str, Decimal] = {}
        for code, value in zip(codes, row[1:]):
            if not code:
                continue
            text_value = value.strip()
            if not text_value or text_value.upper() == "N/A":
                continue
            try:
                number = Decimal(text_value)
            except InvalidOperation:
                raise FxError(
                    f"ECB history CSV row {lineno}: {code} is not a number"
                ) from None
            if not number.is_finite():
                raise FxError(f"ECB history CSV row {lineno}: {code} is not finite")
            rates[code] = number
        by_day[day] = rates
    if not by_day:
        raise FxError("ECB history CSV contains no rates")
    return FxTable(by_day)


def _parse_xml(raw: bytes) -> FxTable:
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        raise FxError("ECB history XML is malformed") from None
    by_day: dict[date, dict[str, Decimal]] = {}
    for cube in root.iter("Cube"):
        time = cube.get("time")
        if not time:
            continue
        day = _parse_day(time, "ECB history XML")
        rates: dict[str, Decimal] = {}
        for item in cube:
            code = (item.get("currency") or "").strip().upper()
            value = (item.get("rate") or "").strip()
            if not code or not value or value.upper() == "N/A":
                continue
            try:
                number = Decimal(value)
            except InvalidOperation:
                raise FxError(f"ECB history XML: {code} on {day} is not a number") from None
            if not number.is_finite():
                raise FxError(f"ECB history XML: {code} on {day} is not finite")
            rates[code] = number
        by_day[day] = rates
    if not by_day:
        raise FxError("ECB history XML contains no rates")
    return FxTable(by_day)


def _parse_day(value: str, where: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise FxError(f"{where}: invalid date {value!r}") from None


# ── disk cache ──────────────────────────────────────────────────────


def cache_dir(home: Path) -> Path:
    return Path(home) / "fx"


def _archive_path(home: Path) -> Path:
    return cache_dir(home) / CACHE_ARCHIVE


def _meta_path(home: Path) -> Path:
    return cache_dir(home) / CACHE_META


def save_cache(home: Path, data: bytes, fetched_on: date) -> None:
    """Atomically store the downloaded archive and the day it was fetched."""
    directory = cache_dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    archive = _archive_path(home)
    archive_tmp = archive.with_suffix(archive.suffix + ".tmp")
    archive_tmp.write_bytes(data)
    os.replace(archive_tmp, archive)
    meta = _meta_path(home)
    meta_tmp = meta.with_suffix(meta.suffix + ".tmp")
    meta_tmp.write_text(json.dumps({"fetched_on": fetched_on.isoformat()}, sort_keys=True))
    os.replace(meta_tmp, meta)


def read_cache(home: Path) -> tuple[FxTable, date | None] | None:
    """Read the cached archive; None when there is no cache."""
    archive = _archive_path(home)
    if not archive.exists():
        return None
    try:
        data = archive.read_bytes()
    except OSError:
        raise FxError("ECB history cache cannot be read") from None
    table = parse_archive(data)
    fetched_on: date | None = None
    meta = _meta_path(home)
    if meta.exists():
        try:
            fetched_on = date.fromisoformat(json.loads(meta.read_text())["fetched_on"])
        except (OSError, ValueError, KeyError, TypeError):
            fetched_on = None
    return table, fetched_on


def _covers(table: FxTable, fetched_on: date | None, needed: list[date]) -> bool:
    """Whether the cache can answer every needed day without a download."""
    if not needed:
        return True
    first, last = needed[0], needed[-1]
    if table.first_day is None or table.last_day is None:
        return False
    # A rate strictly before the first needed day must already be in the archive.
    if table.first_day >= first:
        return False
    if table.last_day >= last:
        return True
    # The archive was downloaded on or after the last needed day, so any
    # publication before that day was already available and is simply absent
    # (a weekend or a holiday).
    return fetched_on is not None and fetched_on >= last


# ── loading ─────────────────────────────────────────────────────────


async def load_fx(
    home: Path,
    needed: Iterable[date],
    *,
    now: datetime | None = None,
    http: httpx.AsyncClient | None = None,
) -> FxTable:
    """Return ECB rates covering ``needed``, downloading the archive at most once."""
    days = sorted({day for day in needed})
    if not days:
        return FxTable({})

    cached: tuple[FxTable, date | None] | None = None
    try:
        cached = read_cache(Path(home))
    except FxError:
        cached = None  # a corrupt cache is repaired by the download below
    if cached is not None and _covers(cached[0], cached[1], days):
        return cached[0]

    today = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()
    data = await _download(http)
    table = parse_archive(data)
    save_cache(Path(home), data, today)
    return table


async def _download(http: httpx.AsyncClient | None) -> bytes:
    own_client = http is None
    client = http or httpx.AsyncClient(timeout=60.0, follow_redirects=True)
    try:
        try:
            response = await client.get(FX_URL)
        except httpx.HTTPError as e:
            raise FxError(f"Cannot download ECB rates: {type(e).__name__}") from None
    finally:
        if own_client:
            await client.aclose()
    if response.status_code != 200:
        raise FxError(f"Cannot download ECB rates: {FX_URL} returned HTTP {response.status_code}")
    if not response.content:
        raise FxError("ECB rates download returned an empty body")
    return response.content
