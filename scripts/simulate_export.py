#!/usr/bin/env python3
"""Drop a simulated "next hour" export into the inbox, derived from a real one.

It changes the high bid of some lots, drops some (sold), adds new lots, and writes the file
atomically (temp name + rename), exactly like a well-behaved downloader should.

    python scripts/simulate_export.py                       # salesdata.csv -> data/inbox/copart/
    python scripts/simulate_export.py --changed 500 --removed 1000 --added 200
    python scripts/simulate_export.py --add-column "Seller Rating" --drop-column Trim

Standard library only, so it runs without the backend's virtualenv.
"""

from __future__ import annotations

import argparse
import csv
import os
import random
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--source", type=Path, default=ROOT / "salesdata.csv")
    ap.add_argument("--inbox", type=Path, default=ROOT / "data" / "inbox" / "copart")
    ap.add_argument("--changed", type=int, default=500, help="lots whose high bid changes")
    ap.add_argument("--removed", type=int, default=1000, help="lots left out (sold/withdrawn)")
    ap.add_argument("--added", type=int, default=200, help="new lots")
    ap.add_argument("--add-column", action="append", default=[], help="extra column to add")
    ap.add_argument("--drop-column", action="append", default=[], help="column to leave out")
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()
    rng = random.Random(args.seed)

    with open(args.source, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        rows = [r for r in reader if len(r) == len(header)]

    col = {name: i for i, name in enumerate(header)}
    lot_i, bid_i = col["Lot number"], col["High Bid =non-vix,Sealed=Vix"]
    upd_i = col.get("Last Updated Time")
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    rng.shuffle(rows)
    rows = rows[args.removed:]
    for row in rows[: args.changed]:
        row[bid_i] = f"{float(row[bid_i] or 0) + rng.choice([25, 50, 100, 250]):.1f}"
        if upd_i is not None:
            row[upd_i] = now
    max_lot = max(int(r[lot_i]) for r in rows)
    for n, template in enumerate(rng.sample(rows, args.added), start=1):
        new = list(template)
        new[lot_i] = str(max_lot + n)
        rows.append(new)
    rows.sort(key=lambda r: int(r[col["Id"]]) if r[col["Id"]].isdigit() else 0)
    for i, row in enumerate(rows, start=1):
        row[col["Id"]] = str(i)

    keep = [i for i, name in enumerate(header) if name not in set(args.drop_column)]
    out_header = [header[i] for i in keep] + args.add_column

    args.inbox.mkdir(parents=True, exist_ok=True)
    target = args.inbox / f"salesdata_{datetime.now(UTC):%Y%m%dT%H%M%S}.csv"
    tmp = target.with_suffix(".csv.part")
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, quoting=csv.QUOTE_ALL)
        writer.writerow(out_header)
        for row in rows:
            writer.writerow([row[i] for i in keep] + [str(rng.randint(1, 5)) for _ in args.add_column])
    os.replace(tmp, target)
    print(f"wrote {target} ({len(rows)} rows: {args.changed} changed, "
          f"{args.removed} removed, {args.added} added)")


if __name__ == "__main__":
    main()
