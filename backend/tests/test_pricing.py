"""The final price model: generated sold prices, training, and pricing the lots."""

import json

import numpy as np
import pandas as pd
import pytest
import xgboost as xgb

from ingest import db, pricing
from ingest.pipeline import process_file
from ingest.providers import COPART

from .conftest import read_sample
from .test_pipeline import col


def lot(**values):
    row = dict.fromkeys(pricing.COLUMNS)
    row.update(year=2018, sale_date="2026-09-18", odometer=60_000.0, est_retail_value=20_000.0,
               repair_cost=5_000.0, high_bid=0.0, buy_it_now_price=0.0, make="TOYOTA",
               model_group="CAMRY", damage_primary="FRONT END", title_type="SC",
               has_keys="YES", runs_drives="Run & Drive Verified")
    return {**row, **values}


def ingest(conn, settings, make_export, rows=None):
    assert process_file(conn, make_export(rows), COPART, settings).status == "succeeded"


def training_frame(conn, copies=7):
    """The ingested lots, copied with jittered retail value and mileage, with sold prices."""
    lots = pd.DataFrame(conn.execute(
        f"SELECT {', '.join(pricing.COLUMNS)} FROM lots").fetchall())
    rng = np.random.default_rng(0)
    frames = []
    for _ in range(copies):
        c = lots.copy()
        for name in ("est_retail_value", "odometer"):
            c[name] = c[name].astype("float64") * rng.uniform(0.7, 1.3, len(c))
        frames.append(c)
    df = pd.concat(frames, ignore_index=True)
    df[pricing.TARGET] = pricing.synthesize(df)
    return df


def save_model(conn, path, **attrs):
    booster, _ = pricing.train(training_frame(conn, copies=2))
    booster.set_attr(**attrs)
    path.parent.mkdir(parents=True, exist_ok=True)
    booster.save_model(path)
    return path


def test_synthesized_prices_are_reproducible_and_follow_the_retail_value():
    df = pd.DataFrame([lot()] * 200 + [lot(est_retail_value=40_000.0)] * 200)

    prices = pricing.synthesize(df, seed=1)

    assert (prices == pricing.synthesize(df, seed=1)).all()
    assert (prices != pricing.synthesize(df, seed=2)).any()
    assert prices[200:].mean() > prices[:200].mean() * 1.5
    assert (prices % 25 == 0).all()


def test_synthesized_prices_never_go_under_the_bid_or_100():
    df = pd.DataFrame([lot(high_bid=90_000.0), dict.fromkeys(pricing.COLUMNS),
                       lot(est_retail_value=500.0, repair_cost=50_000.0)])

    prices = pricing.synthesize(df)

    assert prices[0] == 90_000
    assert prices[1] >= 100  # nothing known about the lot
    assert prices[2] == 100


def test_the_model_beats_the_baseline_and_survives_a_round_trip(
        conn, settings, make_export, tmp_path):
    ingest(conn, settings, make_export)
    df = training_frame(conn)
    df["damage_secondary"] = None  # a column without any value still trains

    booster, metrics = pricing.train(df)

    categories = json.loads(booster.attr("categories"))
    assert categories["damage_secondary"] == ["?"]
    assert all(0 < len(v) <= len(df) / pricing.MIN_CATEGORY_ROWS for v in categories.values())
    assert metrics["rows_train"] + metrics["rows_test"] == int(len(df) * 0.85)
    assert metrics["mae"] < metrics["baseline_mae"]
    assert 0 < metrics["mape"] < 1 and metrics["r2"] > 0
    path = tmp_path / "model.ubj"
    booster.save_model(path)
    loaded = xgb.Booster(model_file=str(path))
    np.testing.assert_allclose(pricing.predict(loaded, df), pricing.predict(booster, df),
                               rtol=1e-6)
    # Values never seen in training, and a lot with nothing known, are priced all the same.
    unseen = pd.DataFrame([lot(make="NEVER SEEN", damage_primary="METEOR"),
                           dict.fromkeys(pricing.COLUMNS)])
    assert (pricing.predict(loaded, unseen) > 0).all()


def test_refresh_prices_each_active_lot_once_per_version_and_model(
        conn, settings, make_export, tmp_path):
    model = tmp_path / "models" / "final_price.ubj"
    header, rows = read_sample()
    ingest(conn, settings, make_export, rows)
    assert pricing.refresh(conn, model) == 0  # no model yet: nothing priced
    save_model(conn, model)
    removed = rows[0][col(header, "Lot number")]
    conn.execute("UPDATE lots SET removed_at = now() WHERE lot_number = %s", (removed,))
    before = conn.execute("SELECT max(sync_version) AS v FROM lots").fetchone()["v"]

    assert pricing.refresh(conn, model, batch=100) == 299  # in three batches

    lots = {r["lot_number"]: r for r in conn.execute("SELECT * FROM lots")}
    assert lots.pop(removed)["predicted_final_price"] is None
    assert all(r["predicted_final_price"] > 0 for r in lots.values())
    assert all(r["predicted_version"] == r["sync_version"] > before for r in lots.values())
    assert len({r["prediction_model"] for r in lots.values()}) == 1
    assert pricing.refresh(conn, model) == 0  # nothing changed since

    changed = [list(r) for r in rows]
    changed[1][col(header, "High Bid =non-vix,Sealed=Vix")] = "999.0"
    ingest(conn, settings, make_export, changed)
    # Only the lot whose columns changed, and the removed one, listed again.
    assert pricing.refresh(conn, model) == 2
    assert conn.execute("SELECT predicted_final_price FROM lots WHERE lot_number = %s",
                        (removed,)).fetchone()["predicted_final_price"] > 0

    save_model(conn, model, note="retrained")
    assert pricing.refresh(conn, model) == 300  # a new model prices every active lot again


def test_cli_writes_a_training_file_and_trains_on_it(
        conn, settings, make_export, tmp_path, monkeypatch, capsys):
    ingest(conn, settings, make_export)
    conn.execute("UPDATE lots SET removed_at = now() WHERE lot_number IN "
                 "(SELECT lot_number FROM lots LIMIT 100)")
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    data, model = tmp_path / "training" / "lots.csv.gz", tmp_path / "models" / "model.ubj"

    pricing.main(["synth", "--out", str(data), "--limit", "250"])
    df = pd.read_csv(data)
    assert list(df.columns) == [*pricing.COLUMNS, pricing.TARGET]  # no ids, VINs or URLs
    assert len(df) == 250  # removed lots included

    pricing.main(["synth", "--out", str(data)])
    assert len(pd.read_csv(data)) == 300

    capsys.readouterr()
    pricing.main(["train", str(data), "--out", str(model)])
    assert json.loads(capsys.readouterr().out)["rows_test"] == 45
    assert model.is_file()


def test_cli_needs_a_command():
    with pytest.raises(SystemExit):
        pricing.main([])
