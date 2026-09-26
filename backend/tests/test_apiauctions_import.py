"""Real sale prices from apiauctions: fetching pages, learning translations, loading `lots`."""

import io
import json
import urllib.error
from datetime import UTC, date, datetime, timedelta

import psycopg
import pytest
from psycopg.rows import dict_row

from ingest import api_sync, apiauctions_import, auction_apis, db
from ingest.apiauctions_import import calibrate, fetch, flatten, load, plan, to_lot

NIGHT_SALE = datetime(2026, 8, 20, 1, tzinfo=UTC)  # Copart's sale of Aug 19, 9 pm Eastern
CODES = {"vehicle_type": {"AUTOMOBILE": "V"},
         "title_type": {"NC - SALVAGE CERTIFICATE OF TITLE": "SC"},
         "runs_drives": {"RUNS AND DRIVES": "Run & Drive Verified"}}


def car(lot, auction="COPART", status="Sold", price=5000, sold_at=NIGHT_SALE, **fields):
    """A lot as apiauctions returns it (trimmed from a real page)."""
    return {"auction_name": auction, "lot_number": int(lot), "vin": f"VIN{lot}", "year": 2019,
            "make": "VOLVO", "model": "XC90", "body_style": "SPORT UTILITY VEHICLE",
            "color": "WHITE", "primary_damage": "FRONT END", "secondary_damage": None,
            "vehicle_type": "AUTOMOBILE", "doc_type": "NC - SALVAGE CERTIFICATE OF TITLE",
            "highlights": "RUNS AND DRIVES", "car_keys": "yes", "odometer": 70000,
            "est_retail_value": 21500, "engine_type": "2.0L  4", "drive": "ALL WHEEL DRIVE",
            "transmission": "AUTOMATIC", "fuel": "GAS", "cylinders": "4",
            "location": "MA - NORTH BOSTON", "seller": None,
            "created_at": "2026-08-10 16:58:31", "buy_now_car": {"purchase_price": 9300},
            "car_photo": {"photo": ["https://cs.copart.com/1.jpg"]},
            "sales_history": [{"purchase_price": price, "sale_status": status,
                               "sold": int(status == "Sold"),
                               "sale_date": int(sold_at.timestamp())}],
            **fields}


def test_a_sold_copart_lot_is_mapped_onto_our_columns():
    row = to_lot(car("70139086"), CODES)
    assert (row["source"], row["lot_number"], row["final_price"]) == ("copart", "70139086", 5000)
    assert (row["sale_at"], row["sale_date"], row["sold_at"]) == (
        NIGHT_SALE, date(2026, 8, 19), NIGHT_SALE)
    assert (row["vehicle_type"], row["title_type"], row["title_state"], row["runs_drives"]) == (
        "V", "SC", "NC", "Run & Drive Verified")
    assert (row["yard_name"], row["location_state"], row["has_keys"], row["engine"]) == (
        "MA - NORTH BOSTON", "MA", "YES", "2.0L 4")
    assert (row["make"], row["model_group"], row["damage_primary"], row["odometer"],
            row["est_retail_value"], row["buy_it_now_price"]) == (
        "VOLVO", "XC90", "FRONT END", 70000, 21500, 9300)
    assert (row["high_bid"], row["repair_cost"], row["odometer_brand"]) == (None, None, None)
    assert row["first_seen_at"] == datetime(2026, 8, 10, 16, 58, 31, tzinfo=UTC)
    assert row["source_created_at"] == datetime(2026, 8, 10, 16, 58, 31)
    attributes = json.loads(row["attributes"])
    assert attributes["sales_history.0.purchase_price"] == "5000"
    assert attributes["car_photo.photo.0"] == "https://cs.copart.com/1.jpg"
    assert "secondary_damage" not in attributes


