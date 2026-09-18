"""Downloader status, written by the host-side downloader so the dashboard can show it.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-18
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE downloader_status (
            source                   text PRIMARY KEY,
            updated_at               timestamptz NOT NULL DEFAULT now(),
            -- when the scheduler (launchd) last woke the downloader, before its random delay
            last_invocation_at       timestamptz,
            schedule_interval_seconds integer,
            jitter_seconds           integer,
            last_attempt_at          timestamptz,
            last_success_at          timestamptz,
            consecutive_failures     integer NOT NULL DEFAULT 0,
            -- earliest time pacing/backoff allows the next download
            next_allowed_at          timestamptz,
            -- 'login_required' / 'challenge': paused until a person logs in
            needs_attention          text
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE downloader_status")
