"""End-to-end ingestion against a real PostgreSQL."""

import shutil

from ingest.pipeline import mark_interrupted, process_file
from ingest.providers import COPART

from .conftest import SAMPLE, read_sample


def ingest(conn, path, settings):
    return process_file(conn, path, COPART, settings)


def run(conn, file_id):
    return conn.execute("SELECT * FROM ingest_files WHERE id = %s", (file_id,)).fetchone()


def open_alerts(conn):
    return {r["type"]: r for r in conn.execute(
        "SELECT * FROM alerts WHERE resolved_at IS NULL")}


def open_alerts_all(conn):
    return conn.execute("SELECT * FROM alerts WHERE resolved_at IS NULL").fetchall()


def col(header, name):
    return header.index(name)


def rebytes(rows):
    """Same lots, different file bytes (the export's row counter differs), so the file is not
    skipped as a duplicate."""
    header, _ = read_sample()
    rows = [list(r) for r in rows]
    rows[0][col(header, "Id")] = str(int(rows[0][col(header, "Id")]) + 100_000)
    return rows


def test_first_ingest_loads_lots_and_rejects_bad_rows(conn, settings):
    path = settings.inbox_dir / "copart" / "sample.csv"
    path.parent.mkdir(parents=True)
    shutil.copy(SAMPLE, path)

    outcome = ingest(conn, path, settings)

    assert outcome.status == "succeeded"
    r = run(conn, outcome.file_id)
    assert (r["rows_total"], r["rows_new"], r["rows_rejected"]) == (302, 300, 2)
    assert r["finished_at"] > r["started_at"]  # wall clock, not the transaction start
    assert conn.execute("SELECT count(*) AS n FROM lots").fetchone()["n"] == 300
    assert conn.execute("SELECT count(*) AS n FROM rejected_rows").fetchone()["n"] == 2
    lot = conn.execute(
        "SELECT make, year, vin, sale_at FROM lots WHERE lot_number = '41633106'").fetchone()
    assert (lot["make"], lot["year"], lot["vin"]) == ("ACURA", 2013, "JH4CU2F65DC010051")
    # archived, gzipped, out of the inbox
    assert not path.exists()
    assert r["archived_path"].endswith("_sample.csv.gz")
    # first file seeds the registry without drift alerts; 2/302 rejected is under 1%
    assert conn.execute("SELECT count(*) AS n FROM column_registry").fetchone()["n"] == 59
    assert open_alerts(conn) == {}


def test_identical_file_is_skipped_as_duplicate(conn, settings, make_export):
    header, rows = read_sample()
    first = ingest(conn, make_export(rows), settings)
    second = ingest(conn, make_export(rows), settings)
    assert second.status == "duplicate"
    assert "identical to run" in run(conn, second.file_id)["error"]
    assert open_alerts(conn)["duplicate_file"]["severity"] == "info"
    assert run(conn, first.file_id)["status"] == "succeeded"


def test_changed_lots_are_versioned(conn, settings, make_export):
    header, rows = read_sample()
    ingest(conn, make_export(rows), settings)
    changed = [list(r) for r in rows]
    bid = col(header, "High Bid =non-vix,Sealed=Vix")
    for r in changed[:5]:
        r[bid] = "999.0"
    changed[10][col(header, "Id")] = "12345"  # row counter: not a change
    changed[11][col(header, "Create Date/Time")] = "2030-01-01-00.00.00.000000"  # volatile

    outcome = ingest(conn, make_export(changed), settings)

    r = run(conn, outcome.file_id)
    assert (r["rows_new"], r["rows_updated"], r["rows_unchanged"]) == (0, 5, 295)
    versions = conn.execute(
        "SELECT lot_number, attributes, superseded_by_file_id FROM lot_versions").fetchall()
    assert len(versions) == 5
    assert all(v["superseded_by_file_id"] == outcome.file_id for v in versions)
    lot = versions[0]["lot_number"]
    current = conn.execute("SELECT high_bid, attributes FROM lots WHERE lot_number = %s",
                           (lot,)).fetchone()
    assert current["high_bid"] == 999
    assert versions[0]["attributes"]["high_bid_non_vix_sealed_vix"] != "999"


def test_missing_lots_are_removed_only_in_yards_the_export_covers(conn, settings, make_export):
    header, rows = read_sample()
    yard = col(header, "Yard number")
    ingest(conn, make_export(rows), settings)
    yard1 = [r for r in rows if r[yard] == "1"]  # 194 lots
    # The next export only covers yard 1, and 4 of its lots are gone.
    outcome = ingest(conn, make_export(yard1[4:]), settings)

    assert run(conn, outcome.file_id)["rows_removed"] == 4
    removed = conn.execute(
        "SELECT yard_code, count(*) AS n FROM lots WHERE removed_at IS NOT NULL GROUP BY 1"
    ).fetchall()
    assert removed == [{"yard_code": "1", "n": 4}]  # the other yards are untouched


def test_removed_lot_that_comes_back_is_active_again(conn, settings, make_export):
    header, rows = read_sample()
    ingest(conn, make_export(rows), settings)
    ingest(conn, make_export(rows[1:]), settings)
    lot = rows[0][col(header, "Lot number")]
    assert conn.execute("SELECT removed_at FROM lots WHERE lot_number = %s",
                        (lot,)).fetchone()["removed_at"] is not None
    ingest(conn, make_export(rebytes(rows)), settings)
    assert conn.execute("SELECT removed_at FROM lots WHERE lot_number = %s",
                        (lot,)).fetchone()["removed_at"] is None


