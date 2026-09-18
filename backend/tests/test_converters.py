from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ingest.converters import (
    ConversionError,
    canonical_value,
    local_timestamp,
    normalize_header,
    normalize_headers,
    to_db2_timestamp,
    to_decimal,
    to_int,
    to_iso_utc,
    to_text,
    to_yyyymmdd,
)


@pytest.mark.parametrize("raw, expected", [
    ("Sale Date M/D/CY", "sale_date_m_d_cy"),
    ("Item#", "item"),
    ("High Bid =non-vix,Sealed=Vix", "high_bid_non_vix_sealed_vix"),
    ("Has Keys-Yes or No", "has_keys_yes_or_no"),
    ("  Lot number ", "lot_number"),
    ("Est. Retail Value", "est_retail_value"),
])
def test_normalize_header(raw, expected):
    assert normalize_header(raw) == expected


def test_normalize_headers_keeps_keys_unique():
    assert normalize_headers(["Lot number", "Lot-Number", "#", ""]) == [
        "lot_number", "lot_number_2", "column", "column_2",
    ]


def test_to_text_blank_is_none():
    assert to_text("  ") is None
    assert to_text(" E020 ") == "E020"


@pytest.mark.parametrize("raw, expected", [
    ("2013", 2013), ("2013.0", 2013), ("", None), (" 7 ", 7),
])
def test_to_int(raw, expected):
    assert to_int(raw) == expected


@pytest.mark.parametrize("raw", ["abc", "1.5", "NaN", "inf"])
def test_to_int_rejects(raw):
    with pytest.raises(ConversionError):
        to_int(raw)


def test_to_decimal():
    assert to_decimal("107409.03") == Decimal("107409.03")
    assert to_decimal("") is None
    for bad in ("12a", "NaN", "Infinity"):
        with pytest.raises(ConversionError):
            to_decimal(bad)


def test_to_yyyymmdd():
    assert to_yyyymmdd("20260918") == date(2026, 9, 18)
    assert to_yyyymmdd("0") is None  # Copart: sale date not assigned yet
    assert to_yyyymmdd("") is None
    with pytest.raises(ConversionError):
        to_yyyymmdd("20261399")


def test_to_db2_timestamp():
    assert to_db2_timestamp("2026-09-14-08.10.17.000363") == datetime(2026, 9, 14, 8, 10, 17, 363)
    with pytest.raises(ConversionError):
        to_db2_timestamp("2026-09-14 08:10")


def test_to_iso_utc():
    assert to_iso_utc("2026-09-14T13:55:06Z") == datetime(2026, 9, 14, 13, 55, 6, tzinfo=UTC)
    assert to_iso_utc("2026-09-14T13:55:06").tzinfo == UTC
    with pytest.raises(ConversionError):
        to_iso_utc("N")  # value seen in shifted rows of the real export


def test_local_timestamp_applies_zone_offset():
    ts = local_timestamp(date(2026, 9, 18), "1200", "PDT")
    assert ts == datetime(2026, 9, 18, 12, 0, tzinfo=timezone(timedelta(hours=-7)))
    assert local_timestamp(date(2026, 9, 18), "930", "EDT").hour == 9


@pytest.mark.parametrize("day, hhmm, tz", [
    (None, "1200", "PDT"), (date(2026, 1, 1), "", "PDT"), (date(2026, 1, 1), "1200", "XYZ"),
    (date(2026, 1, 1), "2460", "PDT"), (date(2026, 1, 1), "12:00", "PDT"),
])
def test_local_timestamp_unknown_parts_give_none(day, hhmm, tz):
    assert local_timestamp(day, hhmm, tz) is None


@pytest.mark.parametrize("raw, expected", [
    ("8119.0", "8119"), ("107409.030", "107409.03"), ("0.0", "0"), ("-0.00", "0"),
    ("94590 7203", "94590 7203"), ("07203", "07203"), ("2013", "2013"), (" TECH ", "TECH"),
    ("1.5.0", "1.5.0"),
])
def test_canonical_value(raw, expected):
    assert canonical_value(raw) == expected
