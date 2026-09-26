"""The final price model: one vocabulary for Copart and IAA, training, pricing the lots."""

import json
import math
from datetime import UTC, date, datetime, timedelta

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
    row = dict.fromkeys(("source", *pricing.COLUMNS))
    row.update(source="copart", year=2018, sale_date="2026-09-18", odometer=60_000.0,
               est_retail_value=20_000.0, make="TOYOTA", model_group="CAMRY",
               damage_primary="FRONT END", title_type="SC", has_keys="YES",
               runs_drives="Run & Drive Verified")
    return {**row, **values}


def ingest(conn, settings, make_export, rows=None):
    assert process_file(conn, make_export(rows), COPART, settings).status == "succeeded"


def test_copart_and_iaa_wording_land_on_the_same_values():
    copart = lot(make="Toyota ", model_group="CAMRY HYB", body_style="4DR SPORT UTILITY",
                 vehicle_type="V", damage_primary="FRONT END", damage_secondary="WATER/FLOOD",
                 title_type="SC", has_keys="YES", runs_drives="DEFAULT",
                 drive="4X4 W/REAR WHEEL DRV", fuel_type="ELECTRIC AND GAS HYBRID",
                 transmission="AUTOMATIC", cylinders="4", location_state="tx")
    iaa = lot(source="iaai", make="TOYOTA", model_group="Camry Hybrid", body_style="Sport Utility",
              vehicle_type="AUTOMOBILE", damage_primary="Left Front", damage_secondary="Flood",
              title_type="Salvage (Texas)", has_keys="yes", runs_drives="Stationary",
              drive="Four Wheel Drive", fuel_type="Hybrid", transmission="Automatic",
              cylinders="4 Cyl", location_state="TX")
    both = pricing.normalize(pd.DataFrame([copart, iaa]))
    features = both.drop(columns="source")
    assert features.iloc[0].to_dict() == features.iloc[1].to_dict() == {
        "make": "TOYOTA", "model": "TOYOTA CAMRY", "body": "SUV", "vehicle_type": "AUTOMOBILE",
        "damage_primary": "FRONT", "damage_secondary": "FLOOD", "title": "SALVAGE",
        "keys": "YES", "runs": "NOT VERIFIED", "drive": "4WD", "fuel": "HYBRID",
        "transmission": "AUTOMATIC", "cylinders": "4", "location_state": "TX"}
    assert list(both["source"]) == ["copart", "iaai"]


@pytest.mark.parametrize(("column", "feature", "value", "expected"), [
    ("damage_primary", "damage_primary", "Engine Burn", "BURN"),
    ("damage_primary", "damage_primary", "Front & Rear", "FRONT & REAR"),
    ("damage_primary", "damage_primary", "Theft", "THEFT"),  # its own value
    ("damage_primary", "damage_primary", "Unknown", None),
    ("title_type", "title", "CT", "CLEAN"), ("title_type", "title", "Clear (Virginia)", "CLEAN"),
    ("title_type", "title", "NR", "NON-REPAIRABLE"),
    ("title_type", "title", "Non-Repairable (Ohio)", "NON-REPAIRABLE"),
    ("title_type", "title", "Bill Of Sale (Georgia)", "BILL OF SALE"),
    ("title_type", "title", "DL", None),  # lien papers: no group
    ("vehicle_type", "vehicle_type", "MOTOR HOME", "RV"),
    ("vehicle_type", "vehicle_type", "J", "J"),  # a code we cannot name
    ("body_style", "body", "Crew Cab", "PICKUP"), ("body_style", "body", "CAB CHASSIS", "TRUCK"),
    ("body_style", "body", "UTILITY TRAILER", "TRAILER"),
    ("body_style", "body", "SPORT", "SPORT"),  # a motorcycle's
    ("drive", "drive", "4X2 Drive", "2WD"), ("drive", "drive", "UNKNOWN", None),
    ("fuel_type", "fuel", "COMPRESSED NATURAL GAS", "CNG"), ("fuel_type", "fuel", "Other", None),
    ("transmission", "transmission", "NONE", None),
    ("cylinders", "cylinders", "0", None), ("cylinders", "cylinders", "All Others", None),
    ("has_keys", "keys", "EXM", None), ("model_group", "model", "CR-V", "TOYOTA CRV"),
    ("model_group", "model", "--", None),
])
def test_each_wording_has_its_value(column, feature, value, expected):
    got = pricing.normalize(pd.DataFrame([lot(**{column: value})]))[feature].iloc[0]
    assert (None if pd.isna(got) else got) == expected


