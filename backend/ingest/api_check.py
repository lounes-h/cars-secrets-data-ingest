"""Can we trust the auction data APIs (auction_apis.py)? Compares their lots and final sale prices
with our Copart lots, with each other, and with prices a person read on copart.com / iaai.com.

A run checks four slices: the Copart and IAA lots of one make that sold on one day, and those
scheduled for a later day. That is small enough for a free quota, and a slice is complete, so
coverage can be measured. `fetch` saves every raw page in the run folder and never requests a
saved page again; `report` works offline from those pages and writes `summary.json`,
`mismatches.csv` and, the first time, `manual.csv` for a person to fill in `final_price`.
Run `report` soon after `fetch`: the active slices are compared with our lots as they are now.

    docker compose run --rm worker python -m ingest.api_check fetch \
        --sold-date 2026-09-23 --active-date 2026-09-30 --make VOLVO
    docker compose run --rm worker python -m ingest.api_check report \
        /data/checks/2026-09-23_2026-09-30_VOLVO
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from psycopg import Connection

from . import auction_apis
from .auction_apis import Record, money
from .config import get_settings
from .providers.copart import listing_url

SOURCES = ("copart", "iaai")
KINDS = ("sold", "active")
MAX_PAGES = 6  # per slice and API; a slice holds well under 100 lots
MANUAL_LOTS = 30  # per auction
HOUR = timedelta(hours=1)
AUCTION = timedelta(hours=4)  # a live auction sells its lots over 2-4 h after it starts
# The share each check must reach for an API to be trusted; the others are for information.
PASS = {"ours_in_api": 0.95, "overlap": 0.95, "vin": 0.99, "year": 0.99, "make": 0.99,
        "price_present": 0.90, "price_at_least_bid": 1.0, "price_at_least_prebid": 1.0,
        "relisted_not_sold": 1.0, "same_status": 0.95, "same_price": 0.95,
        "sale_time": 0.98}
# How complete each API's lots are (what an IAA provider and the price model could use).
FIELDS = ("vin", "odometer", "damage", "has_keys", "runs", "title", "retail", "sale_at",
          "final_price", "current_bid", "buy_now")

# Our Copart lots the APIs listed, and those of the run's make and days (Copart's own sale
# date: a night auction at 01:00 UTC is on the US evening before). `sold`: still listed when its
# auction started and gone after it; `active`: still in the latest exports.
OURS = """
    SELECT lot_number, vin, year, make, odometer, damage_primary, high_bid, buy_it_now_price,
           sale_at, last_seen_at, removed_at, sale_date AS day,
           removed_at IS NOT NULL AND last_seen_at >= sale_at - interval '1 hour' AS sold,
           removed_at IS NULL AND last_seen_at >= (SELECT max(last_seen_at) FROM lots
                                                    WHERE source = 'copart')
                                                   - interval '2 hours' AS active
    FROM lots
    WHERE source = 'copart'
      AND (lot_number = ANY(%s) OR (make = %s AND sale_date = ANY(%s)))
