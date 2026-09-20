"""Index the lots a run created or changed, so run details can page through them.

Revision ID: 0006
Revises: 0005
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Without these, listing one run's new/updated lots scans every lot (142k+ rows) per page.
    op.execute("CREATE INDEX lots_first_file ON lots (first_file_id, lot_number)")
    op.execute("CREATE INDEX lot_versions_superseded "
               "ON lot_versions (superseded_by_file_id, lot_number)")


def downgrade() -> None:
    op.execute("DROP INDEX lot_versions_superseded")
    op.execute("DROP INDEX lots_first_file")