def test_zero_or_placeholder_numbers_are_unknown():
    x = pricing.features(pd.DataFrame([
        lot(odometer=0.0, est_retail_value=0.0), lot(odometer=999_999.0),
        lot(year=2016, sale_date="2026-08-25")]))
    assert x["odometer"].isna().tolist() == [True, True, False]
    assert x["est_retail_value"].isna().tolist() == [True, False, False]
    assert x["age"].tolist() == [8, 8, 10]


def training_frame(conn, copies=7):
    """The ingested lots, copied with jittered retail value and mileage, each copy half from
    IAA, with a price that follows the retail value and the damage."""
    lots = pd.DataFrame(conn.execute(f"SELECT {', '.join(pricing.COLUMNS)} FROM lots").fetchall())
    rng = np.random.default_rng(0)
    frames = []
    for _ in range(copies):
        c = lots.copy()
        for name in ("est_retail_value", "odometer"):
            c[name] = c[name].astype("float64") * rng.uniform(0.7, 1.3, len(c))
        c["source"] = np.where(rng.random(len(c)) < 0.5, "copart", "iaai")
        frames.append(c)
    df = pd.concat(frames, ignore_index=True)
    damage = df["damage_primary"].map({"MINOR DENT/SCRATCHES": 0.6, "FRONT END": 0.3}).fillna(0.4)
    retail = df["est_retail_value"].astype("float64").where(lambda v: v > 0, 4000.0)
    df[pricing.TARGET] = (retail * damage * rng.lognormal(0, 0.1, len(df))).round() + 25
    return df


def save_model(conn, path, **attrs):
    booster, _ = pricing.train(training_frame(conn, copies=2))
    booster.set_attr(**attrs)
    path.parent.mkdir(parents=True, exist_ok=True)
    booster.save_model(path)
    return path


def test_the_model_beats_the_baseline_on_the_test_set_and_survives_a_round_trip(
        conn, settings, make_export, tmp_path):
    ingest(conn, settings, make_export)
    df = training_frame(conn)
    df["damage_secondary"] = None  # a column without any value still trains
    test = df[df["source"] == "copart"].head(200)

    booster, metrics = pricing.train(df, test)

    categories = json.loads(booster.attr("categories"))
    assert categories["damage_secondary"] == ["?"]
    assert categories["source"] == ["copart", "iaai"]
    assert all(0 < len(v) <= len(df) / pricing.MIN_CATEGORY_ROWS for v in categories.values())
    assert metrics["model"]["rows_train"] == int(len(df) * 0.7)
    assert metrics["holdout"]["rows"] == len(df) - int(len(df) * 0.85)
    for part in ("holdout", "test"):
        assert metrics[part]["mae"] < metrics[part]["baseline_mae"]
        assert 0 < metrics[part]["mape"] < 1 and metrics[part]["r2"] > 0
        assert metrics[part]["median_error"] <= metrics[part]["mae"] * 2
    assert metrics["test"]["rows"] == 200
    path = tmp_path / "model.ubj"
    booster.save_model(path)
    loaded = xgb.Booster(model_file=str(path))
    np.testing.assert_allclose(pricing.predict(loaded, df), pricing.predict(booster, df),
                               rtol=1e-6)
    # Values never seen in training, and a lot with nothing known, are priced all the same.
    unseen = pd.DataFrame([lot(make="NEVER SEEN", damage_primary="METEOR"),
                           {**dict.fromkeys(pricing.COLUMNS), "source": "copart"}])
    assert (pricing.predict(loaded, unseen) > 0).all()
    assert "test" not in pricing.train(df.head(400))[1]


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


