"""Third-party APIs that index Copart and IAA lots with their final sale prices: apibara.tech and
apiauctions.io. The clients return raw pages; `normalize_*` maps one lot onto `Record`, the shape
`api_check` compares with our lots and with the other API."""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
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
    next_sale_at: datetime | None = None  # a later auction it is scheduled for, if known apart

    @property
    def sold(self) -> bool:
        """The API says the lot sold. "Sold on Approval" is a bid awaiting the seller's approval,
        not a sale: sellers often decline it, and the lot comes back for another auction."""
        return self.status is not None and self.status.lower() == "sold"


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


def epoch(value: Any, per_second: int = 1) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value) / per_second, UTC)
    except (TypeError, ValueError):
        return None


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


def normalize_apiauctions(car: Mapping[str, Any]) -> Record:
    """A lot keeps only its latest sale in `sales_history` ("Sold" / "Not sold", the price, epoch
    seconds); a later auction it is scheduled for is in `active_bidding` (epoch milliseconds).
    For an unsold lot `purchase_price` is the bid that was not accepted: no final price."""
    sales = sorted(car.get("sales_history") or [], key=lambda s: s.get("sale_date") or 0)
    last = sales[-1] if sales else {}
    bidding = (car.get("active_bidding") or [{}])[-1]
    auction = str(car.get("auction_name") or "").lower()
    return Record(
        api="apiauctions", source="iaai" if auction.startswith("iaai") else auction,
        lot_number=str(car["lot_number"]), vin=car.get("vin"), year=car.get("year"),
        make=car.get("make"), model=car.get("model"), odometer=car.get("odometer"),
        damage=car.get("primary_damage"), has_keys=car.get("car_keys"),
        runs=car.get("highlights"), title=car.get("doc_type"),
        retail=money(car.get("est_retail_value")), status=last.get("sale_status"),
        sale_at=epoch(last.get("sale_date")),
        final_price=money(last.get("purchase_price")) if last.get("sold") else None,
        current_bid=money(bidding.get("current_bid")),
        buy_now=money((car.get("buy_now_car") or {}).get("purchase_price")),
        next_sale_at=epoch(bidding.get("sale_date"), 1000))