"""


@dataclass(frozen=True)
class Slice:
    source: str
    kind: str
    day: date
    make: str

    @property
    def name(self) -> str:
        return f"{self.source}-{self.kind}"


def slices(run: Mapping[str, str]) -> list[Slice]:
    return [Slice(source, kind, date.fromisoformat(run[f"{kind}_date"]), run["make"])
            for kind in KINDS for source in SOURCES]


def _apibara(key: str, s: Slice, cursor: Any) -> Any:
    params = {"platform": s.source, "lot_sub_status": "Ended" if s.kind == "sold" else "Open",
              "make": s.make, "auction_date_from": s.day, "auction_date_to": s.day,
              "per_page": 40}  # the plan may cap it (20 on the free one)
    return auction_apis.apibara_page(key, {**params, "cursor": cursor} if cursor else params)


def _apiauctions(key: str, s: Slice, page: Any) -> Any:
    # US auctions are "COPART" and "IAAI" (/api/v2/get-auctions); 50 lots a page at most.
    params = {"auction_name": s.source.upper(), "make": s.make, "auction_date_from": s.day,
              "auction_date_to": s.day, "page": page or 1, "per_page": 50}
    endpoint = "get-cars" if s.kind == "sold" else "get-active-lots"
    return auction_apis.apiauctions_page(key, endpoint, params)


def _apiauctions_next(body: Any, page: Any) -> Any:
    page = page or 1
    return page + 1 if page < ((body.get("pagination") or {}).get("total_pages") or 0) else None


@dataclass(frozen=True)
class Api:
    key_setting: str
    request: Callable[[str, Slice, Any], Any]  # (key, slice, cursor) -> page
    next_cursor: Callable[[Any, Any], Any]  # (page, its cursor) -> the next cursor or None
    normalize: Callable[[Mapping[str, Any]], Record]


APIS = {
    "apibara": Api("apibara_api_key", _apibara,
                   lambda body, _: (body.get("meta") or {}).get("next_cursor"),
                   auction_apis.normalize_apibara),
    "apiauctions": Api("apiauctions_token", _apiauctions, _apiauctions_next,
                       auction_apis.normalize_apiauctions),
}


def _run(run_dir: Path) -> dict[str, str]:
    return json.loads((run_dir / "run.json").read_text())


def fetch(api: str, key: str, run_dir: Path, budget: int) -> int:
    """Requests the run's pages that are not saved yet, at most `budget`; returns how many."""
    spec, spent = APIS[api], 0
    for s in slices(_run(run_dir)):
        cursor = None
        for page in range(1, MAX_PAGES + 1):
            path = run_dir / api / s.name / f"{page}.json"
            if path.exists():
                body = json.loads(path.read_text())
            elif spent == budget:
                return spent
            else:
                body = spec.request(key, s, cursor)
                spent += 1
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(body))
            cursor = spec.next_cursor(body, cursor)
            if cursor is None:
                break
    return spent


def load(run_dir: Path, api: str) -> dict[str, list[Record]]:
    """The API's lots per slice, from the saved pages (a lot on two pages counts once)."""
    out = {}
    for s in slices(_run(run_dir)):
        lots = {}
        for path in sorted((run_dir / api / s.name).glob("*.json"), key=lambda p: int(p.stem)):
            body = json.loads(path.read_text())
            for item in body.get("data") or body.get("result") or []:  # apibara, apiauctions
                record = APIS[api].normalize(item)
                lots[record.lot_number] = record
        out[s.name] = list(lots.values())
    return out


