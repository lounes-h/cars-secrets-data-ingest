"""AI models a person added on the lot page for its analyses, besides the built-in ones
(ai.MODELS, in code). Deleting one leaves its analyses as they are: each keeps its model id and
what it cost.

Revision ID: 0014
Revises: 0013
"""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ai_models (
            id          text PRIMARY KEY,  -- the model id the provider expects
            provider    text NOT NULL,     -- a key of ai.CLIENTS
            label       text NOT NULL,
            note        text NOT NULL DEFAULT '',
            input_usd   double precision NOT NULL CHECK (input_usd >= 0),  -- per million tokens
            output_usd  double precision NOT NULL CHECK (output_usd >= 0),
            created_by  text,
            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now()
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE ai_models")
