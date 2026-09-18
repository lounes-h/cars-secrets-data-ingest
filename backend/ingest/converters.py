"""Converters from raw CSV strings to typed values. Each raises ConversionError on bad input."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_header(name: str) -> str:
    """'Sale Date M/D/CY' -> 'sale_date_m_d_cy', 'Item#' -> 'item'."""
    return _NON_ALNUM.sub("_", name.strip().lower()).strip("_")


def normalize_headers(names: list[str]) -> list[str]:
    """Normalize headers, suffixing collisions so every key stays unique."""
    seen: dict[str, int] = {}
    out = []
    for name in names:
        key = normalize_header(name) or "column"
        if key in seen:
            seen[key] += 1
            key = f"{key}_{seen[key]}"
        else:
            seen[key] = 1
        out.append(key)
    return out


_DECIMAL = re.compile(r"-?\d+\.\d+")


def canonical_value(value: str) -> str:
    """Strip spaces and trailing fractional zeros ('8119.0' -> '8119', '1.50' -> '1.5') so the
    same number formatted differently by two exports compares equal. Integers are untouched,
    which keeps leading zeros (ZIP codes)."""
    value = value.strip()
    if _DECIMAL.fullmatch(value):
        value = value.rstrip("0").rstrip(".")
        if value in ("-0", ""):
            value = "0"
    return value


class ConversionError(ValueError):
    pass


def to_text(value: str) -> str | None:
    value = value.strip()
    return value or None


def to_https_url(value: str) -> str | None:
    """'cs.copart.com/a.jpg' or 'http://…' -> 'https://…' (images are embedded over HTTPS)."""
    value = value.strip()
    if not value:
        return None
    if value.startswith("//"):
        value = value[2:]
    elif value.lower().startswith("http://"):
        value = value[7:]
    elif value.lower().startswith("https://"):
        value = value[8:]
    if not value or " " in value or "." not in value.split("/", 1)[0]:
        raise ConversionError(f"not a URL: {value!r}")
    return "https://" + value


def to_int(value: str) -> int | None:
    value = value.strip()
    if not value:
        return None
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ConversionError(f"not an integer: {value!r}") from exc
    if not number.is_finite() or number != number.to_integral_value():
        raise ConversionError(f"not an integer: {value!r}")
    return int(number)


def to_decimal(value: str) -> Decimal | None:
    value = value.strip()
    if not value:
        return None
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ConversionError(f"not a number: {value!r}") from exc
    if not result.is_finite():
        raise ConversionError(f"not a finite number: {value!r}")
    return result


def to_yyyymmdd(value: str) -> date | None:
    value = value.strip()
    if not value or value == "0":
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError as exc:
        raise ConversionError(f"not a YYYYMMDD date: {value!r}") from exc


def to_db2_timestamp(value: str) -> datetime | None:
    """'2026-09-14-08.10.17.000363' (no time zone in the source)."""
    value = value.strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d-%H.%M.%S.%f")
    except ValueError as exc:
        raise ConversionError(f"not a DB2 timestamp: {value!r}") from exc


def to_iso_utc(value: str) -> datetime | None:
    """'2026-09-14T13:55:06Z'; a naive value is taken as UTC."""
    value = value.strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConversionError(f"not an ISO timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


# UTC offsets (hours) of North American time zone abbreviations used by auction exports.
TZ_OFFSETS: dict[str, float] = {
    "NDT": -2.5, "NST": -3.5, "ADT": -3, "AST": -4, "EDT": -4, "EST": -5,
    "CDT": -5, "CST": -6, "MDT": -6, "MST": -7, "PDT": -7, "PST": -8,
    "AKDT": -8, "AKST": -9, "HDT": -9, "HST": -10, "UTC": 0, "GMT": 0,
}


def local_timestamp(day: date | None, hhmm: str | None, tz: str | None) -> datetime | None:
    """Combine a date, an 'HHMM' time and a zone abbreviation into an aware datetime.

    Returns None when any part is missing or unrecognised rather than guessing a zone.
    """
    if day is None or not hhmm or not tz:
        return None
    offset = TZ_OFFSETS.get(tz.strip().upper())
    hhmm = hhmm.strip()
    if offset is None or not hhmm.isdigit() or len(hhmm) > 4:
        return None
    hhmm = hhmm.zfill(4)
    hour, minute = int(hhmm[:2]), int(hhmm[2:])
    if hour > 23 or minute > 59:
        return None
    return datetime(
        day.year, day.month, day.day, hour, minute, tzinfo=timezone(timedelta(hours=offset))
    )
