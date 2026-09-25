"""Checking the auction data APIs against our lots, each other and a manual spot check."""

import csv
import json
import urllib.error
from datetime import UTC, date, datetime, timedelta

import pytest

from ingest import api_check, auction_apis, db
from ingest.api_check import Api, check, cross, manual_matches, shares
from ingest.auction_apis import normalize_apibara

AUCTION = datetime(2026, 9, 23, 14, tzinfo=UTC)
RUN = {"sold_date": "2026-09-23", "active_date": "2026-09-30", "make": "VOLVO"}


def apibara(lot, platform="copart", status="Sold", price=5000, at="2026-09-23T14:05:00+00:00",
            buy_now=0, bid=0, **fields):
    """A lot as apibara returns it."""
    return {"platform": platform, "lot_number": lot, "vin": f"VIN{lot}", "year": 2018,
            "make": "VOLVO", "model": "XC90", "odometer": {"mi": 50000},
            "condition": {"primary_damage": "Front end", "has_key": True},
            "auction": {"auction_at": at, "last_sold_status": status},
            "pricing": {"sale_price_usd": price, "current_bid_usd": bid, "buy_now_usd": buy_now},
            **fields}


def ours(**values):
    """Our lot as report() reads it."""
    return {"lot_number": "1", "vin": "VIN1", "year": 2018, "make": "VOLVO", "odometer": 50000,
            "damage_primary": "FRONT END", "high_bid": 3000, "buy_it_now_price": 0,
            "sale_at": AUCTION, "last_seen_at": AUCTION - timedelta(minutes=30), **values}


def test_a_matching_sold_lot_passes_every_check():
    assert check(normalize_apibara(apibara("1")), ours(), "sold") == {
        "vin": True, "year": True, "make": True, "odometer": True, "damage": True,
        "price_at_least_prebid": True, "sale_time": True}


def test_a_wrong_sold_lot_fails_its_checks():
    record = normalize_apibara(apibara("1", vin="OTHER", price=2000, year=2019,
                                       odometer={"mi": 50002},
                                       at="2026-09-23T12:00:00+00:00"))
    assert check(record, ours(), "sold") == {
        "vin": False, "year": False, "make": True, "odometer": False, "damage": True,
        "price_at_least_prebid": False, "sale_time": False,  # before we last saw it listed
        "presale_at_buy_now": None}  # and the lot had no Buy It Now price
    during = normalize_apibara(apibara("1", at="2026-09-23T17:40:00+00:00"))
    assert check(during, ours(), "sold")["sale_time"] is True  # auctions run for hours
    late = normalize_apibara(apibara("1", at="2026-09-23T18:30:00+00:00"))
    assert check(late, ours(), "sold")["sale_time"] is False


def test_checks_that_do_not_apply_are_none():
    record = normalize_apibara({"platform": "copart", "lot_number": "1"})
    assert check(record, ours(odometer=None, high_bid=0), "sold") == dict.fromkeys(
        ("vin", "year", "make", "odometer", "damage", "price_at_least_prebid"))


def test_a_lot_sold_before_its_auction_sold_at_our_buy_it_now_price():
    record = normalize_apibara(apibara("1", price=7000, at="2026-09-23T11:00:00+00:00"))
    seen = ours(sale_at=AUCTION + timedelta(days=6), buy_it_now_price=7000,
                last_seen_at=AUCTION - timedelta(hours=4))
    assert (check(record, seen, "sold")["presale_at_buy_now"],
            check(record, seen, "sold")["sale_time"]) == (True, True)
    assert check(record, {**seen, "buy_it_now_price": 7500}, "sold")["presale_at_buy_now"] is False


def test_an_active_lot_is_checked_on_its_sale_time_and_buy_it_now_price():
    record = normalize_apibara(apibara("1", status=None, price=0, buy_now=9000,
                                       at="2026-09-23T14:30:00+00:00"))
    result = check(record, ours(buy_it_now_price=9000), "active")
    assert (result["sale_time"], result["buy_now"]) == (True, True)
    late = normalize_apibara(apibara("1", status=None, at="2026-09-23T16:00:00+00:00"))
    assert check(late, ours(), "active")["sale_time"] is False


def test_shares_count_only_the_checks_that_applied():
    assert shares([{"a": True, "b": None}, {"a": False, "b": True}, {"a": True}]) == {
        "a": 0.6667, "b": 1.0}
    assert shares([]) == {}


