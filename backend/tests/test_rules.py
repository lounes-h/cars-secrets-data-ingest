"""Alert and monitor rules (pure functions, no database)."""

from datetime import UTC, datetime, timedelta

from ingest.alerts import RunFacts, evaluate_run, evaluate_schema
from ingest.config import Settings
from ingest.monitor import check_staleness, check_worker

SETTINGS = Settings(_env_file=None)
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


def facts(**overrides) -> RunFacts:
    base = dict(source="copart", file_id=1, filename="f.csv", rows_total=100_000,
                rows_rejected=0, field_errors={}, duration_seconds=10, baseline_rows=100_000,
                removal_skipped=[])
    return RunFacts(**{**base, **overrides})


def types(alerts):
    return {(a.type, a.severity) for a in alerts}


def test_clean_run_raises_nothing():
    assert evaluate_run(facts(), SETTINGS) == []


def test_rejected_rows_thresholds():
    assert evaluate_run(facts(rows_rejected=6), SETTINGS) == []  # real export: 6 of 142k
    assert types(evaluate_run(facts(rows_rejected=2_000), SETTINGS)) == {
        ("rejected_rows", "warning")}
    assert types(evaluate_run(facts(rows_rejected=20_000), SETTINGS)) == {
        ("rejected_rows", "critical")}


def test_field_conversion_errors():
    alerts = evaluate_run(facts(field_errors={"year": 5_000, "odometer": 3}), SETTINGS)
    assert types(alerts) == {("field_conversion_errors", "warning")}
    assert alerts[0].details["field_errors"] == {"year": 5_000}


def test_row_count_anomaly_both_directions_and_no_baseline():
    assert types(evaluate_run(facts(rows_total=24_569, baseline_rows=142_335), SETTINGS)) == {
        ("row_count_anomaly", "warning")}
    assert types(evaluate_run(facts(rows_total=200_000), SETTINGS)) == {
        ("row_count_anomaly", "warning")}
    assert evaluate_run(facts(rows_total=110_000), SETTINGS) == []
    assert evaluate_run(facts(baseline_rows=None), SETTINGS) == []


def test_removal_skipped_and_slow():
    alerts = evaluate_run(facts(removal_skipped=["1", "2"], duration_seconds=301), SETTINGS)
    assert types(alerts) == {("removal_skipped", "warning"), ("slow_ingest", "warning")}


def test_fingerprints_are_per_file():
    a = evaluate_run(facts(file_id=1, rows_rejected=5_000), SETTINGS)[0]
    b = evaluate_run(facts(file_id=2, rows_rejected=5_000), SETTINGS)[0]
    assert a.fingerprint != b.fingerprint


def test_schema_drift():
    assert evaluate_schema("copart", first_file=True, added=["a"], removed=[], filename="f") == []
    alerts = evaluate_schema("copart", first_file=False, added=["seller_rating"],
                             removed=["trim"], filename="f")
    assert {a.fingerprint for a in alerts} == {
        "schema_column_added:copart:seller_rating", "schema_column_removed:copart:trim"}


def test_check_worker():
    assert check_worker(NOW - timedelta(seconds=30), NOW, 120).spec is None
    late = check_worker(NOW - timedelta(minutes=5), NOW, 120).spec
    assert late.severity == "critical" and "5 min" in late.title
    assert "never" in check_worker(None, NOW, 120).spec.title


def test_check_staleness():
    fresh = check_staleness("copart", NOW - timedelta(minutes=60), None, NOW, 75)
    assert fresh.spec is None and fresh.fingerprint == "no_new_file:copart"
    stale = check_staleness("copart", NOW - timedelta(minutes=80), None, NOW, 75)
    assert stale.spec.severity == "critical" and "80 min" in stale.spec.title


def test_check_staleness_before_any_file_uses_worker_start():
    assert check_staleness("copart", None, NOW - timedelta(minutes=10), NOW, 75).spec is None
    stale = check_staleness("copart", None, NOW - timedelta(minutes=90), NOW, 75).spec
    assert "since the worker started" in stale.title
    assert check_staleness("copart", None, None, NOW, 75).spec is None
