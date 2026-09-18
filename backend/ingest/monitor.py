"""Periodic health checks that no single ingest can detect: a silent worker, a missing file.

Runs in the API process, so a dead worker is still reported."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from psycopg import Connection

from .alerts import AlertSpec, raise_alert, resolve_fingerprint
from .config import Settings

WORKER_DOWN = "worker_down"


@dataclass(frozen=True)
class Finding:
    fingerprint: str
    spec: AlertSpec | None  # None: condition is clear, resolve any open alert
    source: str | None = None


def check_worker(last_beat: datetime | None, now: datetime, timeout_s: int) -> Finding:
    if last_beat is not None and now - last_beat <= timedelta(seconds=timeout_s):
        return Finding(WORKER_DOWN, None)
    title = (
        "Ingest worker has never reported a heartbeat"
        if last_beat is None
        else f"Ingest worker silent for {int((now - last_beat).total_seconds() // 60)} min"
    )
    return Finding(WORKER_DOWN, AlertSpec(
        "critical", WORKER_DOWN, title, WORKER_DOWN,
        {"last_beat_at": last_beat.isoformat() if last_beat else None},
    ))


def check_staleness(
    source: str, last_success: datetime | None, watching_since: datetime | None,
    now: datetime, stale_minutes: int,
) -> Finding:
    """`watching_since` (worker start) keeps a fresh install from alerting before any file
    could reasonably have arrived."""
    fingerprint = f"no_new_file:{source}"
    reference = last_success or watching_since
    if reference is None or now - reference <= timedelta(minutes=stale_minutes):
        return Finding(fingerprint, None, source)
    minutes = int((now - reference).total_seconds() // 60)
    what = "successful ingest" if last_success else "file ingested since the worker started"
    return Finding(fingerprint, AlertSpec(
        "critical", "no_new_file", f"No {what} for {source} in {minutes} min",
        fingerprint,
        {"last_success_at": last_success.isoformat() if last_success else None,
         "threshold_minutes": stale_minutes},
    ), source)


def run_checks(conn: Connection, settings: Settings) -> list[Finding]:
    row = conn.execute(
        """SELECT now() AS now, hb.last_beat_at AS last_beat, hb.started_at AS started
           FROM (SELECT 1) one
           LEFT JOIN LATERAL (SELECT last_beat_at, started_at FROM worker_heartbeat
                              ORDER BY last_beat_at DESC LIMIT 1) hb ON true"""
    ).fetchone()
    now, last_beat, worker_started = row["now"], row["last_beat"], row["started"]
    findings = [check_worker(last_beat, now, settings.heartbeat_timeout_seconds)]

    last = {
        r["source"]: r["last"]
        for r in conn.execute(
            # An export identical to the previous one still proves the feed is alive.
            """SELECT source, max(finished_at) AS last FROM ingest_files
               WHERE status IN ('succeeded', 'duplicate') GROUP BY source"""
        )
    }
    for source in settings.providers:
        findings.append(check_staleness(
            source, last.get(source), worker_started, now, settings.stale_after_minutes
        ))

    for f in findings:
        if f.spec is None:
            resolve_fingerprint(conn, f.fingerprint)
        else:
            raise_alert(conn, f.spec, source=f.source)
    return findings
