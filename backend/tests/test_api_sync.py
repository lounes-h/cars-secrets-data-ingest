"""The API database: what it holds, and that it follows every change in the ingest database."""

import psycopg
import pytest
from psycopg.rows import dict_row

from ingest import api_sync
from ingest.pipeline import process_file
from ingest.providers import COPART

from .conftest import read_sample
from .test_pipeline import auctions_over, col, rebytes

SERVED = [name for name, _ in api_sync.COLUMNS]


def ingest(conn, path, settings):
    outcome = process_file(conn, path, COPART, settings)
    assert outcome.status == "succeeded"
    return outcome


def sync(conn, api_url, keep=240):
    return api_sync.sync(conn, api_url, keep)


def api(api_url, query="SELECT * FROM lots", params=None):
    with psycopg.connect(api_url, autocommit=True, row_factory=dict_row) as c:
        cur = c.execute(query, params)
        return cur.fetchall() if cur.description else None


def api_lots(api_url):
    return {r["lot_number"]: r for r in api(api_url)}


def ingest_lots(conn):
    return {r["lot_number"]: r for r in conn.execute(f"SELECT {', '.join(SERVED)} FROM lots")}


def watermark(api_url):
    return api(api_url, "SELECT sync_version, synced_at FROM ingest_sync")[0]


def lot_numbers(rows):
    header, _ = read_sample()
    return [r[col(header, "Lot number")] for r in rows]


def test_first_sync_copies_active_lots_with_only_the_served_columns(
        conn, settings, make_export, api_url):
    _, rows = read_sample()
    ingest(conn, make_export(rows), settings)
    old = lot_numbers(rows)[0]  # removed a day ago, long after its auction: not the API's
    conn.execute("""UPDATE lots SET removed_at = now() - interval '1 day',
                                    sale_at = now() - interval '2 days'
                    WHERE lot_number = %s""", (old,))

    result = sync(conn, api_url)

    assert (result.full, result.copied, result.deleted) == (True, 299, 0)
    copied = api_lots(api_url)
    expected = ingest_lots(conn)
    del expected[old]
    assert copied == expected
    assert list(next(iter(copied.values()))) == SERVED  # no attributes, hashes or file ids
    version = conn.execute("SELECT max(sync_version) AS v FROM lots").fetchone()["v"]
    assert result.sync_version == version == watermark(api_url)["sync_version"]


def test_later_syncs_copy_only_new_and_changed_lots(conn, settings, make_export, api_url):
    header, rows = read_sample()
    ingest(conn, make_export(rows[:-1]), settings)
    sync(conn, api_url)
    changed = [list(r) for r in rows]
    changed[0][col(header, "High Bid =non-vix,Sealed=Vix")] = "999.0"
    ingest(conn, make_export(changed), settings)  # one lot changed, the last one is new

    result = sync(conn, api_url)

    assert (result.full, result.copied, result.deleted) == (False, 2, 0)
    assert api_lots(api_url) == ingest_lots(conn)
    assert api_lots(api_url)[lot_numbers(rows)[0]]["high_bid"] == 999

    # Nothing changed: nothing is copied, and the API database is not written to.
    before = watermark(api_url)
    ingest(conn, make_export(rebytes(changed)), settings)  # only the ignored row counter differs
    result = sync(conn, api_url)
    assert (result.copied, result.deleted, result.sync_version) == (0, 0, before["sync_version"])
    assert watermark(api_url) == before


def test_removed_lot_stays_until_its_auction_is_surely_over(conn, settings, make_export, api_url):
    header, rows = read_sample()
    yard1 = [r for r in auctions_over(rows) if r[col(header, "Yard number")] == "1"]
    live, finished = lot_numbers(yard1[:2])
    ingest(conn, make_export(yard1), settings)
    sync(conn, api_url)
    # Its sale opened 3 h ago: past the 2 h removal grace, but bidding may still run to 4 h.
    conn.execute("UPDATE lots SET sale_at = now() - interval '3 hours' WHERE lot_number = %s",
                 (live,))
    ingest(conn, make_export(yard1[2:]), settings)  # both lots are gone from the export

    result = sync(conn, api_url)

    assert (result.copied, result.deleted) == (1, 1)  # the lot sold in 2020 is dropped
    lots = api_lots(api_url)
    assert finished not in lots
    assert lots[live]["removed_at"] is not None  # kept, and no longer served as active

    # Time passes without any new export: the next sync drops it anyway.
    api(api_url, "UPDATE lots SET sale_at = now() - interval '5 hours' WHERE lot_number = %s",
        (live,))
    result = sync(conn, api_url)
    assert (result.copied, result.deleted) == (0, 1)
    assert live not in api_lots(api_url)

    # Both come back in a later export: active again in the API database.
    ingest(conn, make_export(rebytes(yard1)), settings)
    result = sync(conn, api_url)
    assert result.copied == 2
    lots = api_lots(api_url)
    assert lots[live]["removed_at"] is None and lots[finished]["removed_at"] is None