def test_an_unsold_lot_has_no_final_price_and_unknown_wording_no_code():
    row = to_lot(car("1", status="Not sold", doc_type="BILL OF SALE", highlights=None,
                     car_keys=None, location="", created_at=None), CODES)
    assert row["final_price"] is None
    assert (row["title_type"], row["title_state"], row["runs_drives"], row["has_keys"]) == (
        None, None, None, None)
    assert (row["yard_name"], row["location_state"]) == (None, None)
    assert row["first_seen_at"] == NIGHT_SALE  # no listing time: the sale's


def test_a_title_from_a_state_calibration_never_saw_uses_its_wording_in_any_state():
    codes = {"title_type": {"* - CERTIFICATE OF TITLE (P)": "CT"}}
    assert to_lot(car("1", doc_type="TN - CERTIFICATE OF TITLE (P)"), codes)["title_type"] == "CT"
    assert to_lot(car("1", doc_type="CERTIFICATE OF TITLE (P)"), codes)["title_type"] is None


def test_an_iaa_lot_keeps_its_own_wording():
    row = to_lot(car("45665915", auction="IAAI", doc_type="SALVAGE", vehicle_type="SUV"), CODES)
    assert (row["source"], row["vehicle_type"], row["title_type"], row["runs_drives"]) == (
        "iaai", "SUV", "SALVAGE", "RUNS AND DRIVES")


def test_a_lot_without_a_past_sale_is_not_loaded():
    assert to_lot(car("1", sales_history=[]), CODES) is None
    assert to_lot(car("1", sold_at=datetime.now(UTC) + timedelta(days=2)), CODES) is None


def test_flatten_gives_dotted_text_values():
    assert flatten({"a": {"b": [1, None, {"c": True}]}, "d": None, "e": "x"}) == {
        "a.b.0": "1", "a.b.2.c": "True", "e": "x"}


def test_the_plan_reads_calibration_days_then_goes_back_from_the_start():
    steps = plan(date(2026, 8, 25))
    calibration = [next(steps) for _ in apiauctions_import.CALIBRATION]
    assert calibration[0] == ("COPART", date(2026, 9, 18), 9)
    assert calibration[-1] == ("COPART", date(2026, 9, 24), 9)
    assert [next(steps) for _ in range(4)] == [
        ("COPART", date(2026, 8, 25), None), ("IAAI", date(2026, 8, 25), None),
        ("COPART", date(2026, 8, 24), None), ("IAAI", date(2026, 8, 24), None)]


class FakeApi:
    """apiauctions with `pages` pages a day; `fail` maps a request number to an HTTP error: a
    status, or (status, Retry-After, message)."""

    def __init__(self, pages=2, fail=None):
        self.pages, self.fail, self.calls = pages, fail or {}, []

    def __call__(self, token, endpoint, params):
        self.calls.append((params["auction_name"], params["auction_date_from"], params["page"]))
        if (error := self.fail.pop(len(self.calls), None)) is not None:
            status, wait, message = error if isinstance(error, tuple) else (error, None, None)
            body = io.BytesIO(json.dumps({"message": message}).encode()) if message else None
            raise urllib.error.HTTPError("https://apiauctions.io", status, "Too Many Requests",
                                         {"Retry-After": wait} if wait else {}, body)
        assert (token, endpoint, params["per_page"]) == ("tok", "get-cars", 50)
        return {"result": [], "pagination": {"total": 50 * self.pages,
                                             "total_pages": self.pages}}


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(apiauctions_import.time, "sleep", lambda seconds: None)


@pytest.fixture
def one_calibration_page(monkeypatch):
    monkeypatch.setattr(apiauctions_import, "CALIBRATION", (date(2026, 9, 18),))
    monkeypatch.setattr(apiauctions_import, "CALIBRATION_PAGES", 1)


