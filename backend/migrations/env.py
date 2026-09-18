from alembic import context
from sqlalchemy import create_engine, pool

from ingest.config import get_settings
from ingest.db import sqlalchemy_url


def run_migrations_online() -> None:
    url = context.config.attributes.get("database_url") or get_settings().database_url
    engine = create_engine(sqlalchemy_url(url), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