def test_cross_compares_the_lots_both_apis_list():
    a = [normalize_apibara(apibara("1")), normalize_apibara(apibara("2"))]
    b = [normalize_apibara(apibara("1")), normalize_apibara(apibara("2", price=5100)),
         normalize_apibara(apibara("3"))]
    assert cross(a, b) == {"overlap": 0.6667, "same_price": 0.5, "same_vin": 1.0}
    assert cross([], []) == {"overlap": None}


def test_manual_prices_are_compared_where_filled_in():
    records = {("copart", "1"): normalize_apibara(apibara("1", price=4400))}
    rows = [{"source": "copart", "lot_number": "1", "final_price": "$4,400"},
            {"source": "copart", "lot_number": "2", "final_price": "900"},  # not listed
            {"source": "iaai", "lot_number": "1", "final_price": " "}]
    assert manual_matches(rows, records) == {"checked": 1, "same_price": 1.0}
    assert manual_matches([], records) == {"checked": 0}


@pytest.fixture
def run_dir(tmp_path):
    folder = tmp_path / "run"
    folder.mkdir()
    (folder / "run.json").write_text(json.dumps(RUN))
    return folder


def save(run_dir, api, slice_name, lots, page=1, meta=None):
    path = run_dir / api / slice_name / f"{page}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"data": lots, "meta": meta or {}}))


def fake_api(monkeypatch, pages_per_slice=2):
    """apibara answering `pages_per_slice` empty pages per slice; returns the requests made."""
    calls = []

    def request(key, s, cursor):
        calls.append((key, s.name, cursor))
        page = 1 if cursor is None else cursor + 1
        return {"data": [], "meta": {"next_cursor": page if page < pages_per_slice else None}}

    spec = api_check.APIS["apibara"]
    monkeypatch.setitem(api_check.APIS, "apibara",
                        Api(spec.key_setting, request, spec.next_cursor, spec.normalize))
    return calls


def test_fetch_stays_within_budget_and_never_requests_a_saved_page(run_dir, monkeypatch):
    calls = fake_api(monkeypatch)
    assert api_check.fetch("apibara", "k", run_dir, 3) == 3
    assert calls == [("k", "copart-sold", None), ("k", "copart-sold", 1), ("k", "iaai-sold", None)]
    assert api_check.fetch("apibara", "k", run_dir, 10) == 5
    assert calls[3] == ("k", "iaai-sold", 1)
    assert api_check.fetch("apibara", "k", run_dir, 10) == 0
    assert sorted(p.relative_to(run_dir).as_posix() for p in run_dir.glob("apibara/*/*")) == [
        f"apibara/{name}/{page}.json" for name in ("copart-active", "copart-sold",
                                                   "iaai-active", "iaai-sold")
        for page in (1, 2)]


def test_fetch_stops_a_slice_after_max_pages(run_dir, monkeypatch):
    fake_api(monkeypatch, pages_per_slice=100)
    assert api_check.fetch("apibara", "k", run_dir, 100) == 4 * api_check.MAX_PAGES


def test_slice_requests(monkeypatch):
    seen = []
    monkeypatch.setattr(auction_apis, "apibara_page", lambda key, params: seen.append(params))
    monkeypatch.setattr(auction_apis, "apiauctions_page",
                        lambda key, endpoint, params: seen.append((endpoint, params)))
    sold, active = api_check.slices(RUN)[0], api_check.slices(RUN)[3]
    api_check._apibara("k", sold, None)
    api_check._apibara("k", active, "next")
    api_check._apiauctions("k", sold, None)
    api_check._apiauctions("k", active, 2)
    assert seen[0] == {"platform": "copart", "lot_sub_status": "Ended", "make": "VOLVO",
                       "auction_date_from": date(2026, 9, 23),
                       "auction_date_to": date(2026, 9, 23), "per_page": 40}
    assert (seen[1]["platform"], seen[1]["lot_sub_status"], seen[1]["cursor"]) == (
        "iaai", "Open", "next")
    assert (seen[2][0], seen[2][1]["auction_name"], seen[2][1]["page"]) == (
        "get-cars", "COPART", 1)
    assert (seen[3][0], seen[3][1]["auction_name"], seen[3][1]["page"]) == (
        "get-active-lots", "IAAI", 2)


