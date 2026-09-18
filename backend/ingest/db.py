from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from .config import get_settings

EVENTS_CHANNEL = "pipeline_events"


def sqlalchemy_url(url: str) -> str:
    """Alembic goes through SQLAlchemy, which needs the psycopg 3 driver spelled out."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def connect(url: str | None = None, *, autocommit: bool = True) -> psycopg.Connection:
    return psycopg.connect(
        url or get_settings().database_url, autocommit=autocommit, row_factory=dict_row
    )


def run_migrations(url: str | None = None) -> None:
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.attributes["database_url"] = url or get_settings().database_url
    command.upgrade(cfg, "head")


def notify(conn: psycopg.Connection, event: str, **payload: Any) -> None:
    """Publish a dashboard event. Inside a transaction it is only delivered on commit."""
    message = json.dumps({"type": event, **payload}, default=str)
    conn.execute("SELECT pg_notify(%s, %s)", (EVENTS_CHANNEL, message))
