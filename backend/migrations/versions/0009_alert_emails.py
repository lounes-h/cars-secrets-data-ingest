"""Remember which critical alerts were mailed.

Revision ID: 0009
Revises: 0008
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE alerts ADD COLUMN emailed_at timestamptz;
        -- Alerts already open count as mailed: turning mail on must not send a backlog.
        UPDATE alerts SET emailed_at = now() WHERE resolved_at IS NULL;
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE alerts DROP COLUMN emailed_at")
