"""Copart "sales data" CSV export (www.copart.com/downloadSalesData)."""

from __future__ import annotations

import json
import re
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import structlog

from ..converters import (
    ConversionError,
    local_timestamp,
    to_db2_timestamp,
    to_decimal,
    to_https_url,
    to_int,
    to_iso_utc,
    to_text,
    to_yyyymmdd,
)
from .base import FieldMapping, LotImage, LotImages, Provider, RawGetter

log = structlog.get_logger()


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


# Copart writes each text field as '"' + value + '"' and never escapes a quote inside the
# value (inches: `29'9"`, quoted notes), which standard CSV parsing mangles or rejects. Such a
# quote sits inside a field: not right after a comma / line start, nor right before a comma /
# line end.
_INNER_QUOTE = re.compile(r'(?<=[^,\n])"(?=[^,\r\n])')


def split_unescaped_quotes(record: str) -> list[str] | None:
    """Split a record the way Copart wrote it: a quoted field ends at the first quote that is
    followed by a comma or the end of the line. Only for records holding an inner quote
    (None otherwise, and when the record does not follow that shape either)."""
    if not _INNER_QUOTE.search(record):
        return None
    line = record.rstrip("\r\n")
    end = len(line)
    fields: list[str] = []
    i = 0
    while True:
        if i < end and line[i] == '"':
            j = i + 1
            while True:
                k = line.find('"', j)
                if k == -1:
                    return None
                if k + 1 == end or line[k + 1] == ",":
                    break
                j = k + 1
            fields.append(line[i + 1:k])
            i = k + 1
        else:
            k = line.find(",", i)
            k = end if k == -1 else k
            value = line[i:k]
            if '"' in value:  # unquoted fields never hold quotes in Copart's output
                return None
            fields.append(value)
            i = k
        if i == end:
            return fields
        i += 1  # the comma


# The lot's `image_url` points at Copart's image API, which lists every photo of the lot.
# Only these hosts are ever contacted or embedded, whatever the export contains.
IMAGE_API_HOST = "inventoryv2.copart.io"
IMAGE_HOST = "cs.copart.com"
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def _get_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/json",
                                                   "User-Agent": "cars-ingest/1.0"})
    with urllib.request.urlopen(request, timeout=8) as response:  # noqa: S310 - https, fixed host
        return json.loads(response.read(_MAX_RESPONSE_BYTES))


def _image_url(raw: Any) -> str | None:
    """HTTPS URL on Copart's image host, or None."""
    if not isinstance(raw, str):
        return None
    try:
        url = to_https_url(raw)
    except ConversionError:
        return None
    return url if url and urlsplit(url).hostname == IMAGE_HOST else None


def images(lot: Mapping[str, Any], get_json: Callable[[str], Any] = _get_json) -> LotImages:
    """All photos from Copart's public image API. For some lots (seen: about 1 in 6-12, those
    on Copart's newer image store) the API answers 404 although copart.com shows photos; then
    only the export's thumbnail is available (full size = `_thb` -> `_ful`), marked incomplete
    so the UI can link to the lot page. Copart's member-only endpoints are never called."""
    found: list[LotImage] = []
    api = urlsplit((lot.get("image_url") or "").strip())
    if api.hostname == IMAGE_API_HOST and api.path.startswith("/v1/lotImages/"):
        try:
            data = get_json(urlunsplit(("https", api.netloc, api.path, api.query, "")))
            entries = sorted(data.get("lotImages") or [], key=lambda e: e.get("sequence") or 0)
            for entry in entries:
                sizes: dict[str, str | None] = {"thumb": None, "full": None, "hd": None}
                for link in entry.get("link") or []:
                    url = _image_url(link.get("url"))
                    if url is None or link.get("isEngineSound"):
                        continue
                    kind = ("thumb" if link.get("isThumbNail")
                            else "hd" if link.get("isHdImage") else "full")
                    sizes[kind] = sizes[kind] or url
                if sizes["thumb"] or sizes["full"] or sizes["hd"]:
                    found.append(LotImage(**sizes))
        except Exception:  # noqa: BLE001 - photos are best effort, never an API error
            log.warning("images.fetch_failed", lot=lot.get("lot_number"), exc_info=True)
    if found:
        return LotImages(found, complete=True)
    thumb = _image_url(lot.get("thumbnail_url"))
    if thumb:
        full = thumb.replace("_thb.", "_ful.") if "_thb." in thumb else None
        found.append(LotImage(thumb=thumb, full=full))
    return LotImages(found, complete=False)


def listing_url(lot_number: str) -> str:
    return f"https://www.copart.com/lot/{quote(lot_number, safe='')}"


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
        # Filled only when the lot is held away from the yard (seen: ~1 % of lots).
        _f("offsite_address", to_text, "offsite_address1"),
        _f("offsite_city", to_text),
        _f("offsite_state", to_text),
        _f("offsite_zip", to_text),
        _f("seller_name", to_text),
        _f("image_url", to_text),
        _f("thumbnail_url", to_https_url, "image_thumbnail"),
        _f("source_created_at", to_db2_timestamp, "create_date_time"),
        _f("source_updated_at", to_iso_utc, "last_updated_time"),
    ),
    derive=_derive,
    split_record=split_unescaped_quotes,
    derived_sources={alias: "sale_at" for alias in _SALE_TIME + _TIME_ZONE},
    # Downloads can be a subset of yards (seen: 44 of 224), so absence only means removal
    # for lots of a yard the export contains.
    removal_scope="yard_code",
    removal_scope_label="yard",
    fetch_images=images,
    listing_url=listing_url,
)
