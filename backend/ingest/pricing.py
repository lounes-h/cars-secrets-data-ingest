"""Predicted final auction price of each lot: an XGBoost model over the lot's own columns.

It learns from real sale prices (`lots.final_price`, loaded from apiauctions by
apiauctions_import.py), Copart's and IAA's alike. The two auctions word most columns differently
(`FRONT END` / `Left Front`, title code `SC` / `Salvage (Texas)`), so `normalize` puts both into
one vocabulary, the same way for training and for pricing our lots. `export` writes the cleaned
training file and, apart, a test file of our own exported lots with a price; `train` fits the
model and reports on both. The worker `refresh`es the prediction of every active lot whose
columns or model changed since it was last predicted, and api_sync serves `predicted_final_price`.

    docker compose run --rm worker python -m ingest.pricing export
    docker compose run --rm worker python -m ingest.pricing train /data/training/lots.csv.gz \
        --test /data/training/test.csv.gz
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb
from psycopg import Connection

from .loader import NEXT_SYNC_VERSION

# The lot columns the model reads (with `source`). Not repair cost, pre-auction bid, Buy It Now
# price or odometer brand: the sales we learn from never have them. No identifiers, URLs, yard
# or address. `sale_date` only serves to derive the vehicle's age at sale.
COLUMNS = ("year", "odometer", "est_retail_value", "sale_date", "make", "model_group",
           "body_style", "vehicle_type", "damage_primary", "damage_secondary", "title_type",
           "has_keys", "runs_drives", "drive", "fuel_type", "transmission", "cylinders",
           "location_state")
NUMERIC = ("year", "age", "odometer", "est_retail_value")
CATEGORICAL = ("source", "make", "model", "body", "vehicle_type", "damage_primary",
               "damage_secondary", "title", "keys", "runs", "drive", "fuel", "transmission",
               "cylinders", "location_state")
TARGET = "final_bid_sale"
# Rarer values (odd makes and models) count as missing: every categorical split lists its
# categories, so keeping all of them made a 240 MB model for no gain in accuracy.
MIN_CATEGORY_ROWS = 50
PARAMS = {"objective": "reg:squarederror", "tree_method": "hist", "eta": 0.05, "max_depth": 8,
          "subsample": 0.8, "colsample_bytree": 0.8, "max_cat_to_onehot": 8}

Rule = tuple[str, tuple[str, ...]]


def _words(*rules: Rule, others: bool = False) -> Callable[[str], str | None]:
    """The group of the first rule with a word in the text; else the text itself (`others`)
    or None."""
    def group(text: str) -> str | None:
        upper = " ".join(text.upper().split())
        found = next((name for name, words in rules if any(w in upper for w in words)), None)
        return found or (upper if others and upper not in ("UNKNOWN", "NONE", "OTHER") else None)
    return group


_damage = _words(
    ("BURN", ("BURN", "FIRE")), ("FLOOD", ("FLOOD", "WATER")), ("FRONT & REAR", ("FRONT & REAR",)),
    ("FRONT", ("FRONT",)), ("REAR", ("REAR",)), ("SIDE", ("SIDE",)), ("ROOF", ("ROOF",)),
    ("MECHANICAL", ("MECHANICAL", "ENGINE DAMAGE", "TRANSMISSION", "ELECTRICAL", "SUSPENSION")),
    ("HAIL", ("HAIL",)), ("ALL OVER", ("ALL OVER",)), ("ROLLOVER", ("ROLLOVER",)),
    ("VANDALISM", ("VANDAL",)), ("MINOR DENTS", ("MINOR DENT",)),
    ("NORMAL WEAR", ("NORMAL WEAR",)), ("STRIPPED", ("STRIP",)), ("BIOHAZARD", ("BIOHAZARD",)),
    ("FRAME", ("FRAME",)), ("VIN", ("VIN",)), ("UNDERCARRIAGE", ("UNDERCARRIAGE",)), others=True)
# Copart's title codes, grouped by the wording apiauctions shows for them (apiauctions_codes.json).
_TITLE_CODES = {
    **dict.fromkeys(("CT", "DV", "CZ", "DQ", "RP", "R1", "DA", "LQ", "PX"), "CLEAN"),
    **dict.fromkeys(("ST", "SC", "SD", "AC", "S1", "S2", "SM", "PS", "FV", "SV", "FS", "SQ"),
                    "SALVAGE"),
    **dict.fromkeys(("RS", "RT", "SH", "RB", "RC", "CR"), "REBUILT"),
    **dict.fromkeys(("BP", "NR", "CD", "JR", "NX", "NT", "PO", "NQ"), "NON-REPAIRABLE"),
    "BS": "BILL OF SALE"}
_title_words = _words(  # IAA: `Salvage (Texas)`, `Clear (Virginia)`, `Non-Repairable (Ohio)`
    ("NON-REPAIRABLE", ("NON-REPAIRABLE", "JUNK", "PARTS ONLY", "DESTRUCTION")),
    ("REBUILT", ("REBUILT", "RECONSTRUCTED")), ("SALVAGE", ("SALVAGE",)),
    ("BILL OF SALE", ("BILL OF SALE",)), ("CLEAN", ("CLEAR", "CLEAN")))
# Copart's vehicle type codes, and IAA's words for the same types.
_VEHICLE_TYPES = {
    "V": "AUTOMOBILE", "K": "MEDIUM TRUCK", "U": "HEAVY TRUCK", "C": "MOTORCYCLE", "R": "RV",
    "L": "TRAILER", "B": "BUS", "E": "EQUIPMENT", "N": "EQUIPMENT", "A": "ATV", "M": "BOAT",
    "AUTOMOBILE": "AUTOMOBILE", "PASSENGER CAR": "AUTOMOBILE", "MOTORCYCLE": "MOTORCYCLE",
    "TRUCK": "MEDIUM TRUCK", "MOTOR HOME": "RV", "TRAVEL TRAILER": "RV", "TRAILER": "TRAILER",
    "BOAT": "BOAT", "PERSONAL WATERCRAFT": "BOAT", "BUS": "BUS", "FARM EQUIPMENT": "EQUIPMENT"}
_RUNS = {"RUN & DRIVE VERIFIED": "RUNS", "RUN & DRIVE": "RUNS", "VEHICLE STARTS": "STARTS",
         "STARTS": "STARTS", "DEFAULT": "NOT VERIFIED", "STATIONARY": "NOT VERIFIED"}
_body = _words(
    ("TRUCK", ("CHASSIS", "TRACTOR", "STRAIGHT TRUCK", "CUTAWAY", "CONVENTIONAL CAB", "TILT CAB")),
    ("PICKUP", ("PICKUP", "PIC", " CAB")), ("TRAILER", ("TRAILER",)),
    ("SUV", ("SPORT UTILITY", "SUV", "4DR SPOR", "UTILITY")),
    ("VAN", ("VAN",)), ("SEDAN", ("SEDAN",)), ("HATCHBACK", ("HATCHBACK",)),
    ("WAGON", ("WAGON",)), ("COUPE", ("COUPE",)), ("CONVERTIBLE", ("CONVERTIBLE",)),
    ("RV", ("MOTORIZED HOME", "MOTOR HOME")), others=True)
_drive = _words(("4WD", ("4X4", "FOUR", "4WD")), ("AWD", ("ALL",)), ("FWD", ("FRONT",)),
                ("RWD", ("REAR",)), ("2WD", ("4X2",)))
_fuel = _words(("HYBRID", ("HYBRID",)), ("HYDROGEN", ("HYDROGEN",)), ("CNG", ("NATURAL GAS",)),
               ("ELECTRIC", ("ELECTRIC",)), ("FLEX", ("FLEX",)), ("DIESEL", ("DIESEL",)),
               ("GAS", ("GAS",)))
_transmission = _words(("AUTOMATIC", ("AUTO",)), ("MANUAL", ("MANUAL",)))


def _title(value: str) -> str | None:
    return _TITLE_CODES.get(value) or _title_words(value)


def _cylinders(value: str) -> str | None:
    """`4`, `4 Cyl` -> `4`; `0`, `All Others` -> None."""
    match = re.match(r"\s*(\d+)", value)
    return match.group(1) if match and int(match.group(1)) > 0 else None


def _model(value: str) -> str | None:
    """The model's first word: Copart's `CAMRY HYB` and IAA's `CAMRY HYBRID` are one model."""
    words = re.sub(r"[^A-Z0-9 ]", "", value.upper()).split()
    return words[0] if words else None


