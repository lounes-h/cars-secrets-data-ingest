import json

import pytest

from ingest.parsing import CsvFile, FileRejected, row_hash
from ingest.providers import COPART
from ingest.schema import COLUMN_INDEX

from .conftest import SAMPLE, read_sample, write_csv


def test_parses_real_sample_and_rejects_shifted_rows():
    with CsvFile(SAMPLE, COPART) as f:
        rows = list(f.rows())
    assert len(rows) == 300
    assert f.stats.rows_total == 302
    assert f.stats.rows_rejected == 2
    assert all("expected 59 fields" in r.reason for r in f.stats.rejected)

    first = rows[0]
    assert first.lot_number == "41633106"
    assert first.values[COLUMN_INDEX["make"]] == "ACURA"
    assert first.values[COLUMN_INDEX["year"]] == 2013
    assert first.values[COLUMN_INDEX["yard_code"]] == "1"
    assert first.values[COLUMN_INDEX["sale_at"]].isoformat() == "2026-09-18T12:00:00-07:00"
    attributes = json.loads(first.attributes)
    assert "id" not in attributes  # the export's row counter is not stored
    assert attributes["est_retail_value"] == "8119"  # canonicalized from 8119.0
    assert "special_note" not in attributes  # empty values are dropped


def test_empty_file_is_rejected(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("")
    with pytest.raises(FileRejected, match="empty"):
        CsvFile(path, COPART)


def test_missing_lot_key_column_is_rejected(tmp_path):
    header, rows = read_sample()
    idx = header.index("Lot number")
    path = write_csv(tmp_path / "x.csv", [h for i, h in enumerate(header) if i != idx],
                     [[v for i, v in enumerate(r) if i != idx] for r in rows])
    with pytest.raises(FileRejected, match="lot key"):
        CsvFile(path, COPART)


def test_invalid_lot_number_rejects_only_that_row(tmp_path):
    header, rows = read_sample()
    rows = rows[:3]
    rows[1][header.index("Lot number")] = "ABC"
    with CsvFile(write_csv(tmp_path / "x.csv", header, rows), COPART) as f:
        parsed = list(f.rows())
    assert len(parsed) == 2
    assert f.stats.rejected[0].reason == "invalid or missing lot number"
    assert f.stats.rejected[0].line_no == 3


def test_bad_typed_value_keeps_row_and_counts_field_error(tmp_path):
    header, rows = read_sample()
    rows = rows[:2]
    rows[0][header.index("Year")] = "twenty"
    with CsvFile(write_csv(tmp_path / "x.csv", header, rows), COPART) as f:
        parsed = list(f.rows())
    assert len(parsed) == 2
    assert parsed[0].values[COLUMN_INDEX["year"]] is None
    assert json.loads(parsed[0].attributes)["year"] == "twenty"
    assert f.stats.field_errors == {"year": 1}


def test_utf8_bom_and_blank_lines_are_tolerated(tmp_path):
    header, rows = read_sample()
    path = tmp_path / "bom.csv"
    write_csv(path, header, rows[:2])
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes() + b"\r\n\r\n")
    with CsvFile(path, COPART) as f:
        assert f.header[0] == "id"
        assert len(list(f.rows())) == 2


def test_unknown_columns_are_kept_in_attributes(tmp_path):
    header, rows = read_sample()
    path = write_csv(tmp_path / "x.csv", [*header, "Seller Rating"], [[*rows[0], "5"]])
    with CsvFile(path, COPART) as f:
        (row,) = list(f.rows())
    assert json.loads(row.attributes)["seller_rating"] == "5"


def test_row_hash_ignores_key_order_and_volatile_columns():
    a = {"make": "ACURA", "year": "2013", "create_date_time": "t1"}
    b = {"year": "2013", "create_date_time": "t2", "make": "ACURA"}
    volatile = frozenset({"create_date_time"})
    assert row_hash(a, volatile)[1] == row_hash(b, volatile)[1]
    assert row_hash(a)[1] != row_hash(b)[1]
    assert row_hash(a, volatile)[1] != row_hash({**a, "make": "BMW"}, volatile)[1]


def test_same_lot_formatted_differently_hashes_equal(tmp_path):
    header, rows = read_sample()
    row = rows[0]
    other = list(row)
    other[header.index("Est. Retail Value")] = "8119"  # "8119.0" in the original
    other[header.index("Create Date/Time")] = "2026-09-17-09.00.00.000001"
    other[header.index("Id")] = "999"
    with CsvFile(write_csv(tmp_path / "x.csv", header, [row, other]), COPART) as f:
        first, second = list(f.rows())
    assert first.row_hash == second.row_hash
