"""Keep the lot numbers removed by each run.

Revision ID: 0005
Revises: 0004
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NULL distinguishes old runs without a recorded list from runs with no removals.
    op.execute("ALTER TABLE ingest_files ADD COLUMN removed_lot_numbers text[]")


def downgrade() -> None:
    op.execute("ALTER TABLE ingest_files DROP COLUMN removed_lot_numbers")