def test_fetch_follows_the_plan_within_budget_and_never_repeats_a_page(
        tmp_path, one_calibration_page):
    api = FakeApi(pages=2)
    assert fetch("tok", tmp_path, date(2026, 8, 25), 4, api) == 4
    assert api.calls == [("COPART", date(2026, 9, 18), 1),  # calibration: one page
                         ("COPART", date(2026, 8, 25), 1), ("COPART", date(2026, 8, 25), 2),
                         ("IAAI", date(2026, 8, 25), 1)]
    assert fetch("tok", tmp_path, date(2026, 8, 25), 2, api) == 2
    assert api.calls[4:] == [("IAAI", date(2026, 8, 25), 2), ("COPART", date(2026, 8, 24), 1)]
    assert (tmp_path / "IAAI" / "2026-08-25" / "2.json").is_file()


def test_fetch_stops_when_the_credit_runs_out_and_waits_when_rate_limited(
        tmp_path, capsys, one_calibration_page):
    api = FakeApi(pages=1, fail={2: 429, 4: 402})
    assert fetch("tok", tmp_path, date(2026, 8, 25), 100, api) == 2
    assert api.calls[1] == api.calls[2]  # asked again after the 429
    assert "credit used up after 2 requests" in capsys.readouterr().out


def test_fetch_hands_over_each_new_page_as_it_arrives(tmp_path, one_calibration_page):
    class Api(FakeApi):
        def __call__(self, token, endpoint, params):
            return {**super().__call__(token, endpoint, params), "result": [car(len(self.calls))]}

    pages = []
    fetch("tok", tmp_path, date(2026, 8, 25), 2, Api(pages=1), pages.append)
    assert [[c["lot_number"] for c in cars] for cars in pages] == [[1], [2]]
    fetch("tok", tmp_path, date(2026, 8, 25), 1, Api(pages=1), pages.append)
    assert len(pages) == 3  # the saved pages were not handed over again


def test_fetch_waits_only_for_the_rest_of_each_second(tmp_path, monkeypatch,
                                                     one_calibration_page):
    waits, clock = [], iter([10.0, 10.25])  # the request took 0.25 s
    monkeypatch.setattr(apiauctions_import.time, "sleep", waits.append)
    monkeypatch.setattr(apiauctions_import.time, "monotonic", lambda: next(clock))
    fetch("tok", tmp_path, date(2026, 8, 25), 1, FakeApi())
    assert waits == [0.75]


def test_fetch_stops_at_the_monthly_spend_cap_instead_of_waiting_days(
        tmp_path, capsys, monkeypatch, one_calibration_page):
    waits = []
    monkeypatch.setattr(apiauctions_import.time, "sleep", waits.append)
    api = FakeApi(pages=1, fail={2: (429, "5", None), 4: (429, "430452",
                                                        "Monthly spend cap reached.")})
    assert fetch("tok", tmp_path, date(2026, 8, 25), 100, api) == 2
    assert 5 in waits  # a short Retry-After is waited out
    assert "stopped after 2 requests: Monthly spend cap reached. (retry in 119 h)" in (
        capsys.readouterr().out)
    # Without a readable explanation, the status's reason stands in.
    fetch("tok", tmp_path, date(2026, 8, 25), 100, FakeApi(fail={1: (429, "7200", None)}))
    assert "stopped after 0 requests: Too Many Requests (retry in 2 h)" in capsys.readouterr().out


def test_fetch_raises_other_errors(tmp_path):
    with pytest.raises(urllib.error.HTTPError):
        fetch("tok", tmp_path, date(2026, 8, 25), 100, FakeApi(fail={1: 500}))


def test_a_day_without_lots_moves_on(tmp_path, one_calibration_page):
    api = FakeApi(pages=0)
    assert fetch("tok", tmp_path, date(2026, 8, 25), 3, api) == 3
    assert api.calls[-1] == ("IAAI", date(2026, 8, 25), 1)


