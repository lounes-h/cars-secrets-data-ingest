from pathlib import Path

from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parent.parent


def test_offsite_columns_are_backfilled_from_attributes(conn, db_url):
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["database_url"] = db_url
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
