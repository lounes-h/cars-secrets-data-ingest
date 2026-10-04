from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from .conftest import read_sample
from .test_api_sync import api_lots, ingest, lot_numbers, sync

ROOT = Path(__file__).resolve().parent.parent


def config(db_url):
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["database_url"] = db_url
    return cfg


def test_offsite_columns_are_backfilled_from_attributes(conn, db_url):
    cfg = config(db_url)
    command.downgrade(cfg, "0006")
    conn.execute(
        """INSERT INTO lots (source, lot_number, attributes, row_hash, first_seen_at,
                             last_seen_at, last_changed_at)
           VALUES ('copart', '1', %s, '\\x00', now(), now(), now()),
                  ('copart', '2', '{}', '\\x00', now(), now(), now())""",
        ('{"offsite_address1": "645 W UNION ST", "offsite_city": "ALLENTOWN", '
         '"offsite_state": "PA", "offsite_zip": "18101"}',),
    )
    command.upgrade(cfg, "head")
    rows = conn.execute("""SELECT lot_number, offsite_address, offsite_city, offsite_state,
                                  offsite_zip FROM lots ORDER BY lot_number""").fetchall()
    assert rows == [
        {"lot_number": "1", "offsite_address": "645 W UNION ST", "offsite_city": "ALLENTOWN",
         "offsite_state": "PA", "offsite_zip": "18101"},
        {"lot_number": "2", "offsite_address": None, "offsite_city": None,
         "offsite_state": None, "offsite_zip": None},
    ]


def test_test_lots_already_served_leave_the_api_database(
        conn, db_url, settings, make_export, api_url):
    _, rows = read_sample()
    ingest(conn, make_export(rows), settings)
    sync(conn, api_url)
    lot = lot_numbers(rows)[0]
    # Served before test lots were told apart: no new sync_version to make the sync look again.
    conn.execute("UPDATE lots SET yard_name = 'TSS TEST YARD' WHERE lot_number = %s", (lot,))
    cfg = config(db_url)
    command.downgrade(cfg, "0015")
    command.upgrade(cfg, "head")

    result = sync(conn, api_url)

    assert (result.copied, result.deleted) == (0, 1)
    assert lot not in api_lots(api_url)


def test_migrations_form_one_numbered_chain():
    """Two branches that each add migration NNNN merge without a conflict, but then
    `alembic upgrade` fails and no service starts."""
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    script = ScriptDirectory.from_config(cfg)
    files = sorted((ROOT / "migrations" / "versions").glob("[0-9][0-9][0-9][0-9]_*.py"))
    chain = [r.revision for r in script.walk_revisions()][::-1]  # oldest first
    assert len(script.get_heads()) == 1
    assert chain == [f.name[:4] for f in files]  # one migration per number, in file order