def export_lot(conn, lot, **values):
    """A Copart lot an export brought."""
    file_id = conn.execute(
        """INSERT INTO ingest_files (source, filename, sha256, status)
           VALUES ('copart', 'x.csv', %s, 'succeeded') RETURNING id""", (lot,)).fetchone()["id"]
    row = {"make": "VOLVO", "vehicle_type": "V", "title_type": "SC",
           "runs_drives": "Run & Drive Verified", **values}
    conn.execute(
        f"""INSERT INTO lots (source, lot_number, {', '.join(row)}, attributes, row_hash,
                              first_seen_at, last_seen_at, last_changed_at, first_file_id,
                              last_file_id)
            VALUES ('copart', %s, {', '.join(['%s'] * len(row))}, '{{}}', '\\x00', now(), now(),
                    now(), %s, %s)""", (lot, *row.values(), file_id, file_id))


def test_calibrate_learns_the_codes_our_export_gives_the_same_lots(conn):
    for n in range(4):
        export_lot(conn, str(n))
    export_lot(conn, "10", title_type="ST")
    export_lot(conn, "11", title_type="ST")
    export_lot(conn, "12", title_type="SV")
    export_lot(conn, "20", runs_drives="DEFAULT")
    export_lot(conn, "30", title_type="CT")
    export_lot(conn, "31", title_type="CT")
    export_lot(conn, "32", title_type="CT")
    cars = [car(n) for n in range(4)] + [
        car("10", doc_type="VA - TITLE"), car("11", doc_type="VA - TITLE"),
        car("12", doc_type="VA - TITLE"),  # 2 of 3 agree: under 80 %
        car("20", highlights=None), car("99"),  # 99: not one of ours
        car("30", doc_type="AL - TITLE (P)"), car("31", doc_type="MA - TITLE (P)"),
        car("32", doc_type="TN - TITLE (P)")]
    tables = calibrate(conn, cars)
    assert tables["vehicle_type"] == {"AUTOMOBILE": "V"}
    assert tables["title_type"] == {"NC - SALVAGE CERTIFICATE OF TITLE": "SC",
                                    "* - SALVAGE CERTIFICATE OF TITLE": "SC",
                                    "* - TITLE (P)": "CT"}  # 1 lot in each of 3 states
    assert tables["runs_drives"] == {"RUNS AND DRIVES": "Run & Drive Verified"}  # "" seen once


def save(out, auction, day, cars, page=1):
    path = out / auction / day / f"{page}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"result": cars}))


def lots(conn):
    return {r["lot_number"]: r for r in conn.execute("SELECT * FROM lots").fetchall()}


def test_load_writes_finished_lots_and_prices_ours_without_touching_them(
        conn, tmp_path, capsys):
    export_lot(conn, "1", make="OURS")
    save(tmp_path, "COPART", "2026-08-19", [car("1", price=6100), car("2"),
                                           car("3", status="Not sold")])
    save(tmp_path, "COPART", "2026-08-19", [car("2")], page=2)  # listed twice: loaded once
    save(tmp_path, "IAAI", "2026-08-19", [car("4", auction="IAAI"), car("5", sales_history=[])])
    save(tmp_path, "COPART UK", "2026-08-19", [car("6")])  # not a US auction: ignored

    counts = load(conn, tmp_path, CODES)
    assert counts == {"new": 3, "priced": 1, "refreshed": 0, "skipped": 1, "sold": 3}
    assert "IAAI 2026-08-19: 1 lots" in capsys.readouterr().out
    rows = lots(conn)
    assert set(rows) == {"1", "2", "3", "4"}
    ours = rows["1"]
    assert (ours["make"], ours["final_price"], ours["removed_at"]) == ("OURS", 6100, None)
    new = rows["2"]
    assert (new["final_price"], new["removed_at"], new["last_seen_at"]) == (
        5000, NIGHT_SALE, NIGHT_SALE)
    assert (new["first_file_id"], new["last_file_id"], new["title_type"]) == (None, None, "SC")
    assert new["attributes"]["sales_history.0.purchase_price"] == "5000"
    assert rows["3"]["final_price"] is None
    assert rows["4"]["source"] == "iaai"