SOLD = datetime(2026, 8, 25, 14, tzinfo=UTC)


def imported(conn, lot, source="copart", price=5000, vin=None, **values):
    """A priced lot the apiauctions import brought (no file ids)."""
    row = {"final_price": price, "vin": vin or f"VIN{lot}", "year": 2018, "make": "TOYOTA",
           "sale_date": date(2026, 8, 25), "sale_at": SOLD, "removed_at": SOLD, **values}
    conn.execute(
        f"""INSERT INTO lots (source, lot_number, {', '.join(row)}, attributes, row_hash,
                              first_seen_at, last_seen_at, last_changed_at)
            VALUES (%s, %s, {', '.join(['%s'] * len(row))}, '{{}}', '\\x00', now(), now(),
                    now())""", (source, lot, *row.values()))


@pytest.fixture
def priced(conn, settings, make_export):
    """Our 300 exported lots, 3 of them priced, and apiauctions' priced lots."""
    ingest(conn, settings, make_export)
    ours = [r["lot_number"] for r in conn.execute(
        "SELECT lot_number FROM lots ORDER BY lot_number LIMIT 3").fetchall()]
    conn.execute("""UPDATE lots SET final_price = 4000, year = 2018, sale_date = '2026-09-20',
                                    vin = 'OURS' || lot_number,
                                    removed_at = CASE WHEN lot_number <> %s THEN now() END
                    WHERE lot_number = ANY(%s)""", (ours[2], ours))  # ours[2]: still listed
    imported(conn, "1")
    imported(conn, "2", source="iaai", model_group="Camry")
    imported(conn, "3", price=10)  # under the $25 minimum bid
    imported(conn, "4", year=1900)  # impossible year
    imported(conn, "5", vin="DUP", sale_at=SOLD - timedelta(days=30))  # sold again later:
    imported(conn, "6", vin="DUP")  # only this sale is kept
    imported(conn, "7", price=None)  # not sold
    return ours


def test_export_cleans_the_priced_lots_and_keeps_ours_for_the_test(conn, priced):
    training, test = pricing.export(conn, ["copart", "iaai"])

    assert list(training.columns) == ["source", *pricing.COLUMNS, pricing.TARGET]
    assert sorted(zip(training["source"], training["year"], training[pricing.TARGET],
                      strict=True)) == [("copart", 2018, 5000)] * 2 + [("iaai", 2018, 5000)]
    assert len(test) == 2  # the one still listed after its sale is left out
    assert set(test["source"]) == {"copart"}
    copart_only, _ = pricing.export(conn, ["copart"])
    assert set(copart_only["source"]) == {"copart"}


def test_cli_exports_and_trains(conn, settings, make_export, tmp_path, monkeypatch, capsys):
    ingest(conn, settings, make_export)
    train, test = tmp_path / "training" / "lots.csv.gz", tmp_path / "training" / "test.csv.gz"
    train.parent.mkdir()
    training_frame(conn).to_csv(train, index=False)
    conn.execute("UPDATE lots SET final_price = 3000, removed_at = now()")
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    exported = tmp_path / "exported.csv.gz"

    dated = conn.execute("SELECT count(*) AS n FROM lots WHERE sale_date IS NOT NULL"
                         ).fetchone()["n"]  # without a sale date there is no age at sale
    pricing.main(["export", "--out", str(exported), "--test", str(test)])
    assert f"{dated} lots written to {test}" in capsys.readouterr().out
    assert len(pd.read_csv(test)) == dated and pd.read_csv(exported).empty

    model = tmp_path / "models" / "model.ubj"
    pricing.main(["train", str(train), "--test", str(test), "--out", str(model)])
    metrics = json.loads(capsys.readouterr().out)["test"]
    assert metrics["rows"] == dated
    assert math.isnan(metrics["r2"])  # every test price is the same: R² means nothing
    assert model.is_file()
    pricing.main(["train", str(train), "--out", str(model)])
    assert "test" not in json.loads(capsys.readouterr().out)


def test_cli_needs_a_command():
    with pytest.raises(SystemExit):
        pricing.main([])
