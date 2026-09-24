"""Version lot writes so the API's database can copy only what changed.

Revision ID: 0008
Revises: 0007
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE SEQUENCE lots_sync_version_seq;
        -- Added without a default first, so the table is not rewritten. Existing rows stay NULL:
        -- the API database's first sync is a full copy, and any later change sets a version.
        ALTER TABLE lots ADD COLUMN sync_version bigint;
        ALTER TABLE lots ALTER COLUMN sync_version SET DEFAULT nextval('lots_sync_version_seq');
        ALTER SEQUENCE lots_sync_version_seq OWNED BY lots.sync_version;
        CREATE INDEX lots_sync_version ON lots (sync_version);
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE lots DROP COLUMN sync_version")  # drops the owned sequence too
