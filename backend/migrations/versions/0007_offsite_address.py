"""Where a lot is held when it is not at the auction's yard.

Revision ID: 0007
Revises: 0006
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE lots ADD COLUMN offsite_address text,
                         ADD COLUMN offsite_city text,
                         ADD COLUMN offsite_state text,
                         ADD COLUMN offsite_zip text;
        -- Lots missing from later files (other yards, removed lots) would otherwise stay empty.
        UPDATE lots SET offsite_address = attributes ->> 'offsite_address1',
                        offsite_city    = attributes ->> 'offsite_city',
                        offsite_state   = attributes ->> 'offsite_state',
                        offsite_zip     = attributes ->> 'offsite_zip'
        WHERE attributes ?| array['offsite_address1', 'offsite_city', 'offsite_state',
                                  'offsite_zip'];
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE lots DROP COLUMN offsite_address, DROP COLUMN offsite_city,
                         DROP COLUMN offsite_state, DROP COLUMN offsite_zip;
        """
    )