def _map(values: pd.Series, normalize: Callable[[str], str | None]) -> pd.Series:
    text = values.astype("string")
    return text.map({v: normalize(v) for v in text.dropna().unique()}).astype("string")


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """The categorical features in one vocabulary for Copart and IAA."""
    make = _map(df["make"], lambda v: " ".join(v.upper().split()) or None)
    return pd.DataFrame({
        "source": df["source"].astype("string"), "make": make,
        "model": make + " " + _map(df["model_group"], _model),
        "body": _map(df["body_style"], _body),
        "vehicle_type": _map(df["vehicle_type"],
                             lambda v: _VEHICLE_TYPES.get(v.upper(), v.upper())),
        "damage_primary": _map(df["damage_primary"], _damage),
        "damage_secondary": _map(df["damage_secondary"], _damage),
        "title": _map(df["title_type"], _title),
        "keys": _map(df["has_keys"], lambda v: v.upper() if v.upper() in ("YES", "NO") else None),
        "runs": _map(df["runs_drives"], lambda v: _RUNS.get(v.upper())),
        "drive": _map(df["drive"], _drive), "fuel": _map(df["fuel_type"], _fuel),
        "transmission": _map(df["transmission"], _transmission),
        "cylinders": _map(df["cylinders"], _cylinders),
        "location_state": _map(df["location_state"], lambda v: v.upper()),
    }, index=df.index)