def test_apiauctions_pages_until_the_last_one():
    assert api_check._apiauctions_next({"meta": {"last_page": 2}}, None) == 2
    assert api_check._apiauctions_next({"meta": {"last_page": 2}}, 2) is None
    assert api_check._apiauctions_next({}, None) is None


def test_load_counts_a_lot_on_two_pages_once(run_dir):
    save(run_dir, "apibara", "copart-sold", [apibara("1"), apibara("2")])
    save(run_dir, "apibara", "copart-sold", [apibara("2", price=5100)], page=2)
    lots = api_check.load(run_dir, "apibara")
    assert [(r.lot_number, r.final_price) for r in lots["copart-sold"]] == [
        ("1", 5000), ("2", 5100)]
    assert lots["iaai-active"] == []


def insert_lot(conn, lot, **values):
    row = {"vin": f"VIN{lot}", "year": 2018, "make": "VOLVO", "odometer": 50000,
           "damage_primary": "FRONT END", "high_bid": 3000, "buy_it_now_price": 0,
           "sale_at": AUCTION, "last_seen_at": AUCTION - timedelta(minutes=30),
           "removed_at": AUCTION + timedelta(hours=3), **values}
    conn.execute(
        f"""INSERT INTO lots (source, lot_number, attributes, row_hash, first_seen_at,
                              last_changed_at, {', '.join(row)})
            VALUES ('copart', %s, '{{}}', '\\x00', now(), now(), {', '.join(['%s'] * len(row))})""",
        (lot, *row.values()))


@pytest.fixture
def checked_run(conn, run_dir):
    """Our lots and both APIs' pages for a run with every kind of finding."""
    insert_lot(conn, "1")  # sold at its auction, listed by both APIs
    insert_lot(conn, "2")  # sold at its auction, missing from apibara
    insert_lot(conn, "3")  # apibara has another VIN
    insert_lot(conn, "4", sale_at=AUCTION + timedelta(days=6), buy_it_now_price=7000,
               last_seen_at=AUCTION - timedelta(hours=4), removed_at=None)  # bought before
    insert_lot(conn, "5", sale_at=datetime(2026, 9, 30, 15, tzinfo=UTC),
               last_seen_at=datetime.now(UTC), removed_at=None)  # upcoming
    insert_lot(conn, "6", make="AUDI")  # another make: not in the run
    save(run_dir, "apibara", "copart-sold", [
        apibara("1"), apibara("3", vin="OTHER", price=2000),
        apibara("4", price=7000, at="2026-09-23T11:00:00+00:00"),
        apibara("9")])  # a yard we do not download
    save(run_dir, "apibara", "copart-active", [
        apibara("5", status=None, price=0, at="2026-09-30T15:00:00+00:00")])
    save(run_dir, "apibara", "iaai-sold", [
        apibara("100", platform="iaai", price=3000, bid=2500),
        apibara("101", platform="iaai", status="Not Sold", price=0),
        apibara("102", platform="iaai", price=400, bid=11200)])  # below its own bid
    save(run_dir, "apiauctions", "copart-sold", [
        {"lot_number": "1", "vin": "VIN1", "auction": "COPART", "make": "VOLVO",
         "sale_price": 5000, "auction_date": "2026-09-23T14:05:00Z"}])
    return run_dir


def test_report_measures_each_api_against_our_lots(conn, checked_run):
    summary = api_check.report(conn, checked_run)
    sold = summary["apis"]["apibara"]["copart-sold"]
    assert (sold["lots"], sold["ours"], sold["ours_in_api"], sold["api_in_ours"]) == (
        4, 3, 0.6667, 0.75)
    assert (sold["checks"]["vin"], sold["checks"]["price_at_least_prebid"]) == (0.6667, 0.6667)
    assert (sold["checks"]["sale_time"], sold["checks"]["presale_at_buy_now"]) == (1.0, 1.0)
    assert (sold["price_present"], sold["price_at_least_bid"]) == (1.0, None)
    active = summary["apis"]["apibara"]["copart-active"]
    assert (active["ours"], active["ours_in_api"], active["checks"]["sale_time"]) == (1, 1.0, 1.0)
    iaai = summary["apis"]["apibara"]["iaai-sold"]
    assert (iaai["lots"], iaai["price_present"], iaai["price_at_least_bid"]) == (3, 1.0, 0.5)
    assert iaai["statuses"] == {"Sold": 2, "Not Sold": 1}
    assert "ours" not in iaai
    assert iaai["fields"]["final_price"] == 0.6667
    assert summary["cross"]["copart-sold"] == {"overlap": 0.25, "same_price": 1.0,
                                               "same_vin": 1.0}
    assert summary["cross"]["iaai-active"] == {"overlap": None}

    with (checked_run / "mismatches.csv").open() as f:
        problems = {(row["api"], row["lot_number"]): row["problem"] for row in csv.DictReader(f)}
    assert problems[("apibara", "2")] == "missing_from_api"
    assert problems[("apibara", "3")] == "vin,price_at_least_prebid"
    assert problems[("apibara", "102")] == "price_below_its_bid"
    assert problems[("apibara", "9")] == "not_in_ours"
    assert json.loads((checked_run / "summary.json").read_text())["run"] == RUN


