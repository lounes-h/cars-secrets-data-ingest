"""Processes one file end to end: register -> dedupe -> load -> alert -> archive."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import structlog
from psycopg import Connection
from psycopg.types.json import Jsonb

from . import files
from .alerts import (
    AlertSpec,
    RunFacts,
    evaluate_run,
    evaluate_schema,
    raise_alert,
    resolve_fingerprint,
)
from .config import Settings
from .db import notify
from .loader import load_file
from .parsing import CsvFile, FileRejected
from .providers import Provider

log = structlog.get_logger()


@dataclass(frozen=True)
class Outcome:
    file_id: int
    status: str  # succeeded | failed | duplicate


def mark_interrupted(conn: Connection) -> int:
    """A worker restart leaves runs in 'processing'; their transaction was rolled back."""
    cur = conn.execute(
        """UPDATE ingest_files SET status = 'interrupted', finished_at = clock_timestamp(),
                  error = 'worker stopped while processing; the file will be retried'
           WHERE status = 'processing'"""
    )
    return cur.rowcount


def process_file(conn: Connection, path: Path, provider: Provider, settings: Settings) -> Outcome:
    """`conn` must be in autocommit mode; the load itself runs in its own transaction."""
    source = provider.name
    sha = files.sha256_of(path)
    size = path.stat().st_size
    file_id = conn.execute(
        """INSERT INTO ingest_files (source, filename, sha256, size_bytes, status, started_at)
           VALUES (%s, %s, %s, %s, 'processing', now()) RETURNING id""",
        (source, path.name, sha, size),
    ).fetchone()["id"]
    notify(conn, "run_started", file_id=file_id, source=source, filename=path.name)
    log.info("ingest.started", file_id=file_id, source=source, file=path.name, bytes=size)

    already = conn.execute(
        "SELECT id FROM ingest_files WHERE source = %s AND sha256 = %s AND status = 'succeeded'",
        (source, sha),
    ).fetchone()
    if already:
        return _duplicate(conn, path, file_id, source, already["id"], settings)

    started = time.monotonic()
    try:
        with CsvFile(path, provider) as csv_file, conn.transaction():
            result = load_file(conn, file_id, csv_file, settings)
            conn.execute(
                """UPDATE ingest_files SET status = 'succeeded', finished_at = clock_timestamp(),
                          rows_total = %s, rows_new = %s, rows_updated = %s, rows_unchanged = %s,
                          rows_rejected = %s, rows_removed = %s, rows_removal_deferred = %s,
                          removal_skipped = %s, columns = %s, field_errors = %s
                   WHERE id = %s""",
                (result.rows_total, result.rows_new, result.rows_updated, result.rows_unchanged,
                 result.rows_rejected, result.rows_removed, result.rows_removal_deferred,
                 Jsonb(result.removal_skipped),
                 Jsonb(csv_file.source_header), Jsonb(result.field_errors), file_id),
            )
            facts = RunFacts(
                source=source, file_id=file_id, filename=path.name,
                rows_total=result.rows_total, rows_rejected=result.rows_rejected,
                field_errors=result.field_errors, duration_seconds=time.monotonic() - started,
                baseline_rows=result.baseline_rows, removal_skipped=result.removal_skipped,
                removal_scope_label=provider.removal_scope_label,
            )
            specs = evaluate_run(facts, settings) + evaluate_schema(
                source, first_file=result.first_file, added=result.columns_added,
                removed=result.columns_removed, filename=path.name,
            )
            for spec in specs:
                raise_alert(conn, spec, source=source, file_id=file_id)
            # Conditions this successful run clears.
            resolve_fingerprint(conn, f"no_new_file:{source}")
            for col in result.columns_added:
                resolve_fingerprint(conn, f"schema_column_removed:{source}:{col}")
            notify(conn, "run_finished", file_id=file_id, source=source, status="succeeded")
    except FileRejected as exc:
        return _fail(conn, path, file_id, source, str(exc), settings, rejected=True)
    except Exception as exc:  # noqa: BLE001 - any failure must be recorded and alerted
        log.exception("ingest.failed", file_id=file_id)
        return _fail(conn, path, file_id, source, f"{type(exc).__name__}: {exc}", settings)

    log.info("ingest.succeeded", file_id=file_id, rows=result.rows_total, new=result.rows_new,
             updated=result.rows_updated, removed=result.rows_removed,
             removal_deferred=result.rows_removal_deferred,
             rejected=result.rows_rejected, repaired=result.rows_repaired,
             seconds=round(time.monotonic() - started, 1))
    _archive(conn, path, file_id, source, settings)
    return Outcome(file_id, "succeeded")


def _archive(conn: Connection, path: Path, file_id: int, source: str, settings: Settings) -> None:
    try:
        target = files.archive(path, settings.archive_dir, source, file_id)
    except OSError as exc:
        log.exception("archive.failed", file_id=file_id)
        raise_alert(conn, AlertSpec(
            "warning", "archive_failed", f"Could not archive {path.name}: {exc}",
            f"archive_failed:{source}", {"file": path.name, "error": str(exc)},
        ), source=source, file_id=file_id)
        return
    conn.execute("UPDATE ingest_files SET archived_path = %s WHERE id = %s", (str(target), file_id))


def _duplicate(
    conn: Connection, path: Path, file_id: int, source: str, original_id: int, settings: Settings
) -> Outcome:
    conn.execute(
        """UPDATE ingest_files SET status = 'duplicate', finished_at = clock_timestamp(),
                  error = %s WHERE id = %s""",
        (f"identical to run #{original_id}", file_id),
    )
    raise_alert(conn, AlertSpec(
        "info", "duplicate_file", f"{path.name} is identical to run #{original_id}; skipped",
        f"duplicate_file:{source}:{file_id}", {"original_file_id": original_id},
    ), source=source, file_id=file_id)
    resolve_fingerprint(conn, f"no_new_file:{source}")
    notify(conn, "run_finished", file_id=file_id, source=source, status="duplicate")
    _archive(conn, path, file_id, source, settings)
    return Outcome(file_id, "duplicate")


def _fail(
    conn: Connection, path: Path, file_id: int, source: str, error: str, settings: Settings,
    *, rejected: bool = False,
) -> Outcome:
    moved: Path | None = None
    try:
        moved = files.move_to(path, settings.failed_dir, source, file_id)
    except OSError:
        log.exception("move_to_failed.failed", file_id=file_id)
    conn.execute(
        """UPDATE ingest_files SET status = 'failed', finished_at = clock_timestamp(), error = %s,
                  archived_path = %s WHERE id = %s""",
        (error, str(moved) if moved else None, file_id),
    )
    title = f"{'Rejected' if rejected else 'Failed to ingest'} {path.name}: {error}"
    raise_alert(conn, AlertSpec(
        "critical", "file_rejected" if rejected else "ingest_failed", title[:300],
        f"ingest_failed:{source}:{file_id}",
        {"file": path.name, "error": error, "moved_to": str(moved) if moved else None},
    ), source=source, file_id=file_id)
    notify(conn, "run_finished", file_id=file_id, source=source, status="failed")
    return Outcome(file_id, "failed")
