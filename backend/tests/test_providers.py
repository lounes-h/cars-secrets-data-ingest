import pytest

from ingest.converters import to_text
from ingest.providers import COPART, PROVIDERS, FieldMapping, Provider, get_provider
from ingest.schema import COLUMN_NAMES


def test_registry():
    assert get_provider("copart") is COPART
    assert get_provider("iaai") is None
    assert set(PROVIDERS) == {"copart"}


def test_copart_maps_headers_to_canonical_columns():
    assert COPART.mapped_to("lot_number") == "lot_number"
    assert COPART.mapped_to("high_bid_non_vix_sealed_vix") == "high_bid"
    assert COPART.mapped_to("sale_time_hhmm") == "sale_at"
    assert COPART.mapped_to("time_zone") == "sale_at"
    assert COPART.mapped_to("grid_row") is None
    assert COPART.removal_scope == "yard_code"


def test_every_copart_mapping_targets_a_canonical_column():
    assert {m.column for m in COPART.fields} <= set(COLUMN_NAMES)


def test_mapping_to_unknown_column_is_refused():
    with pytest.raises(ValueError, match="unknown canonical column"):
        FieldMapping("colour", ("color",), to_text)


def test_unknown_removal_scope_is_refused():
    with pytest.raises(ValueError, match="removal scope"):
        Provider(name="x", display_name="X", key_aliases=("lot",), key_convert=str, fields=(),
                 removal_scope="branch")


def test_lots_table_has_every_canonical_column(conn):
    """The migration and schema.CANONICAL_COLUMNS must not drift apart."""
    cols = {r["column_name"] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'lots'"
    )}
    assert set(COLUMN_NAMES) <= cols