def test_a_reload_refreshes_only_lots_no_export_brought(conn, tmp_path):
    export_lot(conn, "1", make="OURS")
    save(tmp_path, "COPART", "2026-08-19", [car("1"), car("2", doc_type="VA - TITLE")])
    load(conn, tmp_path, CODES)
    version = lots(conn)["2"]["sync_version"]
    assert lots(conn)["2"]["title_type"] is None

    codes = {**CODES, "title_type": {"VA - TITLE": "ST"}}  # a better translation table
    assert load(conn, tmp_path, codes)["refreshed"] == 1
    rows = lots(conn)
    assert rows["2"]["title_type"] == "ST"
    assert rows["2"]["sync_version"] > version
    assert rows["1"]["make"] == "OURS"
    assert load(conn, tmp_path, codes)["refreshed"] == 0  # nothing changed


def test_load_tells_the_dashboard_the_lot_totals_changed(conn, db_url, tmp_path):
    save(tmp_path, "COPART", "2026-08-19", [car("2")])
    with psycopg.connect(db_url, autocommit=True) as listener:
        listener.execute(f"LISTEN {db.EVENTS_CHANNEL}")
        load(conn, tmp_path, CODES)
        events = [json.loads(n.payload) for n in listener.notifies(timeout=2, stop_after=1)]
    assert events == [{"type": "lots_imported", "new": 1, "priced": 0, "refreshed": 0}]


def test_the_api_database_never_gets_imported_lots(conn, tmp_path, api_url):
    save(tmp_path, "COPART", "2026-08-19", [car("2")])
    load(conn, tmp_path, CODES)
    api_sync.sync(conn, api_url, 240)
    with psycopg.connect(api_url, row_factory=dict_row) as api:
        assert api.execute("SELECT count(*) AS n FROM lots").fetchone()["n"] == 0


def test_cli(conn, settings, tmp_path, monkeypatch, capsys, one_calibration_page):
    monkeypatch.setattr(apiauctions_import, "get_settings", lambda: settings)
    with pytest.raises(SystemExit, match="APIAUCTIONS_TOKEN"):
        apiauctions_import.main(["--dir", str(tmp_path), "fetch"])

    api = FakeApi(pages=1)
    monkeypatch.setattr(auction_apis, "apiauctions_page", api)
    monkeypatch.setattr(apiauctions_import, "get_settings",
                        lambda: settings.model_copy(update={"apiauctions_token": "tok"}))
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    apiauctions_import.main(["--dir", str(tmp_path), "fetch", "--max-requests", "3"])
    assert "3 requests" in capsys.readouterr().out

    # Each fetched page lands in lots at once: the dashboard follows the fetch.
    sold = datetime(2026, 8, 24, 15, tzinfo=UTC)
    monkeypatch.setattr(auction_apis, "apiauctions_page", lambda token, endpoint, params: {
        "result": [car("7", sold_at=sold), car("8", auction="COPART UK")],
        "pagination": {"total": 2, "total_pages": 1}})
    apiauctions_import.main(["--dir", str(tmp_path), "fetch", "--max-requests", "1"])
    assert lots(conn)["7"]["final_price"] == 5000
    assert "8" not in lots(conn)  # not a US auction

    codes = tmp_path / "codes.json"
    monkeypatch.setattr(apiauctions_import, "CODES", codes)
    export_lot(conn, "1")
    save(tmp_path, "COPART", "2026-09-18", [car("1")] * 3)
    apiauctions_import.main(["--dir", str(tmp_path), "calibrate"])
    assert json.loads(codes.read_text())["vehicle_type"] == {}  # one lot is not enough
    apiauctions_import.main(["--dir", str(tmp_path), "load"])
    assert "'priced': 1" in capsys.readouterr().out

    codes.unlink()  # loading works without a translation table
    apiauctions_import.main(["--dir", str(tmp_path), "load"])
    assert lots(conn)["1"]["final_price"] == 5000


def test_cli_needs_a_command():
    with pytest.raises(SystemExit):
        apiauctions_import.main([])