def test_report_writes_a_spot_check_list_then_compares_it(conn, checked_run):
    api_check.report(conn, checked_run)
    manual = checked_run / "manual.csv"
    with manual.open() as f:
        rows = list(csv.DictReader(f))
    # Doubtful prices first, then lots both APIs list; lots that did not sell are left out.
    assert [(r["source"], r["lot_number"]) for r in rows] == [
        ("copart", "3"), ("copart", "1"), ("copart", "4"), ("copart", "9"),
        ("iaai", "102"), ("iaai", "100")]
    assert rows[1]["url"] == "https://www.copart.com/lot/1"
    assert rows[5]["url"] == ""

    rows[1]["final_price"], rows[5]["final_price"] = "5,000", "2900"
    with manual.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    summary = api_check.report(conn, checked_run)
    assert summary["manual"] == {"apibara": {"checked": 2, "same_price": 0.5},
                                 "apiauctions": {"checked": 1, "same_price": 1.0}}
    text = api_check.render(summary)
    assert "    ours_in_api: 66.7% BELOW 95%" in text
    assert "    price_present: 100.0% ok" in text
    assert "    overlap: n/a" in text
    assert "apibara vs apiauctions, copart-sold" in text
    assert "manual spot check, apibara: 2 lots\n    same_price: 50.0% BELOW 95%" in text


def test_cli_fetches_from_apis_with_a_key_then_reports(conn, settings, tmp_path, monkeypatch,
                                                       capsys):
    calls = fake_api(monkeypatch, pages_per_slice=1)
    monkeypatch.setattr(api_check, "get_settings",
                        lambda: settings.model_copy(update={"apibara_api_key": "k"}))
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    args = ["--sold-date", "2026-09-23", "--active-date", "2026-09-30", "--make", "volvo",
            "--dir", str(tmp_path)]
    api_check.main(["fetch", *args])
    out = capsys.readouterr().out
    assert "apibara: 4 requests" in out
    assert "apiauctions: skipped, APIAUCTIONS_TOKEN is not set" in out
    run_dir = tmp_path / "2026-09-23_2026-09-30_VOLVO"
    assert json.loads((run_dir / "run.json").read_text()) == RUN
    assert len(calls) == 4

    api_check.main(["report", str(run_dir)])
    out = capsys.readouterr().out
    assert "copart-sold: 0 lots, 0 of ours expected" in out
    assert f"details in {run_dir}" in out


def test_cli_keeps_going_when_an_api_refuses(settings, tmp_path, monkeypatch, capsys):
    def refuse(key, s, cursor):
        raise urllib.error.HTTPError("https://apibara.tech", 402, "Payment Required", {}, None)

    spec = api_check.APIS["apibara"]
    monkeypatch.setitem(api_check.APIS, "apibara",
                        Api(spec.key_setting, refuse, spec.next_cursor, spec.normalize))
    monkeypatch.setattr(api_check, "get_settings",
                        lambda: settings.model_copy(update={"apibara_api_key": "k"}))
    api_check.main(["fetch", "--sold-date", "2026-09-23", "--active-date", "2026-09-30",
                    "--make", "VOLVO", "--dir", str(tmp_path)])
    assert "apibara: stopped: HTTP Error 402: Payment Required" in capsys.readouterr().out


def test_cli_needs_a_command():
    with pytest.raises(SystemExit):
        api_check.main([])