def share(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def shares(results: Iterable[Mapping[str, bool | None]]) -> dict[str, float | None]:
    """Per check, the share of True among the results it applied to (not None)."""
    counts: dict[str, list[int]] = {}
    for result in results:
        for name, ok in result.items():
            if ok is not None:
                count = counts.setdefault(name, [0, 0])
                count[0] += ok
                count[1] += 1
    return {name: share(passed, total) for name, (passed, total) in counts.items()}


def _same(a: Any, b: Any) -> bool | None:
    return None if a is None or b is None else a == b


def _near(a: Any, b: Any, tolerance: Any) -> bool | None:
    return None if a is None or b is None else abs(a - b) <= tolerance


def _upper(text: Any) -> str | None:
    return text.upper() if isinstance(text, str) else None


def check(record: Record, ours: Mapping[str, Any], kind: str) -> dict[str, bool | None]:
    """Each check of an API lot against our lot: True, False, or None where it does not apply."""
    odometer = None if ours["odometer"] is None else float(ours["odometer"])
    result = {
        "vin": _same(record.vin, ours["vin"]),
        "year": _same(record.year, ours["year"]),
        "make": _same(_upper(record.make), ours["make"]),
        "odometer": _near(record.odometer, odometer, 1),
        "damage": _same(_upper(record.damage), ours["damage_primary"]),
    }
    price, buy_now = record.final_price, money(ours["buy_it_now_price"])
    if kind == "active":
        result["sale_time"] = _near(record.next_sale_at or record.sale_at, ours["sale_at"], HOUR)
        result["buy_now"] = _same(record.buy_now, buy_now)
        return result
    prebid = money(ours["high_bid"])
    # Bids are binding: an auction never ends below the high bid of our last export.
    result["price_at_least_prebid"] = None if price is None or prebid is None else price >= prebid
    if record.sale_at is None or ours["sale_at"] is None:
        return result
    # Listed after that auction for a later one: it came back unsold. (A lot bought before its
    # auction also stays unremoved until then, but is no longer listed.)
    if (ours["removed_at"] is None and ours["last_seen_at"] > record.sale_at
            and ours["sale_at"] > record.sale_at + AUCTION):
        result["relisted_not_sold"] = not record.sold
        return result
    # Sold during its auction, or before it but not before we last saw it listed; then at
    # the Buy It Now price we last saw, unless an offer won.
    presale = record.sale_at < ours["sale_at"] - HOUR
    result["sale_time"] = (record.sale_at >= ours["last_seen_at"] - HOUR if presale
                           else record.sale_at <= ours["sale_at"] + AUCTION)
    if presale:
        result["presale_at_buy_now"] = _same(price, buy_now)
    return result


def _status(record: Record) -> bool | None:
    return None if record.status is None else record.sold


def cross(a: list[Record], b: list[Record], sold_only: bool = False) -> dict[str, float | None]:
    """How far two APIs agree on one slice: the lots they both list, then whether each sold, at
    what price, and VINs. `sold_only`: only the lots either says sold (one API may leave the
    unsold lots out)."""
    by_lot_a, by_lot_b = {r.lot_number: r for r in a}, {r.lot_number: r for r in b}
    lots = by_lot_a.keys() | by_lot_b.keys()
    if sold_only:
        lots = {n for n in lots if any(n in by and by[n].sold for by in (by_lot_a, by_lot_b))}
    both = lots & by_lot_a.keys() & by_lot_b.keys()
    return {"overlap": share(len(both), len(lots)),
            **shares({"same_status": _same(_status(by_lot_a[n]), _status(by_lot_b[n])),
                      "same_price": _same(by_lot_a[n].final_price, by_lot_b[n].final_price),
                      "same_vin": _same(by_lot_a[n].vin, by_lot_b[n].vin)} for n in both)}


def manual_matches(rows: Iterable[Mapping[str, str]],
                   records: Mapping[tuple[str, str], Record]) -> dict[str, Any]:
    """The final prices a person read on the auction's site against the API's."""
    results = []
    for row in rows:
        seen = (row.get("final_price") or "").replace("$", "").replace(",", "").strip()
        record = records.get((row["source"], row["lot_number"]))
        if seen and record:
            results.append({"same_price": record.final_price == float(seen)})
    return {"checked": len(results), **shares(results)}


def manual_template(apis: Mapping[str, Mapping[str, list[Record]]],
                    suspects: set[tuple[str, str]]) -> list[dict[str, Any]]:
    """Up to MANUAL_LOTS sold lots per auction to look up: those with a doubtful price first,
    then those most APIs list."""
    listed: dict[tuple[str, str], list[Record]] = {}
    for by_slice in apis.values():
        for source in SOURCES:
            for r in by_slice[f"{source}-sold"]:
                if r.sold:
                    listed.setdefault((source, r.lot_number), []).append(r)
    rows = []
    for source in SOURCES:
        keys = sorted((k for k in listed if k[0] == source),
                      key=lambda k: (k not in suspects, -len(listed[k]), k))
        for key in keys[:MANUAL_LOTS]:
            r = listed[key][0]
            rows.append({"source": source, "lot_number": r.lot_number, "vin": r.vin,
                         "year": r.year, "make": r.make, "model": r.model,
                         "url": listing_url(r.lot_number) if source == "copart" else "",
                         "final_price": ""})
    return rows


MISMATCH_COLUMNS = ("api", "slice", "problem", "lot_number", "vin", "api_price", "api_sale_at",
                    "our_high_bid", "our_buy_now", "our_sale_at", "our_last_seen_at")


def _mismatch(api: str, s: Slice, record: Record | None, ours: Mapping[str, Any] | None,
              problem: str) -> dict[str, Any]:
    return {"api": api, "slice": s.name, "problem": problem,
            "lot_number": record.lot_number if record else ours["lot_number"],
            "vin": record.vin if record else ours["vin"],
            "api_price": record and record.final_price, "api_sale_at": record and record.sale_at,
            "our_high_bid": ours and ours["high_bid"],
            "our_buy_now": ours and ours["buy_it_now_price"],
            "our_sale_at": ours and ours["sale_at"],
            "our_last_seen_at": ours and ours["last_seen_at"]}


def _write_csv(path: Path, rows: list[dict[str, Any]], header: Iterable[str]) -> None:
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(header))
        writer.writeheader()
        writer.writerows(rows)


