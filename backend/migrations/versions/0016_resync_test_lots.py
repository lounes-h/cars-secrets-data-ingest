"""Copart's test lots leave the API database: a new sync_version makes the next sync re-read
them, and it no longer serves them (`Provider.test_lots`, copied here as it stood).

Revision ID: 0016
Revises: 0015
"""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        UPDATE lots SET sync_version = nextval('lots_sync_version_seq')
        WHERE source = 'copart'
          AND (yard_name ~* '\mTEST\M' OR attributes ->> 'special_note' ~* '\mTEST\M');
        """
    )


def downgrade() -> None:
    pass  # only a version bump; there is nothing to undo
