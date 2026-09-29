"""The exact request of each AI analysis: settings, and per model call the parameters, prompts
and message sent (photos by size), to compare prompts and models later.

Revision ID: 0013
Revises: 0012
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE vehicle_analyses ADD COLUMN request jsonb  -- NULL: made before 0013")


def downgrade() -> None:
    op.execute("ALTER TABLE vehicle_analyses DROP COLUMN request")