def report(conn: Connection, run_dir: Path) -> dict[str, Any]:
    """Compares the saved pages with our lots, each other and `manual.csv`; writes the files."""
    run = _run(run_dir)
    apis = {api: load(run_dir, api) for api in APIS if (run_dir / api).is_dir()}
    listed = sorted({r.lot_number for by_slice in apis.values()
                     for kind in KINDS for r in by_slice[f"copart-{kind}"]})
    days = [date.fromisoformat(run[f"{kind}_date"]) for kind in KINDS]
    ours = {row["lot_number"]: row
            for row in conn.execute(OURS, (listed, run["make"], days)).fetchall()}

    summary: dict[str, Any] = {"run": run, "apis": {}, "cross": {}, "manual": {}}
    mismatches: list[dict[str, Any]] = []
    suspects: set[tuple[str, str]] = set()  # lots whose final price is doubtful
    for api, by_slice in apis.items():
        summary["apis"][api] = {}
        for s in slices(run):
            records = by_slice[s.name]
            entry: dict[str, Any] = {
                "lots": len(records),
                "statuses": dict(Counter(str(r.status) for r in records)),
                "fields": {f: share(sum(getattr(r, f) is not None for r in records), len(records))
                           for f in FIELDS}}
            if s.kind == "sold":
                sold = [r for r in records if r.sold]
                entry["price_present"] = share(sum(r.final_price is not None for r in sold),
                                               len(sold))
                # No lot sells below the API's own last bid for it.
                with_bid = [r for r in sold if r.final_price and r.current_bid]
                below = [r for r in with_bid if r.final_price < r.current_bid]
                entry["price_at_least_bid"] = share(len(with_bid) - len(below), len(with_bid))
                suspects |= {(r.source, r.lot_number) for r in below}
                mismatches += [_mismatch(api, s, r, None, "price_below_its_bid") for r in below]
            if s.source == "copart":
                expected = {n for n, row in ours.items()
                            if row["make"] == s.make and row["day"] == s.day and row[s.kind]}
                results = []
                for r in records:
                    if r.lot_number not in ours:
                        mismatches.append(_mismatch(api, s, r, None, "not_in_ours"))
                        continue
                    result = check(r, ours[r.lot_number], s.kind)
                    results.append(result)
                    if result.get("price_at_least_prebid") is False:
                        suspects.add((r.source, r.lot_number))
                    if failed := [name for name, ok in result.items() if ok is False]:
                        mismatches.append(_mismatch(api, s, r, ours[r.lot_number],
                                                    ",".join(failed)))
                missing = expected - {r.lot_number for r in records}
                mismatches += [_mismatch(api, s, None, ours[n], "missing_from_api")
                               for n in sorted(missing)]
                entry |= {"ours": len(expected),
                          "ours_in_api": share(len(expected) - len(missing), len(expected)),
                          "api_in_ours": share(len(results), len(records)),
                          "checks": shares(results)}
            summary["apis"][api][s.name] = entry

    if len(apis) == 2:
        a, b = apis.values()
        summary["cross"] = {s.name: cross(a[s.name], b[s.name], s.kind == "sold")
                            for s in slices(run)}
        for s in slices(run):
            other = {r.lot_number: r for r in b[s.name]}
            for r in a[s.name]:
                if (o := other.get(r.lot_number)) is None:
                    continue
                if r.final_price and o.final_price not in (None, r.final_price):
                    problem = f"other_api_price:{o.final_price:g}"
                elif None not in (r.status, o.status) and r.sold != o.sold:
                    problem = f"other_api_status:{o.status}"
                else:
                    continue
                suspects.add((r.source, r.lot_number))
                mismatches.append(_mismatch(" vs ".join(apis), s, r, None, problem))

    manual = run_dir / "manual.csv"
    if not manual.exists():
        _write_csv(manual, manual_template(apis, suspects),
                   ("source", "lot_number", "vin", "year", "make", "model", "url", "final_price"))
    with manual.open(newline="") as f:
        rows = list(csv.DictReader(f))
    for api, by_slice in apis.items():
        by_lot = {(r.source, r.lot_number): r for source in SOURCES
                  for r in by_slice[f"{source}-sold"]}
        summary["manual"][api] = manual_matches(rows, by_lot)

    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    _write_csv(run_dir / "mismatches.csv", mismatches, MISMATCH_COLUMNS)
    return summary


