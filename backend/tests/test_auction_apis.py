"""The auction data API clients and the mapping of their lots onto one record shape."""

import io
import urllib.error
import urllib.request
from datetime import UTC, datetime

import pytest

from ingest import auction_apis
from ingest.auction_apis import Record, normalize_apiauctions, normalize_apibara

# One lot as apibara returns it (trimmed from a real page).
APIBARA_LOT = {
    "platform": "copart", "lot_number": "75601085", "vin": "5UXKR2C38H0X06871", "year": 2017,
    "make": "BMW", "model": "X5", "odometer": {"mi": 108696, "km": 174929},
    "auction": {"state": "finished", "auction_at": "2026-09-22T23:58:58+00:00",
                "last_sold_status": "Sold"},
    "pricing": {"current_bid_usd": 0, "buy_now_usd": 0, "sale_price_usd": 4400},
    "condition": {"run_condition": {"value": "DEFAULT", "label": "Default"}, "has_key": True,
                  "primary_damage": "Side"},
    "sale_document": {"name": "SALVAGE CERTIFICATE OF TITLE"},
}


def test_apibara_lot_is_normalized():
    assert normalize_apibara(APIBARA_LOT) == Record(
        api="apibara", source="copart", lot_number="75601085", vin="5UXKR2C38H0X06871",
        year=2017, make="BMW", model="X5", odometer=108696, damage="Side", has_keys=True,
        runs="Default", title="SALVAGE CERTIFICATE OF TITLE", retail=None, status="Sold",
        sale_at=datetime(2026, 9, 22, 23, 58, 58, tzinfo=UTC), final_price=4400,
        current_bid=None, buy_now=None)


def test_apibara_lot_without_details_has_no_values():
    record = normalize_apibara({"platform": "iaai", "lot_number": 45416833})
    assert record.lot_number == "45416833"
    assert (record.odometer, record.damage, record.status, record.sale_at,
            record.final_price) == (None, None, None, None, None)
    assert not record.sold  # not known to have sold


def test_a_buy_now_sale_without_a_sale_price_sold_at_the_buy_now_price():
    lot = {**APIBARA_LOT, "auction": {"last_sold_status": "Sold", "sold_buy_now": True},
           "pricing": {"current_bid_usd": None, "buy_now_usd": 2875, "sale_price_usd": 0}}
    assert normalize_apibara(lot).final_price == 2875
    lot["auction"]["sold_buy_now"] = False
    assert normalize_apibara(lot).final_price is None


def test_apiauctions_lot_takes_the_latest_sale_from_its_history():
    record = normalize_apiauctions({
        "vin": "5UXKR0C52G0P21189", "make": "BMW", "model": "X5", "year": 2016,
        "estimate_retail": 21500, "current_bid": 8400, "buy_now_price": None,
        "history": [
            {"auction": "IAAI", "lot_number": 2, "sale_date": "2024-08-12T15:00:00Z",
             "sale_status": "Sold", "final_bid": 4200, "odometer": 86134,
             "primary_damage": "Front End", "title_type": "Salvage"},
            {"auction": "IAAI", "lot_number": 1, "sale_date": "2024-03-01T15:00:00Z",
             "sale_status": "Not Sold", "final_bid": 0},
        ],
    })
    assert (record.source, record.lot_number, record.final_price, record.status) == (
        "iaai", "2", 4200, "Sold")
    assert record.sale_at == datetime(2024, 8, 12, 15, tzinfo=UTC)
    assert (record.odometer, record.damage, record.title, record.retail) == (
        86134, "Front End", "Salvage", 21500)
    assert (record.current_bid, record.buy_now) == (8400, None)


def test_apiauctions_lot_fields_win_over_history_and_a_bare_date_is_no_sale_time():
    record = normalize_apiauctions({"lot_number": 41234567, "auction": "COPART",
                                    "auction_date": "2024-09-05", "sale_price": 7900})
    assert (record.source, record.lot_number, record.final_price) == ("copart", "41234567", 7900)
    assert record.sale_at is None


@pytest.mark.parametrize(("status", "sold"), [(None, False), ("Sold", True),
                                              ("Sold on Approval", True), ("Not Sold", False),
                                              ("On Approval", False)])
def test_only_a_sold_status_is_sold(status, sold):
    assert normalize_apibara({**APIBARA_LOT, "auction": {"last_sold_status": status}}).sold is sold


def test_money_is_positive_or_none():
    assert [auction_apis.money(v) for v in (4400, "25.5", 0, -1, None, "n/a")] == [
        4400, 25.5, None, None, None, None]


@pytest.fixture
def requests(monkeypatch):
    """Captures each request and answers `{"data": []}`."""
    seen = []

    def urlopen(request, timeout):
        seen.append(request)
        return io.BytesIO(b'{"data": []}')

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return seen


def test_apibara_page_sends_the_key_and_filters(requests):
    assert auction_apis.apibara_page("secret", {"platform": "iaai", "per_page": 20}) == {
        "data": []}
    (request,) = requests
    assert request.full_url == ("https://apibara.tech/api/v1/vehicle-auction/vehicles"
                                "?platform=iaai&per_page=20")
    assert request.get_method() == "GET"
    assert request.get_header("X-api-key") == "secret"
    assert request.get_header("Accept") == "application/json"


def test_apiauctions_page_posts_with_a_bearer_token(requests):
    auction_apis.apiauctions_page("tok", "get-active-lots",
                                  {"auction_names[]": ["COPART", "IAAI"], "page": 1})
    (request,) = requests
    assert request.full_url == ("https://apiauctions.io/api/v2/get-active-lots"
                                "?auction_names%5B%5D=COPART&auction_names%5B%5D=IAAI&page=1")
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer tok"


def test_http_errors_are_raised(monkeypatch):
    def urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 402, "Payment Required", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(urllib.error.HTTPError):
        auction_apis.apibara_page("secret", {})