def test_truncated_yard_is_not_wiped(conn, settings, make_export):
    header, rows = read_sample()
    yard = col(header, "Yard number")
    ingest(conn, make_export(rows), settings)
    yard1 = [r for r in rows if r[yard] == "1"]
    yard2 = [r for r in rows if r[yard] == "2"]
    # yard 1 complete, yard 2 cut to 10 of its 75 lots (e.g. a truncated download)
    outcome = ingest(conn, make_export(yard1 + yard2[:10]), settings)

    r = run(conn, outcome.file_id)
    assert r["rows_removed"] == 0
    assert r["removal_skipped"] == ["2"]
    assert open_alerts(conn)["removal_skipped"]["details"]["scopes"] == ["2"]


def test_schema_drift_is_alerted_without_mass_updates(conn, settings, make_export):
    header, rows = read_sample()
    ingest(conn, make_export(rows), settings)
    trim = col(header, "Trim")
    bid = col(header, "High Bid =non-vix,Sealed=Vix")
    new_header = [h for i, h in enumerate(header) if i != trim] + ["Seller Rating"]
    new_rows = []
    for n, r in enumerate(rows):
        r = list(r)
        if n < 3:
            r[bid] = "777.0"
        new_rows.append([v for i, v in enumerate(r) if i != trim] + ["5"])

    outcome = ingest(conn, make_export(new_rows, header=new_header), settings)

    r = run(conn, outcome.file_id)
    assert (r["rows_updated"], r["rows_unchanged"]) == (3, 297)
    alerts = open_alerts(conn)
    added = alerts["schema_column_added"]["fingerprint"]
    assert added == "schema_column_added:copart:seller_rating"
    assert alerts["schema_column_removed"]["fingerprint"] == "schema_column_removed:copart:trim"
    registry = {r["normalized_name"]: r for r in conn.execute("SELECT * FROM column_registry")}
    assert registry["trim"]["status"] == "missing"
    assert registry["seller_rating"]["mapped_to"] is None
    lot = conn.execute("SELECT trim, attributes FROM lots LIMIT 1").fetchone()
    assert lot["trim"] is None and lot["attributes"]["seller_rating"] == "5"

    # The column comes back: its "removed" alert resolves on its own (and seller_rating,
    # now absent, gets one).
    ingest(conn, make_export(rebytes(rows)), settings)
    fingerprints = {r["fingerprint"] for r in open_alerts_all(conn)}
    assert "schema_column_removed:copart:trim" not in fingerprints
    assert "schema_column_removed:copart:seller_rating" in fingerprints
    assert conn.execute("SELECT status FROM column_registry WHERE normalized_name = 'trim'"
                        ).fetchone()["status"] == "active"


def test_duplicate_lot_in_one_file_keeps_last_occurrence(conn, settings, make_export):
    header, rows = read_sample()
    dup = list(rows[0])
    dup[col(header, "High Bid =non-vix,Sealed=Vix")] = "555.0"
    outcome = ingest(conn, make_export([*rows, dup]), settings)
    r = run(conn, outcome.file_id)
    assert (r["rows_new"], r["rows_rejected"]) == (300, 1)
    reason = conn.execute("SELECT reason FROM rejected_rows").fetchone()["reason"]
    assert reason.startswith("duplicate lot number")
    assert conn.execute("SELECT high_bid FROM lots WHERE lot_number = %s",
                        (rows[0][col(header, "Lot number")],)).fetchone()["high_bid"] == 555


def test_file_without_lot_key_fails_and_is_moved(conn, settings, make_export):
    header, rows = read_sample()
    key = col(header, "Lot number")
    path = make_export([r[:key] + r[key + 1:] for r in rows],
                       header=header[:key] + header[key + 1:])
    outcome = ingest(conn, path, settings)
    assert outcome.status == "failed"
    r = run(conn, outcome.file_id)
    assert "lot key" in r["error"]
    assert not path.exists()
    assert (settings.failed_dir / "copart" / f"{outcome.file_id}_{path.name}").exists()
    alert = open_alerts(conn)["file_rejected"]
    assert alert["severity"] == "critical" and alert["file_id"] == outcome.file_id
    assert conn.execute("SELECT count(*) AS n FROM lots").fetchone()["n"] == 0


def test_database_error_rolls_back_everything(conn, settings, make_export, monkeypatch):
    header, rows = read_sample()
    import ingest.loader as loader

    def boom(*a, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(loader, "_store_rejects", boom)
    outcome = ingest(conn, make_export(rows), settings)
    assert outcome.status == "failed"
    assert run(conn, outcome.file_id)["error"] == "RuntimeError: disk full"
    assert conn.execute("SELECT count(*) AS n FROM lots").fetchone()["n"] == 0
    assert open_alerts(conn)["ingest_failed"]["severity"] == "critical"


def test_mark_interrupted(conn):
    conn.execute("""INSERT INTO ingest_files (source, filename, sha256, status)
                    VALUES ('copart', 'a.csv', 'x', 'processing')""")
    assert mark_interrupted(conn) == 1
    assert conn.execute("SELECT status FROM ingest_files").fetchone()["status"] == "interrupted"


def test_successful_or_duplicate_ingest_resolves_staleness_alert(conn, settings, make_export):
    from ingest.alerts import AlertSpec, raise_alert

    stale = AlertSpec("critical", "no_new_file", "stale", "no_new_file:copart")
    raise_alert(conn, stale)
    ingest(conn, make_export(), settings)
    assert "no_new_file" not in open_alerts(conn)

    raise_alert(conn, stale)
    assert ingest(conn, make_export(), settings).status == "duplicate"
    assert "no_new_file" not in open_alerts(conn)
