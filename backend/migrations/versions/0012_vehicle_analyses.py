"""AI condition and damage analyses of a lot (vehicle_analysis.py), one row per request.

Regenerating adds a row: earlier analyses stay, to compare models, prompts and versions.

Revision ID: 0012
Revises: 0011
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE vehicle_analyses (
            id                  bigserial PRIMARY KEY,
            source              text NOT NULL,
            lot_number          text NOT NULL,
            status              text NOT NULL DEFAULT 'queued'
                                  CHECK (status IN ('queued', 'processing', 'completed', 'failed')),
            language            text NOT NULL,
            requested_by        text,
            -- What ran, stamped by the worker when it starts the analysis.
            provider            text,
            model               text,
            prompt_version      text,
            schema_version      text,
            -- The lot's metadata as sent, and the photos sent: image_id -> original photo.
            input_snapshot      jsonb,
            images              jsonb,
            image_count         integer,
            -- How the photos were chosen among all of the lot's (NULL: all were sent).
            selection           jsonb,
            result              jsonb,  -- the validated analysis; set only when completed
            raw_response        text,   -- the model's answer as returned, kept for debugging
            provider_request_id text,
            usage               jsonb,
            error               text,
            created_at          timestamptz NOT NULL DEFAULT now(),
            started_at          timestamptz,
            completed_at        timestamptz,
            updated_at          timestamptz NOT NULL DEFAULT now(),
            FOREIGN KEY (source, lot_number) REFERENCES lots (source, lot_number)
        );
        CREATE INDEX vehicle_analyses_lot ON vehicle_analyses (source, lot_number, id DESC);
        -- At most one analysis of a lot waits or runs at a time: no duplicate clicks.
        CREATE UNIQUE INDEX vehicle_analyses_one_pending ON vehicle_analyses (source, lot_number)
            WHERE status IN ('queued', 'processing');
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE vehicle_analyses")
