"""Dashboard API: pipeline status, runs, alerts, schema registry, lot explorer with AI damage
analyses, live events."""

import asyncio
import contextlib
import shutil
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal

import psycopg
import structlog
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict, Field

from .. import ai, vehicle_analysis
from ..config import Settings, get_settings
from ..db import EVENTS_CHANNEL, notify
from ..log_config import configure_logging
from ..monitor import downloader_status, run_checks
from ..providers import PROVIDERS
from . import auth

log = structlog.get_logger()

LOT_COLUMNS = """source, lot_number, year, make, model_group, model_detail, trim, vin,
    damage_primary, damage_secondary, odometer, est_retail_value, repair_cost, high_bid,
    buy_it_now_price, currency, sale_status, sale_at, yard_code, yard_name, location_city,
    location_state, thumbnail_url, first_seen_at, last_seen_at, last_changed_at, removed_at"""
# Whether the signed-in user (%(user)s) marked the lot as favorite.
IS_FAVORITE = """EXISTS (SELECT 1 FROM lot_favorites f WHERE f.username = %(user)s
    AND f.source = lots.source AND f.lot_number = lots.lot_number) AS favorite"""


# Lots page filters: query parameter -> condition on `lots`. Makes and yards are matched
# whatever their case (the export writes them in capitals); a model matches by its start.
LOT_FILTERS = {
    "make": "make = upper(%(make)s)",
    "model": "model_group ILIKE %(model)s",
    "year_min": "year >= %(year_min)s",
    "year_max": "year <= %(year_max)s",
    "damage": "damage_primary = %(damage)s",
    "location_state": "location_state = %(location_state)s",
    "yard": "yard_name = upper(%(yard)s)",
    "sale_from": "sale_date >= %(sale_from)s",
    "sale_to": "sale_date <= %(sale_to)s",
    "keys": "has_keys = %(keys)s",
    "runs": "runs_drives = %(runs)s",
    "odometer_max": "odometer <= %(odometer_max)s",
}


class LotQuery(BaseModel):
    """The Lots page's query: search, state, paging and filters (see LOT_FILTERS)."""

    q: str = ""
    source: str | None = None
    state: Literal["active", "removed", "all"] = "all"
    limit: int = Field(50, ge=1, le=200)
    offset: int = Field(0, ge=0)
    favorites: bool = False  # only the signed-in user's favorites
    make: str | None = None
    model: str | None = None
    year_min: int | None = None
    year_max: int | None = None
    damage: str | None = None
    location_state: str | None = None
    yard: str | None = None
    sale_from: date | None = None
    sale_to: date | None = None
    keys: str | None = None
    runs: str | None = None
    odometer_max: int | None = None


