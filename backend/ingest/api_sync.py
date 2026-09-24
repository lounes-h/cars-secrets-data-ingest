"""Keeps the API's database in step with `lots`.

The API (cars-secrets-api) serves lots from its own database, which stays small: it holds every
active lot, and a removed lot only until its auction is surely over (`sale_at` +
`API_KEEP_AFTER_SALE_MINUTES`), with just the columns the API serves. History, `attributes`
and the ingest's bookkeeping stay in the ingest database, the source of truth.

Every write that changes a served column of a lot draws a new `lots.sync_version`, and loads
hold one global lock, so versions become visible in the order they were drawn. A sync reads
the lots above the API database's watermark from one snapshot, and applies them, the purge of
finished auctions and the new watermark in one transaction on the API database: readers never
see half a sync, and a failed sync (API database unreachable) is simply redone by the next one.
Without a watermark (new or emptied API database), or when the API table lacked a column, the
sync copies every lot the API should hold and drops the rest.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row

from .schema import CANONICAL_COLUMNS

# The API table's columns, in COPY order.
COLUMNS: tuple[tuple[str, str], ...] = (
    ("source", "text"), ("lot_number", "text"), *CANONICAL_COLUMNS,
    ("first_seen_at", "timestamptz"), ("last_changed_at", "timestamptz"),
    ("removed_at", "timestamptz"), ("predicted_final_price", "numeric"),  # pricing.py
)
_NOT_NULL = {"source", "lot_number", "first_seen_at", "last_changed_at"}
_NAMES = ", ".join(name for name, _ in COLUMNS)
_UPDATE = ", ".join(f"{name} = EXCLUDED.{name}" for name, _ in COLUMNS[2:])
_TABLE_DDL = ", ".join(
    f"{name} {sql_type}{' NOT NULL' if name in _NOT_NULL else ''}" for name, sql_type in COLUMNS
)
_STAGE_DDL = ", ".join(f"{name} {sql_type}" for name, sql_type in COLUMNS) + ", keep boolean"
# Whether the API database should hold a lot. Lots are only removed once sale_at is known and
# past (loader._mark_removed); removed_at is the fallback should that ever change.
_KEEP = ("removed_at IS NULL OR "
         "coalesce(sale_at, removed_at) + make_interval(mins => %(keep)s) > now()")
# Columns only the ingest's own `lots` has: seeing one means the URL points at that database.
_INGEST_ONLY = {"attributes", "row_hash", "sync_version"}


@dataclass(frozen=True)
class SyncResult:
    full: bool             # every lot was copied, not only the changed ones
    copied: int            # lots written or refreshed
    deleted: int           # lots dropped: auction over, or no longer to be kept
    sync_version: int      # the watermark the API database is now at
    duration_seconds: float


def ensure_schema(api: Connection) -> bool:
    """Create the API database's tables, or add columns `lots` lacks. Returns False when `lots`
    was created or extended, so every row must be copied again."""
    have = {r["column_name"] for r in api.execute(
        """SELECT column_name FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = 'lots'""")}
    if have & _INGEST_ONLY:
        raise RuntimeError("API_DATABASE_URL points at the ingest database; refusing to sync")
    if not have:
        api.execute(f"CREATE TABLE public.lots ({_TABLE_DDL}, PRIMARY KEY (source, lot_number))")
    missing = [(n, t) for n, t in COLUMNS if have and n not in have]
    for name, sql_type in missing:  # a canonical column added since; filled by the full copy
        api.execute(f"ALTER TABLE public.lots ADD COLUMN {name} {sql_type}")
    api.execute(
        """
        CREATE INDEX IF NOT EXISTS lots_listing ON public.lots (first_seen_at DESC, lot_number)
            WHERE removed_at IS NULL;
        CREATE INDEX IF NOT EXISTS lots_lot_number ON public.lots (lot_number);
        CREATE INDEX IF NOT EXISTS lots_vin ON public.lots (vin);
        CREATE INDEX IF NOT EXISTS lots_make_model ON public.lots (make, model_group);
        CREATE INDEX IF NOT EXISTS lots_removed ON public.lots (removed_at)
            WHERE removed_at IS NOT NULL;
        CREATE TABLE IF NOT EXISTS public.ingest_sync (
            id           boolean PRIMARY KEY DEFAULT true CHECK (id),  -- a single row
            sync_version bigint NOT NULL,
            synced_at    timestamptz NOT NULL
        );
        """
    )
    return bool(have) and not missing


def sync(conn: Connection, api_url: str, keep_minutes: int) -> SyncResult:
    """Bring the API database up to date. `conn` is the ingest database, in autocommit mode."""
    started = time.monotonic()
    params = {"keep": keep_minutes}
    with (psycopg.connect(api_url, autocommit=True, row_factory=dict_row,
                          connect_timeout=10) as api,
          api.transaction()):
        # Two workers never interleave their syncs.
        api.execute("SELECT pg_advisory_xact_lock(hashtext('ingest:api_sync'))")
        complete = ensure_schema(api)
        state = api.execute("SELECT sync_version FROM public.ingest_sync").fetchone()
        full = not complete or state is None
        since = 0 if full else state["sync_version"]
        copied = deleted = 0
        with conn.transaction():
            # The rows and the watermark must come from the same snapshot.
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            version = conn.execute(
                "SELECT coalesce(max(sync_version), 0) AS v FROM lots").fetchone()["v"]
            if version < since:  # the ingest database was restored or rebuilt
                full, since = True, 0
            if full or version > since:
                where = f"({_KEEP})" if full else "sync_version > %(since)s"
                api.execute(f"CREATE TEMP TABLE sync_stage ({_STAGE_DDL}) ON COMMIT DROP")
                with (conn.cursor().copy(
                          f"COPY (SELECT {_NAMES}, {_KEEP} FROM lots WHERE {where}) TO STDOUT",
                          {**params, "since": since}) as out,
                      api.cursor().copy(
                          f"COPY sync_stage ({_NAMES}, keep) FROM STDIN") as into):
                    for chunk in out:
                        into.write(chunk)
                if full:
                    api.execute("DELETE FROM public.lots")
                deleted = api.execute(
                    """DELETE FROM public.lots l USING sync_stage s
                       WHERE NOT s.keep AND l.source = s.source
                         AND l.lot_number = s.lot_number""").rowcount
                copied = api.execute(
                    f"""INSERT INTO public.lots ({_NAMES})
                        SELECT {_NAMES} FROM sync_stage WHERE keep
                        ON CONFLICT (source, lot_number) DO UPDATE SET {_UPDATE}""").rowcount
        # Removed lots whose auction ended since the last sync, changed or not.
        deleted += api.execute(
            f"DELETE FROM public.lots WHERE removed_at IS NOT NULL AND NOT ({_KEEP})",
            params).rowcount
        if full or version != since:  # an idle sync writes nothing
            api.execute(
                """INSERT INTO public.ingest_sync (sync_version, synced_at) VALUES (%s, now())
                   ON CONFLICT (id) DO UPDATE SET sync_version = EXCLUDED.sync_version,
                                                  synced_at = EXCLUDED.synced_at""",
                (version,))
    return SyncResult(full, copied, deleted, version, time.monotonic() - started)