def _line(name: str, value: float | None) -> str:
    bar = PASS.get(name)
    verdict = ("" if bar is None or value is None
               else " ok" if value >= bar else f" BELOW {bar:.0%}")
    return f"    {name}: {'n/a' if value is None else f'{value:.1%}'}{verdict}"


def render(summary: Mapping[str, Any]) -> str:
    lines = []
    for api, by_slice in summary["apis"].items():
        lines.append(api)
        for name, entry in by_slice.items():
            ours = f", {entry['ours']} of ours expected" if "ours" in entry else ""
            lines.append(f"  {name}: {entry['lots']} lots{ours}")
            lines += [_line(key, entry[key]) for key in ("ours_in_api", "api_in_ours",
                                                         "price_present", "price_at_least_bid")
                      if key in entry]
            lines += [_line(key, value) for key, value in entry.get("checks", {}).items()]
            lines.append("    fields: " + ", ".join(f"{f} {v:.0%}" for f, v
                                                    in entry["fields"].items() if v is not None))
            lines.append("    statuses: " + ", ".join(f"{s} {n}" for s, n
                                                      in entry["statuses"].items()))
    for name, entry in summary["cross"].items():
        lines.append(f"apibara vs apiauctions, {name}")
        lines += [_line(key, value) for key, value in entry.items()]
    for api, entry in summary["manual"].items():
        lines.append(f"manual spot check, {api}: {entry['checked']} lots")
        lines += [_line(key, value) for key, value in entry.items() if key != "checked"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m ingest.api_check")
    commands = parser.add_subparsers(dest="command", required=True)
    get = commands.add_parser("fetch", help="save the run's pages from every API with a key")
    get.add_argument("--sold-date", type=date.fromisoformat, required=True)
    get.add_argument("--active-date", type=date.fromisoformat, required=True)
    get.add_argument("--make", type=str.upper, required=True)
    get.add_argument("--dir", type=Path, default=Path("/data/checks"))
    get.add_argument("--max-requests", type=int, default=35, help="per API")
    compare = commands.add_parser("report", help="compare the saved pages (no requests)")
    compare.add_argument("run_dir", type=Path)
    args = parser.parse_args(argv)

    if args.command == "report":
        from .db import connect

        with connect() as conn:
            print(render(report(conn, args.run_dir)))
        print(f"details in {args.run_dir}: summary.json, mismatches.csv, manual.csv")
        return
    run_dir = args.dir / f"{args.sold_date}_{args.active_date}_{args.make}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps(
        {"sold_date": str(args.sold_date), "active_date": str(args.active_date),
         "make": args.make}))
    settings = get_settings()
    for api, spec in APIS.items():
        key = getattr(settings, spec.key_setting)
        if not key:
            print(f"{api}: skipped, {spec.key_setting.upper()} is not set")
            continue
        try:
            print(f"{api}: {fetch(api, key, run_dir, args.max_requests)} requests")
        except OSError as error:  # HTTP errors included; the pages saved so far are kept
            print(f"{api}: stopped: {error}")
    print(run_dir)


if __name__ == "__main__":
    main()
