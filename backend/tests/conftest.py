"""Shared fixtures. Database tests run against a real PostgreSQL: TEST_DATABASE_URL if set,
otherwise a throwaway container started through testcontainers (needs Docker)."""

from __future__ import annotations

import csv
import os
from collections.abc import Callable, Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from ingest.config import Settings
from ingest.db import connect, run_migrations

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE = FIXTURES / "sample.csv"


@pytest.fixture(scope="session")
def db_url() -> Iterator[str]:
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        yield url
        return
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:17-alpine", driver=None) as pg:
        yield pg.get_connection_url()


@pytest.fixture
def conn(db_url: str) -> Iterator[psycopg.Connection]:
    """A freshly migrated, empty database for every test."""
    with psycopg.connect(db_url, autocommit=True) as admin:
        admin.execute("DROP SCHEMA public CASCADE")
        admin.execute("CREATE SCHEMA public")
    run_migrations(db_url)
    with connect(db_url) as c:
        yield c


@pytest.fixture
def api_url(db_url: str) -> str:
    """An empty database on the same server, standing in for the API's."""
    name = psycopg.conninfo.conninfo_to_dict(db_url)["dbname"] + "_api"
    with psycopg.connect(db_url, autocommit=True) as admin:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
            sql.Identifier(name)))
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    return psycopg.conninfo.make_conninfo(db_url, dbname=name)


@pytest.fixture
def settings(tmp_path: Path, db_url: str) -> Settings:
    return Settings(
        _env_file=None,
        database_url=db_url,
        inbox_dir=tmp_path / "inbox",
        archive_dir=tmp_path / "archive",
        failed_dir=tmp_path / "failed",
        price_model_path=tmp_path / "models" / "final_price.ubj",
        file_stable_seconds=0.2,
        dashboard_password="test-password",
        session_secret="s" * 40,
    )


def read_sample() -> tuple[list[str], list[list[str]]]:
    """Header and well-formed rows of the fixture (300 real Copart rows)."""
    with open(SAMPLE, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        rows = [r for r in reader if len(r) == len(header)]
    return header, rows


def write_copart_csv(path: Path, header: list[str], rows: list[list[str]]) -> Path:
    """Write rows the way Copart does: every field in quotes, quotes inside values NOT
    escaped (so `29'9"` becomes `"29'9""`), CRLF line ends."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        for row in [header, *rows]:
            fh.write(",".join(f'"{v}"' for v in row) + "\r\n")
    return path


def write_csv(path: Path, header: list[str], rows: list[list[str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)
    return path


@pytest.fixture
def make_export(settings: Settings) -> Callable[..., Path]:
    """Write an export into inbox/<source>/ (defaults: the sample's header and rows)."""
    counter = iter(range(1, 1000))

    def make(
        rows: list[list[str]] | None = None, header: list[str] | None = None,
        name: str | None = None, source: str = "copart",
    ) -> Path:
        sample_header, sample_rows = read_sample()
        path = settings.inbox_dir / source / (name or f"export_{next(counter)}.csv")
        return write_csv(path, header or sample_header,
                         sample_rows if rows is None else rows)

    return make
