"""Loads one parsed export into PostgreSQL in a single transaction.

Flow: COPY rows into a temp staging table -> drop in-file duplicates -> count new/updated/
unchanged against `lots` -> snapshot previous versions of changed lots into `lot_versions` ->
upsert `lots` -> mark lots absent from the export as removed -> update the column registry.
Any error rolls everything back, so a lot is never half-updated.

Change detection compares row hashes (which leave out the provider's volatile columns) when
the export has the same columns as the previous one.
When the column set changed (schema drift) hashes are not comparable, so only the values of the
columns present in both exports are compared; otherwise every lot would look "updated".
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from psycopg import Connection
from psycopg.types.json import Jsonb

from .config import Settings
from .parsing import CsvFile, RejectedRow
from .schema import CANONICAL_COLUMNS, COLUMN_NAMES

_TYPED_DDL = ", ".join(f"{name} {sql_type}" for name, sql_type in CANONICAL_COLUMNS)
_TYPED = ", ".join(COLUMN_NAMES)
_TYPED_UPDATE = ", ".join(f"{name} = EXCLUDED.{name}" for name in COLUMN_NAMES)


@dataclass
class LoadResult:
    rows_total: int = 0
    rows_new: int = 0
    rows_updated: int = 0
    rows_unchanged: int = 0
    rows_rejected: int = 0
    rows_removed: int = 0
    removal_skipped: list[str] = field(default_factory=list)
    baseline_rows: float | None = None
    field_errors: dict[str, int] = field(default_factory=dict)
    first_file: bool = False
    columns_added: list[str] = field(default_factory=list)
    columns_removed: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0


def baseline_rows(conn: Connection, source: str, runs: int) -> float | None:
    row = conn.execute(
        """SELECT avg(rows_total)::float AS avg FROM (
               SELECT rows_total FROM ingest_files
               WHERE source = %s AND status = 'succeeded'
               ORDER BY received_at DESC LIMIT %s) recent""",
        (source, runs),
    ).fetchone()
    return row["avg"] if row else None


def load_file(
    conn: Connection, file_id: int, csv_file: CsvFile, settings: Settings
) -> LoadResult:
    """Must be called inside a transaction; the caller commits."""
    started = time.monotonic()
    source = csv_file.provider.name
    result = LoadResult(baseline_rows=baseline_rows(conn, source, settings.baseline_runs))
    registry = {
        r["normalized_name"]: r["status"]
        for r in conn.execute(
            "SELECT normalized_name, status FROM column_registry WHERE source = %s", (source,)
        )
    }
    ignored = csv_file.provider.ignored_columns | csv_file.provider.volatile_columns
    previous = {c for c, st in registry.items() if st == "active" and c not in ignored}
    current = {c for c in csv_file.header if c not in ignored}
    if not registry or previous == current:
        changed = _hash_changed
        params: dict = {}
    else:
        changed = _common_columns_changed
        params = {"common": sorted(previous & current)}

    # One ingest per provider at a time, even with several workers.
    conn.execute("SELECT pg_advisory_xact_lock(hashtext('ingest:' || %s))", (source,))
    conn.execute(
        f"""CREATE TEMP TABLE stage (
                line_no integer, lot_number text, {_TYPED_DDL}, attributes jsonb, row_hash bytea
            ) ON COMMIT DROP"""
    )
    with conn.cursor() as cur, cur.copy(
        f"COPY stage (line_no, lot_number, {_TYPED}, attributes, row_hash) FROM STDIN"
    ) as copy:
        for row in csv_file.rows():
            copy.write_row((row.line_no, row.lot_number, *row.values, row.attributes, row.row_hash))
    stats = csv_file.stats
    result.rows_total = stats.rows_total
    result.field_errors = dict(stats.field_errors)
    conn.execute("CREATE INDEX ON stage (lot_number)")
    conn.execute("ANALYZE stage")

    # The same lot twice in one export: the last occurrence wins.
    duplicates = conn.execute(
        """DELETE FROM stage s USING stage t
           WHERE s.lot_number = t.lot_number AND s.line_no < t.line_no
           RETURNING s.line_no, s.lot_number, s.attributes"""
    ).fetchall()
    rejects = list(stats.rejected) + [
        RejectedRow(d["line_no"], f"duplicate lot number {d['lot_number']} in file",
                    [json.dumps(d["attributes"])])
        for d in duplicates
    ]
    result.rows_rejected = stats.rows_rejected + len(duplicates)

    counts = conn.execute(
        f"""SELECT count(*) FILTER (WHERE l.lot_number IS NULL)           AS new,
                   count(*) FILTER (WHERE l.lot_number IS NOT NULL
                                      AND {changed("l", "s")})            AS updated,
                   count(*)                                               AS loaded
            FROM stage s
            LEFT JOIN lots l ON l.source = %(source)s AND l.lot_number = s.lot_number""",
        {"source": source, **params},
    ).fetchone()
    assert counts is not None
    result.rows_new, result.rows_updated = counts["new"], counts["updated"]
    result.rows_unchanged = counts["loaded"] - counts["new"] - counts["updated"]

    conn.execute(
        f"""INSERT INTO lot_versions (source, lot_number, valid_from, valid_to, row_hash,
                                      attributes, file_id, superseded_by_file_id)
            SELECT l.source, l.lot_number, l.last_changed_at, now(), l.row_hash,
                   l.attributes, l.last_file_id, %(file_id)s
            FROM lots l JOIN stage s ON l.source = %(source)s AND l.lot_number = s.lot_number
            WHERE {changed("l", "s")}""",
        {"source": source, "file_id": file_id, **params},
    )
    conn.execute(
        f"""INSERT INTO lots (source, lot_number, {_TYPED}, attributes, row_hash, first_seen_at,
                              last_seen_at, last_changed_at, first_file_id, last_file_id)
            SELECT %(source)s, lot_number, {_TYPED}, attributes, row_hash, now(), now(), now(),
                   %(file_id)s, %(file_id)s
            FROM stage
            ON CONFLICT (source, lot_number) DO UPDATE SET
                {_TYPED_UPDATE},
                attributes      = EXCLUDED.attributes,
                last_changed_at = CASE WHEN {changed("lots", "EXCLUDED")}
                                       THEN now() ELSE lots.last_changed_at END,
                row_hash        = EXCLUDED.row_hash,
                last_seen_at    = now(),
                last_file_id    = EXCLUDED.last_file_id,
                removed_at      = NULL""",
        {"source": source, "file_id": file_id, **params},
    )

    result.rows_removed, result.removal_skipped = _mark_removed(
        conn, source, csv_file.provider.removal_scope, settings.removal_min_ratio
    )

    _update_registry(conn, file_id, csv_file, registry, result)
    _store_rejects(conn, file_id, rejects)
    result.duration_seconds = time.monotonic() - started
    return result


def _mark_removed(
    conn: Connection, source: str, scope_column: str | None, min_ratio: float
) -> tuple[int, list[str]]:
    """Mark lots absent from the export as removed, scope by scope (e.g. per yard).

    A scope counts as covered only if the export contains it and holds at least `min_ratio`
    of the lots active there; otherwise it looks truncated and is skipped. Returns the number
    of lots removed and the skipped scopes. Must run after the upsert.
    """
    # No scope column: the whole provider is a single scope.
    scope = scope_column or "''::text"
    lot_scope = f"l.{scope_column}" if scope_column else scope
    rows = conn.execute(
        f"""WITH in_file AS (
                SELECT {scope} AS scope, count(*) AS n FROM stage
                WHERE {scope} IS NOT NULL GROUP BY 1),
            active AS (
                SELECT {scope} AS scope, count(*) AS n FROM lots
                WHERE source = %(source)s AND removed_at IS NULL
                  AND {scope} IN (SELECT scope FROM in_file) GROUP BY 1)
            SELECT f.scope, f.n >= %(ratio)s * coalesce(a.n, 0) AS covered
            FROM in_file f LEFT JOIN active a USING (scope)""",
        {"source": source, "ratio": min_ratio},
    ).fetchall()
    covered = [r["scope"] for r in rows if r["covered"]]
    skipped = sorted(r["scope"] or "(all)" for r in rows if not r["covered"])
    if not covered:
        return 0, skipped
    cur = conn.execute(
        f"""UPDATE lots l SET removed_at = now()
            WHERE l.source = %(source)s AND l.removed_at IS NULL
              AND {lot_scope} = ANY(%(covered)s)
              AND NOT EXISTS (SELECT 1 FROM stage s WHERE s.lot_number = l.lot_number)""",
        {"source": source, "covered": covered},
    )
    return cur.rowcount, skipped


def _hash_changed(old: str, new: str) -> str:
    return f"{old}.row_hash <> {new}.row_hash"


def _common_columns_changed(old: str, new: str) -> str:
    return (f"EXISTS (SELECT 1 FROM unnest(%(common)s::text[]) k "
            f"WHERE {old}.attributes ->> k IS DISTINCT FROM {new}.attributes ->> k)")


def _update_registry(
    conn: Connection, file_id: int, csv_file: CsvFile, known: dict[str, str], result: LoadResult
) -> None:
    source, provider = csv_file.provider.name, csv_file.provider
    result.first_file = not known
    current = dict(zip(csv_file.header, csv_file.source_header, strict=True))
    result.columns_added = [c for c in csv_file.header if known.get(c) != "active"]
    result.columns_removed = [c for c, st in known.items() if st == "active" and c not in current]

    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO column_registry (source, normalized_name, source_name, mapped_to,
                                            status, first_file_id, last_file_id)
               VALUES (%s, %s, %s, %s, 'active', %s, %s)
               ON CONFLICT (source, normalized_name) DO UPDATE SET
                   source_name = EXCLUDED.source_name, mapped_to = EXCLUDED.mapped_to,
                   status = 'active', last_seen_at = now(), last_file_id = EXCLUDED.last_file_id""",
            [(source, norm, src, provider.mapped_to(norm), file_id, file_id)
             for norm, src in current.items()],
        )
    if result.columns_removed:
        conn.execute(
            """UPDATE column_registry SET status = 'missing'
               WHERE source = %s AND normalized_name = ANY(%s)""",
            (source, result.columns_removed),
        )


def _store_rejects(conn: Connection, file_id: int, rejects: list[RejectedRow]) -> None:
    if not rejects:
        return
    with conn.cursor() as cur, cur.copy(
        "COPY rejected_rows (file_id, line_no, reason, raw) FROM STDIN"
    ) as copy:
        for r in rejects:
            copy.write_row((file_id, r.line_no, r.reason, Jsonb(r.raw)))
