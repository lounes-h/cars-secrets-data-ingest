"""Predicted final auction price of each lot (pricing.py).

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
        -- Filled by the worker's first price refresh; nothing to backfill.
        ALTER TABLE lots ADD COLUMN predicted_final_price numeric,
                         ADD COLUMN prediction_model text,
                         ADD COLUMN predicted_version bigint;
        """
    )


def downgrade() -> None:
    op.execute(
        """ALTER TABLE lots DROP COLUMN predicted_final_price, DROP COLUMN prediction_model,
                            DROP COLUMN predicted_version"""
    )
