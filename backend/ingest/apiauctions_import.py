"""Real sale prices from apiauctions.io, stored in `lots` for the price model to learn from.

    python -m ingest.apiauctions_import fetch [--start 2026-08-25] [--max-requests N]
    python -m ingest.apiauctions_import calibrate      # translation tables, from our own lots
    python -m ingest.apiauctions_import load           # offline, can be rerun

`fetch` spends requests ($0.01 each, 50 lots per request). It first reads a few pages of the
Copart sales we also have (CALIBRATION), then every US Copart and IAAI lot of `--start`, then of
each day before it, until the credit runs out (402) or `--max-requests`. Each raw page is saved
under `<dir>/<AUCTION>/<day>/<page>.json` and never requested again.

apiauctions words a few Copart columns differently from our export (`AUTOMOBILE` for `V`, a
state's title description for `SC`, `RUNS AND DRIVES` for `Run & Drive Verified`). `calibrate`
learns those translations from the lots both have and writes them to `apiauctions_codes.json`;
a value it could not learn is stored as NULL (the raw value stays in `attributes`).

`load` maps each lot onto the canonical columns and writes it as a finished lot: removed at its
latest sale, without file ids (no export brought it), `final_price` set only if it sold. A lot an
export brought keeps its columns and only gets `final_price`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import urllib.error
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from psycopg import Connection

from . import auction_apis
from .auction_apis import epoch
from .config import get_settings
from .db import notify
from .loader import NEXT_SYNC_VERSION
from .schema import CANONICAL_COLUMNS, COLUMN_NAMES

AUCTIONS = ("COPART", "IAAI")  # US only
SOURCES = {"COPART": "copart", "IAAI": "iaai"}
# Copart sales our own lots also cover: they teach the translations (calibrate).
CALIBRATION = tuple(date(2026, 9, 18) + timedelta(days=d) for d in range(7))
CALIBRATION_PAGES = 9
PER_PAGE = 50  # the API's maximum
PAUSE = 1.0  # at least this many seconds per request: the API allows 60 a minute
CODES = Path(__file__).with_name("apiauctions_codes.json")
# Our column <- the apiauctions field it is translated from (Copart only).
TRANSLATED = {"vehicle_type": "vehicle_type", "title_type": "doc_type",
              "runs_drives": "highlights"}
MIN_LOTS, MIN_SHARE = 3, 0.8  # a translation needs this many lots, this share agreeing
EASTERN = ZoneInfo("America/New_York")  # Copart's sale date: a 01:00 UTC night sale is the eve

Get = Callable[[str, str, Mapping[str, Any]], Any]


def plan(start: date) -> Iterator[tuple[str, date, int | None]]:
    """(auction, day, page limit) in fetch order: calibration, then from `start` backwards."""
    for day in CALIBRATION:
        yield "COPART", day, CALIBRATION_PAGES
    day = start
    while True:
        for auction in AUCTIONS:
            yield auction, day, None
        day -= timedelta(days=1)


def fetch(token: str, out: Path, start: date, budget: int, get: Get | None = None) -> int:
    """Saves the pages not saved yet, in `plan` order, until `budget` requests are spent or the
    credit runs out (402). Returns the requests spent."""
    get = get or auction_apis.apiauctions_page
    spent = 0
    for auction, day, limit in plan(start):
        page = 1
        while True:
            path = out / auction / day.isoformat() / f"{page}.json"
            if path.exists():
                body = json.loads(path.read_text())
            elif spent == budget:
                return spent
            else:
                params = {"auction_name": auction, "auction_date_from": day,
                          "auction_date_to": day, "page": page, "per_page": PER_PAGE}
                started = time.monotonic()
                try:
                    body = get(token, "get-cars", params)
                except urllib.error.HTTPError as error:
                    if error.code == 402:
                        print(f"credit used up after {spent} requests")
                        return spent
                    if error.code != 429:
                        raise
                    time.sleep(60)  # rate limited: wait, then ask again
                    continue
                spent += 1
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(body))
                time.sleep(max(0.0, PAUSE - (time.monotonic() - started)))
            pages = (body.get("pagination") or {}).get("total_pages") or 0
            if page == 1:
                total = (body.get("pagination") or {}).get("total")
                print(f"{auction} {day}: {total} lots, {spent} requests so far", flush=True)
            if page >= min(pages, limit or pages):
                break
            page += 1


def pages(out: Path, auction: str = "*") -> Iterator[tuple[str, date, list[dict[str, Any]]]]:
    """(auction, day, its lots) of the saved pages, a lot listed on two pages once."""
    for folder in sorted(out.glob(f"{auction}/*")):
        lots = {}
        for path in sorted(folder.glob("*.json"), key=lambda p: int(p.stem)):
            for car in json.loads(path.read_text()).get("result") or []:
                lots[str(car["lot_number"])] = car
        yield folder.parent.name, date.fromisoformat(folder.name), list(lots.values())


def calibrate(conn: Connection, cars: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, str]]:
    """Per translated column: apiauctions value -> the code our export gives the same lots."""
    by_lot = {str(car["lot_number"]): car for car in cars}
    ours = conn.execute(
        f"""SELECT lot_number, {', '.join(TRANSLATED)} FROM lots
            WHERE source = 'copart' AND last_file_id IS NOT NULL AND lot_number = ANY(%s)""",
        (list(by_lot),)).fetchall()
    tables = {}
    for column, field in TRANSLATED.items():
        seen: dict[str, Counter] = defaultdict(Counter)
        for row in ours:
            if row[column] is not None:
                value = by_lot[row["lot_number"]].get(field) or ""
                for key in {value, _any_state(value)} - {None}:
                    seen[key][row[column]] += 1
        tables[column] = {
            value: code for value, codes in sorted(seen.items())
            for code, n in codes.most_common(1)
            if codes.total() >= MIN_LOTS and n >= MIN_SHARE * codes.total()}
    return tables


def flatten(value: Any, prefix: str = "") -> dict[str, str]:
    """Nested JSON as dotted keys with text values: `attributes` is shown as text."""
    if isinstance(value, dict | list):
        items = value.items() if isinstance(value, dict) else enumerate(value)
        return {k: v for key, item in items
                for k, v in flatten(item, f"{prefix}.{key}" if prefix else str(key)).items()}
    return {} if value is None else {prefix: str(value)}


def _any_state(text: str) -> str | None:
    """`NC - SALVAGE CERTIFICATE` -> `* - SALVAGE CERTIFICATE`: a title's wording in any state,
    translated only when every state using it agrees."""
    state = _state(text)
    return None if state is None else "*" + text[len(state):]


def _state(text: str) -> str | None:
    """The state prefix of `NC - SALVAGE CERTIFICATE` or `MA - NORTH BOSTON`."""
    state, dash, _ = text.partition(" - ")
    return state if dash and len(state) == 2 else None


def to_lot(car: Mapping[str, Any], codes: Mapping[str, Mapping[str, str]]) -> dict[str, Any] | None:
    """The `lots` row of one apiauctions lot, or None when it has no past sale yet."""
    sales = sorted(car.get("sales_history") or [], key=lambda s: s.get("sale_date") or 0)
    last = sales[-1] if sales else {}
    sold_at = epoch(last.get("sale_date"))
    if sold_at is None or sold_at > datetime.now(UTC):
        return None
    copart = car["auction_name"] == "COPART"

    def translated(column: str) -> Any:
        value = car.get(TRANSLATED[column])
        if not copart:
            return value
        table = codes.get(column, {})
        return table.get(value or "", table.get(_any_state(value or "") or ""))

    location, title = car.get("location") or "", car.get("doc_type") or ""
    try:
        created = datetime.fromisoformat(car["created_at"]).replace(tzinfo=UTC)
    except (KeyError, TypeError, ValueError):
        created = sold_at
    columns = {
        "yard_name": location or None, "sale_date": sold_at.astimezone(EASTERN).date(),
        "sale_at": sold_at, "vehicle_type": translated("vehicle_type"), "year": car.get("year"),
        "make": car.get("make"), "model_group": car.get("model"),
        "body_style": car.get("body_style"), "color": car.get("color"),
        "damage_primary": car.get("primary_damage"),
        "damage_secondary": car.get("secondary_damage"), "title_state": _state(title),
        "title_type": translated("title_type"),
        "has_keys": (car.get("car_keys") or "").upper() or None, "vin": car.get("vin"),
        "odometer": car.get("odometer"), "est_retail_value": car.get("est_retail_value"),
        "engine": " ".join((car.get("engine_type") or "").split()) or None,
        "drive": car.get("drive"), "transmission": car.get("transmission"),
        "fuel_type": car.get("fuel"), "cylinders": car.get("cylinders"),
        "runs_drives": translated("runs_drives"),
        "buy_it_now_price": auction_apis.money((car.get("buy_now_car") or {})
                                               .get("purchase_price")),
        "currency": "USD", "location_state": _state(location), "seller_name": car.get("seller"),
        "source_created_at": created.replace(tzinfo=None),
    }
    return {
        "source": SOURCES[car["auction_name"]], "lot_number": str(car["lot_number"]),
        **{name: columns.get(name) for name in COLUMN_NAMES},
        "final_price": auction_apis.money(last.get("purchase_price")) if last.get("sold") else None,
        "attributes": json.dumps(flatten(car)),
        "row_hash": hashlib.sha256(json.dumps(car, sort_keys=True).encode()).digest(),
        "first_seen_at": min(created, sold_at), "sold_at": sold_at,
    }


_STAGE = ", ".join(["source text", "lot_number text",
                    *(f"{name} {sql_type}" for name, sql_type in CANONICAL_COLUMNS),
                    "final_price numeric", "attributes jsonb", "row_hash bytea",
                    "first_seen_at timestamptz", "sold_at timestamptz"])
_FIELDS = ["source", "lot_number", *COLUMN_NAMES, "final_price", "attributes", "row_hash",
           "first_seen_at", "sold_at"]
_TYPED = ", ".join(COLUMN_NAMES)
_S_TYPED = ", ".join(f"s.{name}" for name in COLUMN_NAMES)


def write(conn: Connection, rows: list[dict[str, Any]]) -> dict[str, int]:
    """One transaction under the loader's lock: insert new lots as finished ones, set
    `final_price` on every lot, refresh the lots only this import wrote, tell the dashboard.
    `conn` autocommits."""
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('ingest:lots'))")
        conn.execute(f"CREATE TEMP TABLE import_stage ({_STAGE}) ON COMMIT DROP")
        with conn.cursor().copy(f"COPY import_stage ({', '.join(_FIELDS)}) FROM STDIN") as copy:
            for row in rows:
                copy.write_row([row[field] for field in _FIELDS])
        new = conn.execute(
            f"""INSERT INTO lots (source, lot_number, {_TYPED}, final_price, attributes,
                                  row_hash, first_seen_at, last_seen_at, last_changed_at,
                                  removed_at)
                SELECT source, lot_number, {_TYPED}, final_price, attributes, row_hash,
                       first_seen_at, sold_at, sold_at, sold_at FROM import_stage
                ON CONFLICT (source, lot_number) DO NOTHING""").rowcount
        priced = conn.execute(
            """UPDATE lots l SET final_price = s.final_price FROM import_stage s
               WHERE l.source = s.source AND l.lot_number = s.lot_number
                 AND l.final_price IS DISTINCT FROM s.final_price""").rowcount
        # A lot no export brought: its columns follow the latest page and translations.
        refreshed = conn.execute(
            f"""UPDATE lots l SET ({_TYPED}, attributes, row_hash, last_seen_at,
                                   last_changed_at, removed_at, sync_version)
                    = ({_S_TYPED}, s.attributes, s.row_hash, s.sold_at, s.sold_at, s.sold_at,
                       {NEXT_SYNC_VERSION})
                FROM import_stage s
                WHERE l.source = s.source AND l.lot_number = s.lot_number
                  AND l.last_file_id IS NULL
                  AND ({', '.join(f'l.{n}' for n in COLUMN_NAMES)}, l.attributes, l.removed_at)
                      IS DISTINCT FROM ({_S_TYPED}, s.attributes, s.sold_at)""").rowcount
        counts = {"new": new, "priced": priced, "refreshed": refreshed}
        notify(conn, "lots_imported", **counts)  # the dashboard's lot totals and lists
    return counts


def load(conn: Connection, out: Path, codes: Mapping[str, Mapping[str, str]]) -> Counter:
    """Writes every saved day, one transaction per day; returns the counts."""
    counts: Counter = Counter()
    for auction, day, cars in pages(out):
        if auction not in AUCTIONS:
            continue
        rows = [row for car in cars if (row := to_lot(car, codes))]
        counts["skipped"] += len(cars) - len(rows)  # no past sale yet
        counts["sold"] += sum(row["final_price"] is not None for row in rows)
        counts.update(write(conn, rows))
        print(f"{auction} {day}: {len(rows)} lots", flush=True)
    return counts


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m ingest.apiauctions_import")
    parser.add_argument("--dir", type=Path, default=Path("/data/apiauctions"))
    commands = parser.add_subparsers(dest="command", required=True)
    get = commands.add_parser("fetch", help="save pages until the credit runs out")
    get.add_argument("--start", type=date.fromisoformat, default=date(2026, 8, 25))
    get.add_argument("--max-requests", type=int, default=3000)
    commands.add_parser("calibrate", help=f"learn the translations into {CODES.name}")
    commands.add_parser("load", help="write the saved pages into lots")
    args = parser.parse_args(argv)

    if args.command == "fetch":
        token = get_settings().apiauctions_token
        if not token:
            raise SystemExit("APIAUCTIONS_TOKEN is not set")
        print(f"{fetch(token, args.dir, args.start, args.max_requests)} requests")
        return
    from .db import connect

    with connect() as conn:
        if args.command == "calibrate":
            tables = calibrate(conn, (car for _, _, cars in pages(args.dir, "COPART")
                                      for car in cars))
            CODES.write_text(json.dumps(tables, indent=2, sort_keys=True) + "\n")
            print({column: len(table) for column, table in tables.items()}, "->", CODES)
        else:
            codes = json.loads(CODES.read_text()) if CODES.exists() else {}
            print(dict(load(conn, args.dir, codes)))


if __name__ == "__main__":
    main()
