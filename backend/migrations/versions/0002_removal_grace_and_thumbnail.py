"""Defer removals until the auction is over; lot thumbnail column.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-18
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        -- Lots missing from a file whose sale (+ grace period) is not over yet: kept active.
        ALTER TABLE ingest_files ADD COLUMN rows_removal_deferred integer;
        -- Filled by the next ingest of each lot (typed columns are rewritten on every upsert).
        ALTER TABLE lots ADD COLUMN thumbnail_url text;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE lots DROP COLUMN thumbnail_url;
        ALTER TABLE ingest_files DROP COLUMN rows_removal_deferred;
        """
    )
