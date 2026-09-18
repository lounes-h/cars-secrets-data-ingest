"""Downloader runs as an always-on process: heartbeat, next planned run, run-now requests.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-18
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE downloader_status
            ADD COLUMN heartbeat_at      timestamptz,  -- every ~30 s while the process runs
            ADD COLUMN daemon_started_at timestamptz,
            ADD COLUMN activity          text,         -- 'idle' | 'downloading'
            ADD COLUMN next_run_at       timestamptz,  -- planned by the process itself
            ADD COLUMN run_requested_at  timestamptz,  -- "Download now" from the dashboard
            ADD COLUMN run_requested_by  text,
            DROP COLUMN schedule_interval_seconds,
            DROP COLUMN jitter_seconds;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE downloader_status
            DROP COLUMN heartbeat_at, DROP COLUMN daemon_started_at, DROP COLUMN activity,
            DROP COLUMN next_run_at, DROP COLUMN run_requested_at, DROP COLUMN run_requested_by,
            ADD COLUMN schedule_interval_seconds integer, ADD COLUMN jitter_seconds integer;
        """
    )
