"""Third-party APIs that index Copart and IAA lots with their final sale prices: apibara.tech and
apiauctions.io. The clients return raw pages; `normalize_*` maps one lot onto `Record`, the shape
`api_check` compares with our lots and with the other API."""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

APIBARA_URL = "https://apibara.tech/api/v1/vehicle-auction/vehicles"
APIAUCTIONS_URL = "https://apiauctions.io/api/v2/"
TIMEOUT = 30


@dataclass(frozen=True)
class Record:
    """One lot as an API reports it. Prices are USD; None when the API gives none (or 0)."""

    api: str
    source: str  # copart | iaai
    lot_number: str
    vin: str | None
    year: int | None
    make: str | None
    model: str | None
    odometer: float | None
    damage: str | None
    has_keys: Any
    runs: str | None
    title: str | None
    retail: float | None
    status: str | None
    sale_at: datetime | None  # when it sold (or is scheduled to)
    final_price: float | None
    current_bid: float | None
    buy_now: float | None

    @property
    def sold(self) -> bool:
        """The API says the lot sold ("Sold", "Sold on Approval")."""
        return self.status is not None and self.status.lower().startswith("sold")


def _get_json(request: urllib.request.Request) -> Any:
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", "cars-ingest/1.0")
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310 - https, fixed host
        return json.load(response)


def apibara_page(key: str, params: Mapping[str, Any]) -> Any:
    url = f"{APIBARA_URL}?{urlencode(params)}"
    return _get_json(urllib.request.Request(url, headers={"X-API-Key": key}))


def apiauctions_page(token: str, endpoint: str, params: Mapping[str, Any]) -> Any:
    # Parameters go in the query string, as in the provider's examples; the body stays empty.
    url = f"{APIAUCTIONS_URL}{endpoint}?{urlencode(params, doseq=True)}"
    return _get_json(urllib.request.Request(url, data=b"", method="POST",
                                            headers={"Authorization": f"Bearer {token}"}))


def money(value: Any) -> float | None:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    return amount if amount > 0 else None


def _time(value: Any) -> datetime | None:
    """An aware timestamp, or None: a bare date cannot be compared with sale times."""
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else None


def normalize_apibara(v: Mapping[str, Any]) -> Record:
    auction, pricing = v.get("auction") or {}, v.get("pricing") or {}
    condition = v.get("condition") or {}
    return Record(
        api="apibara", source=v["platform"], lot_number=str(v["lot_number"]), vin=v.get("vin"),
        year=v.get("year"), make=v.get("make"), model=v.get("model"),
        odometer=(v.get("odometer") or {}).get("mi"), damage=condition.get("primary_damage"),
        has_keys=condition.get("has_key"), runs=(condition.get("run_condition") or {}).get("label"),
        title=(v.get("sale_document") or {}).get("name"), retail=None,
        status=auction.get("last_sold_status"), sale_at=_time(auction.get("auction_at")),
        # A Buy Now sale (seen on IAA) may carry its price only as the Buy Now price.
        final_price=money(pricing.get("sale_price_usd")) or (
            money(pricing.get("buy_now_usd")) if auction.get("sold_buy_now") else None),
        current_bid=money(pricing.get("current_bid_usd")),
        buy_now=money(pricing.get("buy_now_usd")))


# ponytail: shaped after the docs' examples only (no token yet); check on the first real page.
def normalize_apiauctions(car: Mapping[str, Any]) -> Record:
    history = sorted(car.get("history") or [], key=lambda h: str(h.get("sale_date") or ""))
    last = history[-1] if history else {}

    def pick(key: str, history_key: str | None = None) -> Any:
        value = car.get(key)
        return value if value is not None else last.get(history_key or key)

    auction = str(pick("auction") or "").lower()
    return Record(
        api="apiauctions", source="iaai" if auction.startswith("iaa") else auction,
        lot_number=str(pick("lot_number")), vin=car.get("vin"), year=car.get("year"),
        make=car.get("make"), model=car.get("model"), odometer=pick("odometer"),
        damage=pick("primary_damage"), has_keys=car.get("has_keys"), runs=car.get("highlights"),
        title=pick("title_type"), retail=money(car.get("estimate_retail")),
        status=pick("sale_status"), sale_at=_time(pick("auction_date", "sale_date")),
        final_price=money(pick("sale_price", "final_bid")),
        current_bid=money(car.get("current_bid")), buy_now=money(car.get("buy_now_price")))
