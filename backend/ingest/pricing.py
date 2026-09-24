"""Predicted final auction price of each lot: an XGBoost model over the lot's own columns.

No sold prices exist yet, so `synth` writes a training file from real lots with a generated
`final_bid_sale`, and `train` fits the model on it; real sale prices later go into the same file
format. The worker `refresh`es the prediction of every active lot whose columns or model changed
since it was last predicted, and api_sync serves `predicted_final_price` with the other columns.

    docker compose run --rm worker python -m ingest.pricing synth   # up to 300k lots
    docker compose run --rm worker python -m ingest.pricing train /data/training/lots.csv.gz
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb
from psycopg import Connection

from .loader import NEXT_SYNC_VERSION

# Only the columns that move a sale price: no identifiers, URLs, yard or address columns.
# `sale_date` only serves to derive the vehicle's age at sale.
NUMERIC = ("year", "odometer", "est_retail_value", "repair_cost", "high_bid", "buy_it_now_price")
CATEGORICAL = ("make", "model_group", "body_style", "vehicle_type", "damage_primary",
               "damage_secondary", "title_type", "has_keys", "runs_drives", "drive", "fuel_type",
               "transmission", "cylinders", "odometer_brand", "location_state")
COLUMNS = (*NUMERIC, "sale_date", *CATEGORICAL)
TARGET = "final_bid_sale"
# Rarer values (odd makes and models) count as missing: every categorical split lists its
# categories, so keeping all of them made a 240 MB model for no gain in accuracy.
MIN_CATEGORY_ROWS = 50
PARAMS = {"objective": "reg:squarederror", "tree_method": "hist", "eta": 0.05, "max_depth": 8,
          "subsample": 0.8, "colsample_bytree": 0.8, "max_cat_to_onehot": 8}


def features(df: pd.DataFrame, categories: dict[str, list[str]] | None = None) -> pd.DataFrame:
    """The model's input. `categories` are the training ones: other values become missing."""
    x = pd.DataFrame({c: df[c].astype("float64") for c in NUMERIC}, index=df.index)
    x["age"] = pd.to_datetime(df["sale_date"]).dt.year - x["year"]
    for c in CATEGORICAL:
        s = df[c].astype("string")
        known = categories[c] if categories else sorted(s.dropna().unique())
        x[c] = pd.Categorical(s.where(s.isin(known)), categories=known)
    return x


# ponytail: a made-up sold price, only to build and test the pipeline; replace with real sales.
_DAMAGE = {"MINOR DENT/SCRATCHES": 0.75, "NORMAL WEAR": 0.8, "HAIL": 0.65, "REAR END": 0.55,
           "SIDE": 0.5, "VANDALISM": 0.55, "FRONT END": 0.45, "MECHANICAL": 0.4,
           "UNDERCARRIAGE": 0.4, "ALL OVER": 0.3, "ROLLOVER": 0.25, "WATER/FLOOD": 0.25,
           "BURN": 0.15}
_RUNS = {"Run & Drive Verified": 1.15, "Vehicle Starts": 1.0}
_TITLE = {"CT": 1.15, "NR": 0.6, "BS": 0.6, "DL": 0.6}  # clean; non-repairable, bill of sale


def synthesize(df: pd.DataFrame, seed: int = 0) -> np.ndarray:
    """A plausible sold price per lot: retail value discounted for damage, title, condition,
    age and mileage, less part of the repair cost, with noise; never under the current bid."""
    rng = np.random.default_rng(seed)
    x = features(df)
    retail = x["est_retail_value"].where(x["est_retail_value"] > 0, 4000.0)
    factor = (df["damage_primary"].map(_DAMAGE).fillna(0.45).astype("float64")
              * df["runs_drives"].map(_RUNS).fillna(0.85).astype("float64")
              * df["title_type"].map(_TITLE).fillna(1.0).astype("float64")
              * np.where(df["has_keys"] == "YES", 1.0, 0.85)
              * 0.97 ** x["age"].clip(0, 30).fillna(10)
              * (1 - (x["odometer"].fillna(100_000) / 300_000).clip(0, 0.5)))
    price = (retail * factor - 0.15 * x["repair_cost"].fillna(0)) * rng.lognormal(0, 0.15, len(df))
    floor = np.maximum(x["high_bid"].fillna(0), 100)
    return (np.maximum(price, floor) / 25).round().to_numpy() * 25  # bids move by $25


def train(df: pd.DataFrame, seed: int = 0) -> tuple[xgb.Booster, dict[str, float]]:
    """Fit on 70 % of `df`, stop early on 15 %, and report on the last 15 % against a naive
    baseline (the training set's median price/retail ratio times the retail value)."""
    categories = {}
    for c in CATEGORICAL:
        counts = df[c].astype("string").value_counts()  # most frequent first
        # XGBoost refuses a categorical feature without categories: keep at least one.
        categories[c] = (sorted(counts.index[counts >= MIN_CATEGORY_ROWS])
                         or list(counts.index[:1]) or ["?"])
    x, y = features(df, categories), df[TARGET].astype("float64").to_numpy()
    order = np.random.default_rng(seed).permutation(len(df))
    fit, val, test = np.split(order, [int(len(df) * 0.7), int(len(df) * 0.85)])

    def matrix(rows):
        return xgb.DMatrix(x.iloc[rows], np.log1p(y[rows]), enable_categorical=True)

    booster = xgb.train({**PARAMS, "seed": seed}, matrix(fit), 2000,
                        evals=[(matrix(val), "val")], early_stopping_rounds=50,
                        verbose_eval=False)
    booster = booster[: booster.best_iteration + 1]
    booster.set_attr(categories=json.dumps(categories))

    actual, predicted = y[test], predict(booster, df.iloc[test])
    retail = x["est_retail_value"].to_numpy()
    usable = retail[fit] > 0
    baseline = np.median(y[fit][usable] / retail[fit][usable]) * retail[test]
    metrics = {
        "rows_train": len(fit), "rows_test": len(test), "trees": booster.num_boosted_rounds(),
        "mae": float(np.mean(np.abs(predicted - actual))),
        "mape": float(np.mean(np.abs(predicted - actual) / actual)),
        "r2": float(1 - np.sum((predicted - actual) ** 2) / np.sum((actual - actual.mean()) ** 2)),
        "baseline_mae": float(np.mean(np.abs(baseline - actual))),
    }
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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m ingest.pricing")
    commands = parser.add_subparsers(dest="command", required=True)
    synth = commands.add_parser("synth", help="write a training file with generated sold prices")
    synth.add_argument("--out", type=Path, default=Path("/data/training/lots.csv.gz"))
    synth.add_argument("--limit", type=int, default=300_000)
    synth.add_argument("--seed", type=int, default=0)
    fit = commands.add_parser("train", help="fit the model on a training file")
    fit.add_argument("data", type=Path)
    fit.add_argument("--out", type=Path, default=Path("/data/models/final_price.ubj"))
    args = parser.parse_args(argv)

    if args.command == "synth":
        from .db import connect

        with connect() as conn:  # the most recent lots, removed ones included
            rows = conn.execute(f"""SELECT {', '.join(COLUMNS)} FROM lots
                                    ORDER BY first_seen_at DESC LIMIT %s""",
                                (args.limit,)).fetchall()
        df = pd.DataFrame(rows, columns=list(COLUMNS))
        df[TARGET] = synthesize(df, args.seed)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(args.out, index=False)
        print(f"{len(df)} lots written to {args.out}")
    else:
        df = pd.read_csv(args.data, dtype={c: "string" for c in CATEGORICAL})
        booster, metrics = train(df)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        booster.save_model(args.out)
        print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
