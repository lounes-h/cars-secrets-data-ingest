"""Lots a dashboard user marked as favorite.

Revision ID: 0011
Revises: 0010
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE lot_favorites (
            username    text NOT NULL,  -- the dashboard user (session)
            source      text NOT NULL,
            lot_number  text NOT NULL,
            created_at  timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (username, source, lot_number),
            FOREIGN KEY (source, lot_number) REFERENCES lots (source, lot_number)
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE lot_favorites")