def test_copart_test_lots_are_never_served(conn, settings, make_export, api_url):
    header, rows = read_sample()
    rows = [list(r) for r in rows]
    rows[0][col(header, "Yard name")] = "TS - TEST STAGE Prod"
    rows[1][col(header, "Special Note")] = "TSS TEST SUB LOT"
    rows[2][col(header, "Special Note")] = "Biohazard for suspected narcotics *Not tested*"
    test_yard, sub_lot, real = lot_numbers(rows[:3])
    ingest(conn, make_export(rows), settings)

    sync(conn, api_url)

    served = api_lots(api_url)
    assert set(served) == set(ingest_lots(conn)) - {test_yard, sub_lot}
    assert real in served

    # A served lot moved to a test yard leaves at the next sync.
    rows[2][col(header, "Yard name")] = "TSS TEST YARD"
    ingest(conn, make_export(rows), settings)
    result = sync(conn, api_url)
    assert (result.full, result.copied, result.deleted) == (False, 0, 1)
    assert real not in api_lots(api_url)


def test_a_typed_column_that_changes_without_a_new_hash_is_synced(
        conn, settings, make_export, api_url):
    """A new mapping (or a header alias) fills a typed column although the export's values,
    and so the row hash, did not change."""
    _, rows = read_sample()
    lot = lot_numbers(rows)[0]
    ingest(conn, make_export(rows), settings)
    conn.execute("UPDATE lots SET make = 'UNMAPPED' WHERE lot_number = %s", (lot,))
    sync(conn, api_url)
    assert api_lots(api_url)[lot]["make"] == "UNMAPPED"

    ingest(conn, make_export(rebytes(rows)), settings)
    result = sync(conn, api_url)

    assert result.copied == 1
    assert api_lots(api_url)[lot]["make"] == ingest_lots(conn)[lot]["make"] != "UNMAPPED"


def test_a_lost_watermark_or_column_or_a_restored_ingest_database_copies_everything(
        conn, settings, make_export, api_url):
    _, rows = read_sample()
    ingest(conn, make_export(rows), settings)
    sync(conn, api_url)

    # The API database was emptied of its watermark and holds a lot it should not.
    api(api_url, "DELETE FROM ingest_sync")
    api(api_url, "INSERT INTO lots (source, lot_number, first_seen_at, last_changed_at) "
                 "VALUES ('copart', 'ghost', now(), now())")
    assert sync(conn, api_url).full
    assert api_lots(api_url) == ingest_lots(conn)

    # A column is missing, e.g. a canonical column added since the table was created.
    api(api_url, "ALTER TABLE lots DROP COLUMN make")
    result = sync(conn, api_url)
    assert (result.full, result.copied) == (True, 300)
    assert api_lots(api_url) == ingest_lots(conn)

    # The ingest database went back in time (restored from a backup).
    api(api_url, "UPDATE ingest_sync SET sync_version = sync_version + 1000000")
    assert sync(conn, api_url).full
    assert watermark(api_url)["sync_version"] == result.sync_version

    # The table itself was dropped while the watermark survived.
    api(api_url, "DROP TABLE lots")
    assert sync(conn, api_url).full
    assert api_lots(api_url) == ingest_lots(conn)


def test_a_failed_sync_changes_nothing_and_the_next_one_catches_up(
        conn, settings, make_export, api_url):
    header, rows = read_sample()
    ingest(conn, make_export(rows), settings)
    first = sync(conn, api_url)
    changed = [list(r) for r in rows]
    changed[0][col(header, "High Bid =non-vix,Sealed=Vix")] = "999.0"
    ingest(conn, make_export(changed), settings)
    before = api_lots(api_url)
    # Any write fails half way (a lost connection would behave the same).
    api(api_url, "ALTER TABLE lots ADD CONSTRAINT broken CHECK (high_bid < 0) NOT VALID")

    with pytest.raises(psycopg.errors.CheckViolation):
        sync(conn, api_url)

    assert api_lots(api_url) == before
    assert watermark(api_url)["sync_version"] == first.sync_version
    api(api_url, "ALTER TABLE lots DROP CONSTRAINT broken")
    assert sync(conn, api_url).copied == 1
    assert api_lots(api_url) == ingest_lots(conn)


def test_refuses_to_sync_into_the_ingest_database(conn, settings, make_export, db_url):
    _, rows = read_sample()
    ingest(conn, make_export(rows), settings)

    with pytest.raises(RuntimeError, match="points at the ingest database"):
        sync(conn, db_url)

    assert conn.execute("SELECT count(*) AS n FROM lots").fetchone()["n"] == 300


def test_loads_version_only_the_lots_they_change(conn, settings, make_export):
    header, rows = read_sample()
    yard1 = [r for r in auctions_over(rows) if r[col(header, "Yard number")] == "1"]
    ingest(conn, make_export(yard1), settings)
    versions = {r["lot_number"]: r["sync_version"]
                for r in conn.execute("SELECT lot_number, sync_version FROM lots")}
    assert None not in versions.values() and len(set(versions.values())) == len(yard1)

    # Only Copart's export timestamp (a volatile column, typed as source_created_at) moved.
    stamped = [list(r) for r in yard1]
    for r in stamped:
        r[col(header, "Create Date/Time")] = "2030-01-01-00.00.00.000000"
    ingest(conn, make_export(stamped), settings)
    assert {r["lot_number"]: r["sync_version"] for r in conn.execute(
        "SELECT lot_number, sync_version FROM lots")} == versions

    ingest(conn, make_export(stamped[1:]), settings)  # the first lot is removed, nothing else

    after = {r["lot_number"]: r["sync_version"]
             for r in conn.execute("SELECT lot_number, sync_version FROM lots")}
    removed = lot_numbers(yard1)[0]
    assert after[removed] > max(versions.values())
    assert {k: v for k, v in after.items() if k != removed} == {
        k: v for k, v in versions.items() if k != removed}
