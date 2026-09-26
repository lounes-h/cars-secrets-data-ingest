"""The price a lot actually sold for, from apiauctions (apiauctions_import.py).

Not a canonical column: exports never carry it, so the loader must not overwrite it.

Revision ID: 0010
Revises: 0009
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE lots ADD COLUMN final_price numeric  -- NULL: unknown or not sold")


def downgrade() -> None:
    op.execute("ALTER TABLE lots DROP COLUMN final_price")
