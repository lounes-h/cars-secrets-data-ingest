"""Copart "sales data" CSV export (www.copart.com/downloadSalesData)."""

from __future__ import annotations

from typing import Any

from ..converters import (
    ConversionError,
    local_timestamp,
    to_db2_timestamp,
    to_decimal,
    to_int,
    to_iso_utc,
    to_text,
    to_yyyymmdd,
)
from .base import FieldMapping, Provider, RawGetter


def _lot_number(raw: str) -> str | None:
    try:
        number = to_int(raw)
    except ConversionError:
        return None
    return str(number) if number is not None and number > 0 else None


def _f(column: str, convert: Any, *aliases: str) -> FieldMapping:
    return FieldMapping(column, aliases or (column,), convert)


_SALE_TIME = ("sale_time_hhmm", "sale_time")
_TIME_ZONE = ("time_zone",)


def _derive(raw: RawGetter, values: dict[str, Any]) -> None:
    values["sale_at"] = local_timestamp(values.get("sale_date"), raw(_SALE_TIME), raw(_TIME_ZONE))


COPART = Provider(
    name="copart",
    display_name="Copart",
    key_aliases=("lot_number",),
    key_convert=_lot_number,
    ignored_columns=frozenset({"id"}),  # row counter of the export, changes every file
    # Stamped with the export batch time on every row, so it differs in each download.
    volatile_columns=frozenset({"create_date_time"}),
    fields=(
        _f("yard_code", to_text, "yard_number"),
        _f("yard_name", to_text),
        _f("sale_date", to_yyyymmdd, "sale_date_m_d_cy", "sale_date"),
        _f("vehicle_type", to_text),
        _f("year", to_int),
        _f("make", to_text),
        _f("model_group", to_text),
        _f("model_detail", to_text),
        _f("trim", to_text),
        _f("body_style", to_text),
        _f("color", to_text),
        _f("damage_primary", to_text, "damage_description"),
        _f("damage_secondary", to_text, "secondary_damage"),
        _f("title_state", to_text, "sale_title_state"),
        _f("title_type", to_text, "sale_title_type"),
        _f("has_keys", to_text, "has_keys_yes_or_no", "has_keys"),
        _f("vin", to_text),
        _f("odometer", to_decimal),
        _f("odometer_brand", to_text),
        _f("est_retail_value", to_decimal),
        _f("repair_cost", to_decimal),
        _f("engine", to_text),
        _f("drive", to_text),
        _f("transmission", to_text),
        _f("fuel_type", to_text),
        _f("cylinders", to_text),
        _f("runs_drives", to_text),
        _f("sale_status", to_text),
        _f("high_bid", to_decimal, "high_bid_non_vix_sealed_vix", "high_bid"),
        _f("buy_it_now_price", to_decimal),
        _f("currency", to_text, "currency_code"),
        _f("location_city", to_text),
        _f("location_state", to_text),
        _f("location_zip", to_text),
        _f("location_country", to_text),
        _f("seller_name", to_text),
        _f("image_url", to_text),
        _f("source_created_at", to_db2_timestamp, "create_date_time"),
        _f("source_updated_at", to_iso_utc, "last_updated_time"),
    ),
    derive=_derive,
    derived_sources={alias: "sale_at" for alias in _SALE_TIME + _TIME_ZONE},
    # Downloads can be a subset of yards (seen: 44 of 224), so absence only means removal
    # for lots of a yard the export contains.
    removal_scope="yard_code",
)
