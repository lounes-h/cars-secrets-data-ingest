"""Alert rules and storage.

Rules are pure functions returning AlertSpec lists, so they are testable without a database.
`raise_alert` deduplicates by fingerprint: while an alert is unresolved, the same condition
bumps its `occurrences` instead of creating a new row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from psycopg import Connection
from psycopg.types.json import Jsonb

from .config import Settings
from .db import notify

Severity = Literal["info", "warning", "critical"]


@dataclass(frozen=True)
class AlertSpec:
    severity: Severity
    type: str
    title: str
    fingerprint: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunFacts:
    """What a finished ingest looked like, as input to the run rules."""

    source: str
    file_id: int
    filename: str
    rows_total: int
    rows_rejected: int
    field_errors: dict[str, int]
    duration_seconds: float
    baseline_rows: float | None
    removal_skipped: list[str]  # removal scopes (yards) that looked truncated


def evaluate_run(facts: RunFacts, settings: Settings) -> list[AlertSpec]:
    alerts: list[AlertSpec] = []
    src, name = facts.source, facts.filename
    total = max(facts.rows_total, 1)

    ratio = facts.rows_rejected / total
    if facts.rows_rejected and ratio >= settings.rejected_warning_ratio:
        severity: Severity = "critical" if ratio >= settings.rejected_critical_ratio else "warning"
        alerts.append(AlertSpec(
            severity, "rejected_rows",
            f"{facts.rows_rejected} of {facts.rows_total} rows rejected in {name} ({ratio:.1%})",
            f"rejected_rows:{src}:{facts.file_id}",
            {"rows_rejected": facts.rows_rejected, "rows_total": facts.rows_total},
        ))

    bad_fields = {k: v for k, v in facts.field_errors.items()
                  if v / total >= settings.rejected_warning_ratio}
    if bad_fields:
        alerts.append(AlertSpec(
            "warning", "field_conversion_errors",
            f"Unparseable values in {', '.join(sorted(bad_fields))} ({name})",
            f"field_conversion_errors:{src}:{facts.file_id}",
            {"field_errors": bad_fields, "hint": "the source format of these columns changed?"},
        ))

    if facts.baseline_rows:
        deviation = (facts.rows_total - facts.baseline_rows) / facts.baseline_rows
        if abs(deviation) >= settings.row_count_deviation_ratio:
            alerts.append(AlertSpec(
                "warning", "row_count_anomaly",
                f"{name} has {facts.rows_total} rows, {deviation:+.0%} vs recent average "
                f"({facts.baseline_rows:.0f})",
                f"row_count_anomaly:{src}:{facts.file_id}",
                {"rows_total": facts.rows_total, "baseline_rows": round(facts.baseline_rows)},
            ))

    if facts.removal_skipped:
        n = len(facts.removal_skipped)
        alerts.append(AlertSpec(
            "warning", "removal_skipped",
            f"{n} scope(s) in {name} hold under half their active lots; "
            "missing lots there were NOT marked removed",
            f"removal_skipped:{src}:{facts.file_id}",
            {"scopes": facts.removal_skipped[:50], "scopes_total": n},
        ))

    if facts.duration_seconds >= settings.slow_ingest_seconds:
        alerts.append(AlertSpec(
            "warning", "slow_ingest",
            f"Ingesting {name} took {facts.duration_seconds:.0f}s",
            f"slow_ingest:{src}:{facts.file_id}",
            {"duration_seconds": round(facts.duration_seconds, 1)},
        ))
    return alerts


def evaluate_schema(
    source: str, *, first_file: bool, added: list[str], removed: list[str], filename: str
) -> list[AlertSpec]:
    """Schema drift between the registry and a new file. The first file only seeds the registry."""
    if first_file:
        return []
    alerts = [
        AlertSpec(
            "warning", "schema_column_added",
            f"New column '{col}' in {source} export (stored in attributes, not mapped)",
            f"schema_column_added:{source}:{col}",
            {"column": col, "file": filename,
             "hint": "map it in the provider if it should become a typed column"},
        )
        for col in added
    ]
    alerts += [
        AlertSpec(
            "warning", "schema_column_removed",
            f"Column '{col}' missing from {source} export",
            f"schema_column_removed:{source}:{col}",
            {"column": col, "file": filename,
             "hint": "renamed? add the new header as an alias in the provider"},
        )
        for col in removed
    ]
    return alerts


def raise_alert(
    conn: Connection, spec: AlertSpec, *, source: str | None = None, file_id: int | None = None
) -> int:
    row = conn.execute(
        """
        INSERT INTO alerts (severity, type, title, details, fingerprint, source, file_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (fingerprint) WHERE resolved_at IS NULL DO UPDATE
           SET occurrences  = alerts.occurrences + 1,
               last_seen_at = now(),
               severity     = EXCLUDED.severity,
               title        = EXCLUDED.title,
               details      = EXCLUDED.details
        RETURNING id, (xmax = 0) AS inserted
        """,
        (spec.severity, spec.type, spec.title, Jsonb(spec.details), spec.fingerprint,
         source, file_id),
    ).fetchone()
    assert row is not None
    notify(conn, "alert", id=row["id"], severity=spec.severity, title=spec.title,
           new=row["inserted"])
    return row["id"]


def resolve_fingerprint(conn: Connection, fingerprint: str, by: str = "system") -> int:
    """Auto-resolve an open alert whose condition cleared. Returns how many were resolved."""
    cur = conn.execute(
        """UPDATE alerts SET resolved_at = now(), resolved_by = %s
           WHERE fingerprint = %s AND resolved_at IS NULL""",
        (by, fingerprint),
    )
    if cur.rowcount:
        notify(conn, "alert_resolved", fingerprint=fingerprint)
    return cur.rowcount
