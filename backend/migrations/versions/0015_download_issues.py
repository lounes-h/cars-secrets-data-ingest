"""Downloads that delivered no file (missed while the Mac slept, skipped, failed, paused), so the
Runs page shows them between the ingested files. A delivered file is an ingest_files row.

Revision ID: 0015
Revises: 0014
"""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE download_issues (
            id       bigserial PRIMARY KEY,
            source   text NOT NULL,
            status   text NOT NULL CHECK (status IN ('missed', 'skipped', 'failed', 'paused')),
            at       timestamptz NOT NULL,  -- when the download started, or was planned (missed)
            message  text NOT NULL
        );
        CREATE INDEX download_issues_at ON download_issues (at DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE download_issues")