def features(df: pd.DataFrame, categories: dict[str, list[str]] | None = None) -> pd.DataFrame:
    """The model's input. `categories` are the training ones: other values become missing. An
    odometer or retail value of 0 (or Copart's 999,999) is unknown, not a value."""
    x = pd.DataFrame(index=df.index)
    x["year"] = df["year"].astype("float64")
    x["age"] = pd.to_datetime(df["sale_date"]).dt.year - x["year"]
    odometer = df["odometer"].astype("float64")
    x["odometer"] = odometer.where((odometer > 0) & (odometer < 999_990))
    retail = df["est_retail_value"].astype("float64")
    x["est_retail_value"] = retail.where(retail > 0)
    categorical = normalize(df)
    for c in CATEGORICAL:
        s = categorical[c]
        known = categories[c] if categories else sorted(s.dropna().unique())
        x[c] = pd.Categorical(s.where(s.isin(known)), categories=known)
    return x


def evaluate(booster: xgb.Booster, df: pd.DataFrame, ratio: float) -> dict[str, float]:
    """Errors on `df`, and those of a naive baseline: `ratio` (price/retail) times retail."""
    actual, predicted = df[TARGET].astype("float64").to_numpy(), predict(booster, df)
    error = np.abs(predicted - actual)
    baseline = ratio * features(df)["est_retail_value"].to_numpy()
    spread = np.sum((actual - actual.mean()) ** 2)  # 0 when every price is the same: no R²
    return {
        "rows": len(df), "mae": float(error.mean()), "median_error": float(np.median(error)),
        "mape": float(np.mean(error / actual)),
        "r2": float(1 - np.sum(error ** 2) / spread) if spread else float("nan"),
        "baseline_mae": float(np.nanmean(np.abs(baseline - actual))),
    }


def train(df: pd.DataFrame, test: pd.DataFrame | None = None,
          seed: int = 0) -> tuple[xgb.Booster, dict[str, dict[str, float]]]:
    """Fit on 70 % of `df`, stop early on 15 %, report on the last 15 % and on `test` next to
    a naive baseline (the training set's median price/retail ratio times the retail value)."""
    categorical = normalize(df)
    categories = {}
    for c in CATEGORICAL:
        counts = categorical[c].value_counts()  # most frequent first
        # XGBoost refuses a categorical feature without categories: keep at least one.
        categories[c] = (sorted(counts.index[counts >= MIN_CATEGORY_ROWS])
                         or list(counts.index[:1]) or ["?"])
    x, y = features(df, categories), df[TARGET].astype("float64").to_numpy()
    order = np.random.default_rng(seed).permutation(len(df))
    fit, val, held = np.split(order, [int(len(df) * 0.7), int(len(df) * 0.85)])

    def matrix(rows):
        return xgb.DMatrix(x.iloc[rows], np.log1p(y[rows]), enable_categorical=True)

    booster = xgb.train({**PARAMS, "seed": seed}, matrix(fit), 2000,
                        evals=[(matrix(val), "val")], early_stopping_rounds=50,
                        verbose_eval=False)
    booster = booster[: booster.best_iteration + 1]
    booster.set_attr(categories=json.dumps(categories))

    retail = x["est_retail_value"].to_numpy()[fit]
    usable = retail > 0
    ratio = float(np.median(y[fit][usable] / retail[usable]))
    metrics = {"model": {"rows_train": len(fit), "trees": booster.num_boosted_rounds()},
               "holdout": evaluate(booster, df.iloc[held], ratio)}
    if test is not None:
        metrics["test"] = evaluate(booster, test, ratio)
    return booster, metrics


def predict(booster: xgb.Booster, df: pd.DataFrame) -> np.ndarray:
    x = features(df, json.loads(booster.attr("categories")))
    return np.expm1(booster.inplace_predict(x))