def like_prefix(text: str) -> str:
    """A LIKE pattern matching values that start with `text`, taken literally."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class TTLCache:
    """Small in-process cache (photo lists change rarely; Copart is not called per page view)."""

    def __init__(self, ttl_seconds: float, max_items: int = 500, clock=time.monotonic):
        self.ttl, self.max_items, self.clock = ttl_seconds, max_items, clock
        self._items: dict[Any, tuple[float, Any]] = {}

    def get(self, key: Any) -> Any | None:
        hit = self._items.get(key)
        if hit is None or hit[0] < self.clock():
            self._items.pop(key, None)
            return None
        return hit[1]

    def put(self, key: Any, value: Any) -> None:
        if len(self._items) >= self.max_items:
            self._items.pop(next(iter(self._items)))  # oldest insertion
        self._items[key] = (self.clock() + self.ttl, value)


class EventBroker:
    """Fans Postgres NOTIFY events out to every open SSE stream."""

    def __init__(self) -> None:
        self.subscribers: set[asyncio.Queue[str]] = set()

    def publish(self, payload: str) -> None:
        for q in list(self.subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(payload)

    async def listen(self, url: str) -> None:
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
                    await conn.execute(f"LISTEN {EVENTS_CHANNEL}")
                    async for note in conn.notifies():
                        self.publish(note.payload)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - reconnect whatever went wrong
                log.exception("events.listen_failed")
                await asyncio.sleep(3)


async def monitor_loop(pool: ConnectionPool, settings: Settings) -> None:
    def check(after_sleep: bool) -> None:
        with pool.connection() as conn:
            run_checks(conn, settings, after_sleep=after_sleep)

    last = None
    while True:
        # A long gap between rounds means the machine slept (Docker sleeps with the Mac).
        now = time.time()
        after_sleep = last is not None and now - last > 3 * settings.monitor_interval_seconds
        last = now
        try:
            await asyncio.to_thread(check, after_sleep)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("monitor.failed")
        await asyncio.sleep(settings.monitor_interval_seconds)


def create_app(settings: Settings | None = None, *, background: bool = True) -> FastAPI:
    settings = settings or get_settings()
    auth.check_auth_settings(settings)
    pool = ConnectionPool(
        settings.database_url, min_size=1, max_size=10, open=False,
        kwargs={"autocommit": True, "row_factory": dict_row},
    )
    broker = EventBroker()
    limiter = auth.LoginRateLimiter()
    image_cache = TTLCache(ttl_seconds=15 * 60)
    filter_cache = TTLCache(ttl_seconds=15 * 60)
    model_list_cache = TTLCache(ttl_seconds=15 * 60)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        configure_logging()
        pool.open()
        tasks = []
        if background:
            tasks = [asyncio.create_task(broker.listen(settings.database_url)),
                     asyncio.create_task(monitor_loop(pool, settings))]
        yield
        for t in tasks:
            t.cancel()
        pool.close()

    app = FastAPI(title="Ingest dashboard API", lifespan=lifespan, docs_url="/api/docs",
                  openapi_url="/api/openapi.json")
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.pool, app.state.broker = pool, broker

    def db() -> Iterator[psycopg.Connection]:
        with pool.connection() as conn:
            yield conn

    Conn = Annotated[psycopg.Connection, Depends(db)]
    User = Annotated[str, Depends(auth.current_user)]

    public = APIRouter(prefix="/api")
    api = APIRouter(prefix="/api", dependencies=[Depends(auth.current_user)])

    # --- auth ----------------------------------------------------------------------------------
    class Login(BaseModel):
        username: str
        password: str

    @public.post("/auth/login")
    def login(body: Login, request: Request, response: Response) -> dict[str, str]:
        client = request.client.host if request.client else "unknown"
        if limiter.blocked(client):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many failed attempts")
        if not auth.credentials_valid(body.username, body.password, settings):
            limiter.failure(client)
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
        limiter.reset(client)
        response.set_cookie(
            auth.COOKIE_NAME, auth.issue_token(body.username, settings),
            max_age=settings.session_max_age_seconds, httponly=True, samesite="strict",
            secure=settings.cookie_secure, path="/",
        )
        return {"username": body.username}

    @public.post("/auth/logout", status_code=204)
    def logout(response: Response) -> None:
        response.delete_cookie(auth.COOKIE_NAME, path="/")

    @api.get("/auth/me")
    def me(user: User) -> dict[str, str]:
        return {"username": user}

    @public.get("/health")
    def health(conn: Conn) -> dict[str, str]:
        conn.execute("SELECT 1")
        return {"status": "ok"}

    # --- overview --------------------------------------------------------------------------------
    @api.get("/overview")
    def overview(conn: Conn) -> dict[str, Any]:
        worker = conn.execute(
            """SELECT worker_id, state, started_at, last_beat_at,
                      last_beat_at > now() - make_interval(secs => %s) AS healthy
               FROM worker_heartbeat ORDER BY last_beat_at DESC LIMIT 1""",
            (settings.heartbeat_timeout_seconds,),
        ).fetchone()
        alerts = {r["severity"]: r["n"] for r in conn.execute(
            "SELECT severity, count(*) AS n FROM alerts WHERE resolved_at IS NULL GROUP BY 1"
        )}
        lots = conn.execute(
            """SELECT count(*) FILTER (WHERE removed_at IS NULL)                     AS active,
                      count(*) FILTER (WHERE removed_at IS NOT NULL)                 AS removed,
                      count(*) FILTER (WHERE first_seen_at > now() - interval '24h') AS new_24h,
                      count(*) FILTER (WHERE removed_at > now() - interval '24h')    AS removed_24h,
                      (SELECT count(DISTINCT (source, lot_number)) FROM lot_versions
                       WHERE valid_to > now() - interval '24h')                     AS changed_24h
               FROM lots"""
        ).fetchone()
        sources = sorted(set(settings.providers) | {
            r["source"] for r in conn.execute("SELECT DISTINCT source FROM ingest_files")
        })
        providers = []
        for source in sources:
            last_run = conn.execute(
                """SELECT id, filename, status, received_at, finished_at, rows_total, rows_new,
                          rows_updated, rows_removed, rows_rejected
                   FROM ingest_files WHERE source = %s ORDER BY id DESC LIMIT 1""",
                (source,),
            ).fetchone()
            last_success = conn.execute(
                """SELECT max(finished_at) AS at FROM ingest_files
                   WHERE source = %s AND status = 'succeeded'""",
                (source,),
            ).fetchone()["at"]
            active = conn.execute(
                "SELECT count(*) AS n FROM lots WHERE source = %s AND removed_at IS NULL",
                (source,),
            ).fetchone()["n"]
            providers.append({
                "source": source,
                "display_name": PROVIDERS[source].display_name if source in PROVIDERS else source,
                "last_run": last_run, "last_success_at": last_success, "lots_active": active,
                "stale_after_minutes": settings.stale_after_minutes,
            })
        dl = downloader_summary(conn)
        downloader_ok = dl["status"] in ("scheduled", "downloading", "requested")
        health = ("down" if alerts.get("critical") or not (worker and worker["healthy"])
                  or (dl["installed"] and not downloader_ok)
                  else "degraded" if alerts.get("warning") else "healthy")
        return {
            "health": health, "worker": worker, "providers": providers, "lots": lots,
            "downloader": {k: dl.get(k) for k in (
                "status", "installed", "heartbeat_at", "next_run_at", "last_success_at",
                "needs_attention", "run_requested_at")},
            "alerts_open": {s: alerts.get(s, 0) for s in ("critical", "warning", "info")},
            "now": conn.execute("SELECT now() AS now").fetchone()["now"],
        }

    # --- runs ------------------------------------------------------------------------------------
    RUN_COLUMNS = """id, source, filename, sha256, size_bytes, status, received_at, started_at,
        finished_at, extract(epoch FROM finished_at - started_at)::float AS duration_seconds,
        rows_total, rows_new, rows_updated, rows_unchanged, rows_rejected, rows_removed,
        removal_skipped, error"""
    RUN_LOT_COLUMNS = """l.source, l.lot_number, l.year, l.make, l.model_group, l.trim, l.vin,
        l.sale_status, l.sale_at, l.yard_name, l.removed_at"""
    REMOVAL_REASON = (
        "Missing from this export, and its sale time plus the "
        f"{settings.removal_grace_minutes}-minute grace period had passed, so bidding was over."
    )

    @api.get("/runs")
    def runs(
        conn: Conn, source: str | None = None,
        status_: Annotated[str | None, Query(alias="status")] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 50, offset: Annotated[int, Query(ge=0)] = 0,
        downloads: bool = False,
    ) -> dict[str, Any]:
        """Ingested files, newest first; with `downloads`, also the downloads that delivered no
        file (missed, skipped, failed, paused: `download_issues`), as `kind` 'download' rows."""
        where = "WHERE (%(source)s::text IS NULL OR source = %(source)s) " \
                "AND (%(status)s::text IS NULL OR status = %(status)s)"
        params = {"source": source, "status": status_, "end": offset + limit}
        items = conn.execute(
            f"SELECT 'file' AS kind, {RUN_COLUMNS} FROM ingest_files {where} "
            "ORDER BY id DESC LIMIT %(end)s", params,
        ).fetchall()
        total = conn.execute(
            f"SELECT count(*) AS n FROM ingest_files {where}", params).fetchone()["n"]
        if downloads:
            items += conn.execute(
                "SELECT 'download' AS kind, id, source, status, at AS received_at, "
                f"message AS error FROM download_issues {where} "
                "ORDER BY at DESC, id DESC LIMIT %(end)s", params,
            ).fetchall()
            total += conn.execute(
                f"SELECT count(*) AS n FROM download_issues {where}", params).fetchone()["n"]
            items.sort(key=lambda r: r["received_at"], reverse=True)
        return {"items": items[offset:offset + limit], "total": total}

    @api.get("/runs/{run_id}")
    def run_detail(run_id: int, conn: Conn) -> dict[str, Any]:
        run = conn.execute(
            f"SELECT {RUN_COLUMNS}, columns, field_errors, archived_path "
            "FROM ingest_files WHERE id = %s", (run_id,),
        ).fetchone()
        if run is None:
            raise HTTPException(404, "run not found")
        alerts = conn.execute(
            """SELECT id, severity, type, title, created_at, resolved_at FROM alerts
               WHERE file_id = %s ORDER BY id""", (run_id,),
        ).fetchall()
        return {"run": run, "alerts": alerts}

    def removed_lot_numbers(conn: psycopg.Connection, run: dict) -> list[str] | None:
        """The lots a run removed, or None when the list cannot be established."""
        if run["removed_lot_numbers"] is not None or not run["rows_removed"]:
            return run["removed_lot_numbers"] or []
        # Older runs saved only counts. Recover a list only when every removal can
        # be attributed to this run alone; reappearances can erase that evidence.
        removed = conn.execute(
            """SELECT lot_number FROM lots l
               WHERE l.source = %(source)s
                 AND l.removed_at BETWEEN %(started_at)s AND %(finished_at)s
                 AND NOT EXISTS (
                     SELECT 1 FROM ingest_files f
                     WHERE f.source = l.source AND f.id <> %(id)s
                       AND f.status = 'succeeded'
                       AND l.removed_at BETWEEN f.started_at AND f.finished_at)
               ORDER BY lot_number""", run,
        ).fetchall()
        if len(removed) != run["rows_removed"]:
            return None
        return [lot["lot_number"] for lot in removed]

    @api.get("/runs/{run_id}/rows")
    def run_rows(
        run_id: int, conn: Conn, kind: Literal["new", "updated", "removed", "rejected"],
        limit: Annotated[int, Query(ge=1, le=200)] = 50, offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, Any]:
        """One page of the lots a run created, changed or removed, or of its rejected rows.

        `total` comes from the counts the load transaction wrote, except for rejected rows:
        only the first `MAX_STORED_REJECTS` of those are kept, so they are counted.
        `available` is false only when an older run's removed lots cannot be reconstructed.
        """
        run = conn.execute(
            """SELECT id, source, started_at, finished_at, rows_new, rows_updated, rows_removed,
                      removed_lot_numbers FROM ingest_files WHERE id = %s""", (run_id,),
        ).fetchone()
        if run is None:
            raise HTTPException(404, "run not found")
        page = {"limit": limit, "offset": offset, "id": run_id, "source": run["source"]}
        if kind == "rejected":
            items = conn.execute(
                """SELECT line_no, reason, raw FROM rejected_rows WHERE file_id = %(id)s
                   ORDER BY line_no LIMIT %(limit)s OFFSET %(offset)s""", page,
            ).fetchall()
            total = conn.execute(
                "SELECT count(*) AS n FROM rejected_rows WHERE file_id = %s", (run_id,),
            ).fetchone()["n"]
            return {"items": items, "total": total, "available": True, "reason": None}
        if kind == "new":
            items = conn.execute(
                f"""SELECT {RUN_LOT_COLUMNS} FROM lots l WHERE l.first_file_id = %(id)s
                    ORDER BY l.lot_number LIMIT %(limit)s OFFSET %(offset)s""", page,
            ).fetchall()
            total = run["rows_new"]
        elif kind == "updated":
            # One lot_versions row is written per lot this run changed.
            items = conn.execute(
                f"""SELECT {RUN_LOT_COLUMNS} FROM lot_versions v
                    JOIN lots l ON l.source = v.source AND l.lot_number = v.lot_number
                    WHERE v.superseded_by_file_id = %(id)s
                    ORDER BY v.lot_number LIMIT %(limit)s OFFSET %(offset)s""", page,
            ).fetchall()
            total = run["rows_updated"]
        else:
            numbers = removed_lot_numbers(conn, run)
            if numbers is None:
                return {"items": [], "total": run["rows_removed"] or 0, "available": False,
                        "reason": REMOVAL_REASON}
            items = conn.execute(
                f"""SELECT {RUN_LOT_COLUMNS} FROM lots l
                    WHERE l.source = %(source)s AND l.lot_number = ANY(%(page)s)
                    ORDER BY l.lot_number""",
                {**page, "page": numbers[offset:offset + limit]},
            ).fetchall()
            total = len(numbers)
        return {"items": items, "total": total or 0, "available": True,
                "reason": REMOVAL_REASON if kind == "removed" else None}

    @api.post("/runs/{run_id}/retry", status_code=202)
    def retry_run(run_id: int, conn: Conn) -> dict[str, str]:
        """Put a failed file back into its inbox; the worker picks it up again."""
        run = conn.execute(
            "SELECT source, filename, status, archived_path FROM ingest_files WHERE id = %s",
            (run_id,),
        ).fetchone()
        if run is None:
            raise HTTPException(404, "run not found")
        if run["status"] != "failed" or not run["archived_path"]:
            raise HTTPException(409, "only failed runs whose file was kept can be retried")
        failed_root = settings.failed_dir.resolve()
        src = Path(run["archived_path"]).resolve()
        if not src.is_relative_to(failed_root) or not src.is_file():
            raise HTTPException(409, "the failed file is no longer available")
        if run["source"] not in PROVIDERS:
            raise HTTPException(409, f"unknown provider {run['source']!r}")
        target = settings.inbox_dir / run["source"] / Path(run["filename"]).name
        if target.exists():
            raise HTTPException(409, f"{target.name} is already waiting in the inbox")
        shutil.move(str(src), target)
        conn.execute("UPDATE ingest_files SET archived_path = NULL WHERE id = %s", (run_id,))
        return {"status": "requeued", "file": target.name}

    # --- downloader ------------------------------------------------------------------------------
    def downloader_summary(conn: psycopg.Connection) -> dict[str, Any]:
        now = conn.execute("SELECT now() AS now").fetchone()["now"]
        row = conn.execute(
            "SELECT * FROM downloader_status WHERE source = 'copart'").fetchone()
        status = downloader_status(row, now, settings.downloader_timeout_seconds)
        return {"now": now, "status": status, "installed": row is not None, **(row or {})}

    @api.get("/downloader")
    def downloader(conn: Conn) -> dict[str, Any]:
        """What the host-side downloader is doing and when it downloads next."""
        return downloader_summary(conn)

    @api.post("/downloader/run", status_code=202)
    def downloader_run(conn: Conn, user: User) -> dict[str, Any]:
        """Ask the downloader to download now; it picks the request up within seconds."""
        summary = downloader_summary(conn)
        if not summary["installed"]:
            raise HTTPException(409, "the downloader is not set up on this machine")
        if summary["status"] == "paused":
            raise HTTPException(409, "downloads are paused until you log in to Copart again")
        if summary["status"] == "downloading":
            raise HTTPException(409, "a download is already in progress")
        conn.execute(
            """UPDATE downloader_status SET run_requested_at = now(), run_requested_by = %s
               WHERE source = 'copart'""", (user,))
        conn.execute("SELECT pg_notify(%s, %s)", (EVENTS_CHANNEL, '{"type":"downloader"}'))
        return downloader_summary(conn)

    # --- alerts ----------------------------------------------------------------------------------
    @api.get("/alerts")
    def alerts(
        conn: Conn, state: Literal["open", "resolved", "all"] = "open",
        severity: Literal["info", "warning", "critical"] | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100, offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, Any]:
        where = ["TRUE"]
        if state == "open":
            where.append("resolved_at IS NULL")
        elif state == "resolved":
            where.append("resolved_at IS NOT NULL")
        if severity:
            where.append("severity = %(severity)s")
        clause = " AND ".join(where)
        params = {"severity": severity, "limit": limit, "offset": offset}
        items = conn.execute(
            f"""SELECT * FROM alerts WHERE {clause}
                ORDER BY resolved_at IS NULL DESC,
                         array_position(ARRAY['critical','warning','info'], severity),
                         last_seen_at DESC
                LIMIT %(limit)s OFFSET %(offset)s""", params,
        ).fetchall()
        total = conn.execute(f"SELECT count(*) AS n FROM alerts WHERE {clause}", params).fetchone()
        return {"items": items, "total": total["n"]}

    def _alert_action(conn: psycopg.Connection, alert_id: int, sql: str, user: str) -> dict:
        row = conn.execute(sql, (user, alert_id)).fetchone()
        if row is None:
            raise HTTPException(404, "alert not found or already resolved")
        conn.execute("SELECT pg_notify(%s, %s)", (EVENTS_CHANNEL, '{"type":"alert_updated"}'))
        return row

    @api.post("/alerts/{alert_id}/ack")
    def ack_alert(alert_id: int, conn: Conn, user: User) -> dict:
        return _alert_action(conn, alert_id, """UPDATE alerts SET acked_at = now(), acked_by = %s
            WHERE id = %s AND resolved_at IS NULL RETURNING *""", user)

    @api.post("/alerts/{alert_id}/resolve")
    def resolve_alert(alert_id: int, conn: Conn, user: User) -> dict:
        return _alert_action(conn, alert_id, """UPDATE alerts SET resolved_at = now(),
            resolved_by = %s WHERE id = %s AND resolved_at IS NULL RETURNING *""", user)

    # --- schema ----------------------------------------------------------------------------------
    @api.get("/schema")
    def schema(conn: Conn, source: str | None = None) -> dict[str, Any]:
        items = conn.execute(
            """SELECT * FROM column_registry WHERE (%(s)s::text IS NULL OR source = %(s)s)
               ORDER BY source, status DESC, first_seen_at, normalized_name""", {"s": source},
        ).fetchall()
        return {"items": items}

    # --- lots ------------------------------------------------------------------------------------
    @api.get("/lots")
    def lots(conn: Conn, user: User, query: Annotated[LotQuery, Query()]) -> dict[str, Any]:
        """One page of lots, most recently changed first, and how many match in all."""
        q, source, state = query.q, query.source, query.state
        where = ["(%(source)s::text IS NULL OR source = %(source)s)"]
        if query.favorites:  # only the lots the user marked
            where.append("""(source, lot_number) IN (SELECT source, lot_number
                             FROM lot_favorites WHERE username = %(user)s)""")
        if state == "active":
            where.append("removed_at IS NULL")
        elif state == "removed":
            where.append("removed_at IS NOT NULL")
        q = q.strip()
        if q:
            where.append("""(lot_number = %(q)s OR vin = upper(%(q)s)
                             OR make ILIKE %(prefix)s OR model_group ILIKE %(prefix)s)""")
        given = {k: v.strip() if isinstance(v, str) else v
                 for k, v in query.model_dump(include=set(LOT_FILTERS)).items() if v is not None}
        given = {k: v for k, v in given.items() if v != ""}
        if "model" in given:
            given["model"] = like_prefix(given["model"])
        where += [LOT_FILTERS[k] for k in given]
        clause = " AND ".join(where)
        params = {"source": source, "q": q, "prefix": like_prefix(q),
                  "limit": query.limit, "offset": query.offset, "user": user, **given}
        items = conn.execute(
            f"""SELECT {LOT_COLUMNS}, {IS_FAVORITE} FROM lots WHERE {clause}
                ORDER BY last_changed_at DESC LIMIT %(limit)s OFFSET %(offset)s""", params,
        ).fetchall()
        total = conn.execute(f"SELECT count(*) AS n FROM lots WHERE {clause}", params).fetchone()
        return {"items": items, "total": total["n"]}

    @api.get("/lot-filters")
    def lot_filters(conn: Conn) -> dict[str, list[str]]:
        """The values the Lots page's filters offer: those of the active lots (cached; one
        pass over the table takes a few hundred ms)."""
        options = filter_cache.get("options")
        if options is None:
            options = conn.execute(
                """SELECT coalesce(array_agg(DISTINCT make ORDER BY make)
                                   FILTER (WHERE make <> ''), '{}') AS makes,
                          coalesce(array_agg(DISTINCT damage_primary ORDER BY damage_primary)
                                   FILTER (WHERE damage_primary <> ''), '{}') AS damages,
                          coalesce(array_agg(DISTINCT location_state ORDER BY location_state)
                                   FILTER (WHERE location_state <> ''), '{}') AS states,
                          coalesce(array_agg(DISTINCT yard_name ORDER BY yard_name)
                                   FILTER (WHERE yard_name <> ''), '{}') AS yards,
                          coalesce(array_agg(DISTINCT has_keys ORDER BY has_keys)
                                   FILTER (WHERE has_keys <> ''), '{}') AS keys,
                          coalesce(array_agg(DISTINCT runs_drives ORDER BY runs_drives)
                                   FILTER (WHERE runs_drives <> ''), '{}') AS runs
                   FROM lots WHERE removed_at IS NULL""").fetchone()
            filter_cache.put("options", options)
        return options

    @api.get("/lots/{source}/{lot_number}")
    def lot_detail(source: str, lot_number: str, conn: Conn, user: User) -> dict[str, Any]:
        lot = conn.execute(
            f"""SELECT {LOT_COLUMNS}, {IS_FAVORITE}, attributes, first_file_id, last_file_id,
                       predicted_final_price
                FROM lots WHERE source = %(source)s AND lot_number = %(lot_number)s""",
            {"source": source, "lot_number": lot_number, "user": user},
        ).fetchone()
        if lot is None:
            raise HTTPException(404, "lot not found")
        versions = conn.execute(
            """SELECT id, valid_from, valid_to, attributes, file_id, superseded_by_file_id
               FROM lot_versions WHERE source = %s AND lot_number = %s
               ORDER BY valid_to DESC LIMIT 200""", (source, lot_number),
        ).fetchall()
        history, newer = [], lot["attributes"]
        for v in versions:
            history.append({
                "changed_at": v["valid_to"], "file_id": v["superseded_by_file_id"],
                "changes": diff_attributes(v["attributes"], newer),
            })
            newer = v["attributes"]
        provider = PROVIDERS.get(source)
        lot["listing_url"] = (provider.listing_url(lot_number)
                              if provider and provider.listing_url else None)
        return {"lot": lot, "history": history}

    @api.put("/lots/{source}/{lot_number}/favorite", status_code=204)
    def add_favorite(source: str, lot_number: str, conn: Conn, user: User) -> None:
        """Mark the lot as a favorite of the signed-in user (again: no change)."""
        try:
            conn.execute(
                """INSERT INTO lot_favorites (username, source, lot_number) VALUES (%s, %s, %s)
                   ON CONFLICT DO NOTHING""", (user, source, lot_number))
        except psycopg.errors.ForeignKeyViolation:
            raise HTTPException(404, "lot not found") from None

    @api.delete("/lots/{source}/{lot_number}/favorite", status_code=204)
    def remove_favorite(source: str, lot_number: str, conn: Conn, user: User) -> None:
        conn.execute(
            "DELETE FROM lot_favorites WHERE username = %s AND source = %s AND lot_number = %s",
            (user, source, lot_number))

    @api.get("/lots/{source}/{lot_number}/images")
    def lot_images(source: str, lot_number: str, conn: Conn) -> dict[str, Any]:
        """Photos of a lot. The provider is asked with the URLs stored for the lot -- never
        with anything the client sends -- and only known image hosts are returned."""
        lot = conn.execute(
            """SELECT lot_number, image_url, thumbnail_url FROM lots
               WHERE source = %s AND lot_number = %s""", (source, lot_number),
        ).fetchone()
        if lot is None:
            raise HTTPException(404, "lot not found")
        provider = PROVIDERS.get(source)
        if provider is None or provider.fetch_images is None:
            return {"images": [], "complete": False}
        key = (source, lot_number, lot["image_url"], lot["thumbnail_url"])
        result = image_cache.get(key)
        if result is None:
            result = asdict(provider.fetch_images(lot))
            image_cache.put(key, result)
        return result

    # --- AI damage analysis (run by the worker, vehicle_analysis.py) ----------------------------
    ANALYSIS_COLUMNS = """id, source, lot_number, status, language, requested_by, provider,
        model, prompt_version, schema_version, images, image_count, result, usage, request,
        error, created_at, started_at, completed_at, updated_at"""

    def lot_exists(conn: psycopg.Connection, source: str, lot_number: str) -> None:
        if conn.execute("SELECT 1 FROM lots WHERE source = %s AND lot_number = %s",
                        (source, lot_number)).fetchone() is None:
            raise HTTPException(404, "lot not found")

    # the history's columns: no photos, result or request, which only the analysis itself needs
    ANALYSIS_SUMMARY = """id, source, lot_number, status, language, requested_by, provider,
        model, prompt_version, schema_version, image_count, usage, error, created_at,
        started_at, completed_at, updated_at"""

    @api.get("/lots/{source}/{lot_number}/analyses")
    def lot_analyses(source: str, lot_number: str, conn: Conn) -> dict[str, Any]:
        """Every analysis of the lot, newest first, without their photos, result or request."""
        lot_exists(conn, source, lot_number)
        return {"analyses": conn.execute(
            f"""SELECT {ANALYSIS_SUMMARY} FROM vehicle_analyses
                WHERE source = %s AND lot_number = %s ORDER BY id DESC""",
            (source, lot_number)).fetchall()}

    @api.get("/lots/{source}/{lot_number}/analyses/{analysis_id}")
    def lot_analysis(source: str, lot_number: str, analysis_id: int,
                     conn: Conn) -> dict[str, Any]:
        row = conn.execute(
            f"""SELECT {ANALYSIS_COLUMNS} FROM vehicle_analyses
                WHERE id = %s AND source = %s AND lot_number = %s""",
            (analysis_id, source, lot_number)).fetchone()
        if row is None:
            raise HTTPException(404, "analysis not found")
        return row

    ADDED_MODEL = "id, provider, label, note, input_usd, output_usd, true AS custom"

    @api.get("/analysis-models")
    def analysis_models(conn: Conn) -> dict[str, Any]:
        """The models an analysis may use, with their prices, and the default (AI_MODEL): the
        built-in ones, then those people added (`custom`, which they may change)."""
        added = conn.execute(
            f"SELECT {ADDED_MODEL} FROM ai_models ORDER BY created_at, id").fetchall()
        return {"default": settings.ai_model,
                "models": [{"id": k, **asdict(m), "custom": False} for k, m in ai.MODELS.items()]
                + added}

    @api.get("/analysis-models/available")
    def available_models(provider: str) -> dict[str, Any]:
        """The provider's models, offered when a person adds one (cached)."""
        if provider not in ai.MODEL_LISTS:
            raise HTTPException(422, f"unknown provider {provider!r}")
        models = model_list_cache.get(provider)
        if models is None:
            try:
                models = ai.MODEL_LISTS[provider](settings)
            except ai.AIError as exc:
                raise HTTPException(502, f"could not list the models: {exc}") from None
            model_list_cache.put(provider, models)
        return {"models": models}

    class AddedModel(BaseModel):
        model_config = ConfigDict(str_strip_whitespace=True)

        id: str = Field(max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
        provider: str
        label: str = Field(min_length=1, max_length=100)
        note: str = Field("", max_length=300)
        input_usd: float = Field(ge=0, le=10_000)  # per million tokens
        output_usd: float = Field(ge=0, le=10_000)

    def save_model(conn: psycopg.Connection, sql: str, body: AddedModel,
                   **params: Any) -> dict[str, Any] | None:
        if body.provider not in ai.CLIENTS:
            raise HTTPException(422, f"unknown provider {body.provider!r}")
        taken = HTTPException(409, f"the model {body.id!r} already exists")
        if body.id in ai.MODELS:
            raise taken
        try:
            return conn.execute(sql, {**body.model_dump(), **params}).fetchone()
        except psycopg.errors.UniqueViolation:
            raise taken from None

    @api.post("/analysis-models", status_code=201)
    def add_model(body: AddedModel, conn: Conn, user: User) -> dict[str, Any]:
        return save_model(conn, f"""
            INSERT INTO ai_models (id, provider, label, note, input_usd, output_usd, created_by)
            VALUES (%(id)s, %(provider)s, %(label)s, %(note)s, %(input_usd)s, %(output_usd)s,
                    %(user)s)
            RETURNING {ADDED_MODEL}""", body, user=user)

    @api.put("/analysis-models/{model_id}")
    def change_model(model_id: str, body: AddedModel, conn: Conn) -> dict[str, Any]:
        """Past analyses keep the id and cost they ran with."""
        row = save_model(conn, f"""
            UPDATE ai_models SET id = %(id)s, provider = %(provider)s, label = %(label)s,
                note = %(note)s, input_usd = %(input_usd)s, output_usd = %(output_usd)s,
                updated_at = clock_timestamp()
            WHERE id = %(model_id)s RETURNING {ADDED_MODEL}""", body, model_id=model_id)
        if row is None:
            raise HTTPException(404, "no such added model")
        return row

    @api.delete("/analysis-models/{model_id}", status_code=204)
    def delete_model(model_id: str, conn: Conn) -> None:
        if conn.execute("DELETE FROM ai_models WHERE id = %s", (model_id,)).rowcount == 0:
            raise HTTPException(404, "no such added model")

    class AnalysisRequest(BaseModel):
        language: str | None = None  # default: AI_LANGUAGE
        model: str | None = None  # default: AI_MODEL; one of ai.MODELS or an added model

    @api.post("/lots/{source}/{lot_number}/analyses", status_code=202)
    def request_analysis(source: str, lot_number: str, conn: Conn, user: User,
                         body: AnalysisRequest | None = None) -> dict[str, Any]:
        """Queue a new analysis of the lot; the worker starts it within seconds."""
        language = (body.language if body else None) or settings.ai_language
        if language not in vehicle_analysis.LANGUAGES:
            raise HTTPException(422, f"unsupported language {language!r}")
        model = (body.model if body else None) or settings.ai_model
        # a client never picks an unlisted (unpriced) model; an added one runs on its provider,
        # a built-in one on the worker's AI_PROVIDER
        added = conn.execute("SELECT provider FROM ai_models WHERE id = %s",
                             (model,)).fetchone()
        if model not in ai.MODELS and added is None:
            raise HTTPException(422, f"unsupported model {model!r}")
        lot_exists(conn, source, lot_number)
        try:
            row = conn.execute(
                f"""INSERT INTO vehicle_analyses (source, lot_number, language, model, provider,
                                                  requested_by)
                    VALUES (%s, %s, %s, %s, %s, %s) RETURNING {ANALYSIS_COLUMNS}""",
                (source, lot_number, language, model, added and added["provider"],
                 user)).fetchone()
        except psycopg.errors.UniqueViolation:
            raise HTTPException(409, "an analysis of this lot is already in progress") from None
        notify(conn, "analysis", id=row["id"], source=source, lot_number=lot_number,
               status=row["status"])
        return row

    # --- live events -----------------------------------------------------------------------------
    @api.get("/events")
    async def events(request: Request) -> StreamingResponse:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=100)
        broker.subscribers.add(queue)

        async def stream() -> AsyncIterator[str]:
            try:
                yield "retry: 3000\n\n"
                while not await request.is_disconnected():
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=15)
                        yield f"data: {payload}\n\n"
                    except TimeoutError:
                        yield ": keep-alive\n\n"
            finally:
                broker.subscribers.discard(queue)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
        })

    app.include_router(public)
    app.include_router(api)
    return app


def diff_attributes(old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
    """Field-level changes between two versions of a lot's attributes."""
    return [
        {"field": k, "old": old.get(k), "new": new.get(k)}
        for k in sorted(old.keys() | new.keys())
        if old.get(k) != new.get(k)
    ]


def app_factory() -> FastAPI:  # uvicorn --factory entrypoint
    return create_app()