def refresh(conn: Connection, model_path: Path, batch: int = 20_000) -> int:
    """Predict the active lots not yet predicted by this model at their current version.
    Returns how many were predicted; 0 without a model file. `conn` is in autocommit mode."""
    if not model_path.is_file():
        return 0
    data = model_path.read_bytes()
    model = hashlib.sha256(data).hexdigest()[:12]  # a new model file re-predicts every lot
    booster = xgb.Booster(model_file=bytearray(data))
    total = 0
    while True:
        with conn.transaction():
            # The loader's lock: sync versions must commit in the order they are drawn.
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('ingest:lots'))")
            rows = conn.execute(
                f"""SELECT source, lot_number, {', '.join(COLUMNS)} FROM lots
                    WHERE removed_at IS NULL AND (prediction_model IS DISTINCT FROM %s
                          OR predicted_version IS DISTINCT FROM sync_version)
                    LIMIT %s""", (model, batch)).fetchall()
            if not rows:
                return total
            df = pd.DataFrame(rows)
            conn.execute("""CREATE TEMP TABLE prediction (source text, lot_number text,
                                                          price numeric) ON COMMIT DROP""")
            with conn.cursor().copy("COPY prediction FROM STDIN") as copy:
                for source, lot, price in zip(df["source"], df["lot_number"],
                                              predict(booster, df).round(), strict=True):
                    copy.write_row((source, lot, float(price)))
            # One new version for the batch, remembered as the one it was predicted at.
            conn.execute(
                f"""UPDATE lots l SET predicted_final_price = p.price, prediction_model = %s,
                                      sync_version = v.v, predicted_version = v.v
                    FROM prediction p, (SELECT {NEXT_SYNC_VERSION} AS v) v
                    WHERE l.source = p.source AND l.lot_number = p.lot_number""", (model,))
        total += len(rows)


# The priced lots worth learning from: at least Copart's $25 minimum bid, a possible model
# year, not a lot our export lists again after its sale (the sale fell through), and one sale
# per car (its latest). Our own exported lots are the test set: never trained on.
EXPORT = f"""
    WITH priced AS (
        SELECT source, last_file_id IS NOT NULL AS ours, vin, final_price AS {TARGET},
               {', '.join(COLUMNS)},
               row_number() OVER (PARTITION BY vin ORDER BY sale_at DESC) AS nth
        FROM lots
        WHERE final_price >= 25 AND source = ANY(%(sources)s)
          AND year BETWEEN 1950 AND extract(year FROM sale_date) + 1
          AND NOT (removed_at IS NULL AND last_file_id IS NOT NULL
                   AND last_seen_at >= (SELECT max(last_seen_at) FROM lots
                                        WHERE last_file_id IS NOT NULL) - interval '2 hours'))
    SELECT source, ours, {', '.join(COLUMNS)}, {TARGET} FROM priced
    WHERE vin IS NULL OR nth = 1
"""


def export(conn: Connection, sources: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(training rows, test rows) of the priced lots of these auctions."""
    df = pd.DataFrame(conn.execute(EXPORT, {"sources": sources}).fetchall(),
                      columns=["source", "ours", *COLUMNS, TARGET])
    return (df[~df["ours"]].drop(columns="ours").reset_index(drop=True),
            df[df["ours"]].drop(columns="ours").reset_index(drop=True))


def _read(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype={c: "string" for c in ("source", *COLUMNS)
                                    if c not in ("year", "odometer", "est_retail_value")})


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m ingest.pricing")
    commands = parser.add_subparsers(dest="command", required=True)
    out = commands.add_parser("export", help="write the training and test files")
    out.add_argument("--out", type=Path, default=Path("/data/training/lots.csv.gz"))
    out.add_argument("--test", type=Path, default=Path("/data/training/test.csv.gz"))
    out.add_argument("--sources", default="copart,iaai", help="auctions to learn from")
    fit = commands.add_parser("train", help="fit the model on a training file")
    fit.add_argument("data", type=Path)
    fit.add_argument("--test", type=Path, help="our own lots, never trained on")
    fit.add_argument("--out", type=Path, default=Path("/data/models/final_price.ubj"))
    args = parser.parse_args(argv)

    if args.command == "export":
        from .db import connect

        with connect() as conn:
            training, test = export(conn, args.sources.split(","))
        for df, path in ((training, args.out), (test, args.test)):
            path.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(path, index=False)
            print(f"{len(df)} lots written to {path}")
    else:
        booster, metrics = train(_read(args.data), _read(args.test) if args.test else None)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        booster.save_model(args.out)
        print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
