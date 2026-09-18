"""Initial schema: files, lots with history, column registry, alerts, heartbeat.

Revision ID: 0001
Revises:
Create Date: 2026-09-17
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ingest_files (
            id              bigserial PRIMARY KEY,
            source          text NOT NULL,
            filename        text NOT NULL,
            sha256          text NOT NULL,
            size_bytes      bigint,
            status          text NOT NULL CHECK (status IN
                              ('processing', 'succeeded', 'failed', 'duplicate', 'interrupted')),
            received_at     timestamptz NOT NULL DEFAULT now(),
            started_at      timestamptz,
            finished_at     timestamptz,
            rows_total      integer,
            rows_new        integer,
            rows_updated    integer,
            rows_unchanged  integer,
            rows_rejected   integer,
            rows_removed    integer,
            -- Removal scopes (e.g. yards) whose missing lots were not marked removed.
            removal_skipped jsonb NOT NULL DEFAULT '[]',
            columns         jsonb,
            field_errors    jsonb,
            error           text,
            archived_path   text
        );
        -- The same export is never ingested twice; failed attempts may be retried.
        CREATE UNIQUE INDEX ingest_files_sha256_succeeded
            ON ingest_files (source, sha256) WHERE status = 'succeeded';
        CREATE INDEX ingest_files_received_at ON ingest_files (received_at DESC);
        CREATE INDEX ingest_files_source ON ingest_files (source, received_at DESC);

        CREATE TABLE lots (
            source              text NOT NULL,
            lot_number          text NOT NULL,
            yard_code           text,
            yard_name           text,
            sale_date           date,
            sale_at             timestamptz,
            vehicle_type        text,
            year                integer,
            make                text,
            model_group         text,
            model_detail        text,
            trim                text,
            body_style          text,
            color               text,
            damage_primary      text,
            damage_secondary    text,
            title_state         text,
            title_type          text,
            has_keys            text,
            vin                 text,
            odometer            numeric,
            odometer_brand      text,
            est_retail_value    numeric,
            repair_cost         numeric,
            engine              text,
            drive               text,
            transmission        text,
            fuel_type           text,
            cylinders           text,
            runs_drives         text,
            sale_status         text,
            high_bid            numeric,
            buy_it_now_price    numeric,
            currency            text,
            location_city       text,
            location_state      text,
            location_zip        text,
            location_country    text,
            seller_name         text,
            image_url           text,
            source_created_at   timestamp,
            source_updated_at   timestamptz,
            attributes          jsonb NOT NULL,
            row_hash            bytea NOT NULL,
            first_seen_at       timestamptz NOT NULL,
            last_seen_at        timestamptz NOT NULL,
            last_changed_at     timestamptz NOT NULL,
            removed_at          timestamptz,
            first_file_id       bigint REFERENCES ingest_files (id),
            last_file_id        bigint REFERENCES ingest_files (id),
            PRIMARY KEY (source, lot_number)
        ) WITH (fillfactor = 80);  -- room for HOT updates of last_seen_at every run
        CREATE INDEX lots_lot_number ON lots (lot_number);
        CREATE INDEX lots_vin ON lots (vin);
        CREATE INDEX lots_make_model ON lots (make, model_group);
        CREATE INDEX lots_sale_at ON lots (sale_at);
        CREATE INDEX lots_first_seen_at ON lots (first_seen_at);
        CREATE INDEX lots_last_changed_at ON lots (last_changed_at);
        CREATE INDEX lots_active ON lots (source) WHERE removed_at IS NULL;

        -- Previous versions of a lot; the current version lives in `lots`.
        CREATE TABLE lot_versions (
            id          bigserial PRIMARY KEY,
            source      text NOT NULL,
            lot_number  text NOT NULL,
            valid_from  timestamptz NOT NULL,
            valid_to    timestamptz NOT NULL,
            row_hash    bytea NOT NULL,
            attributes  jsonb NOT NULL,
            file_id     bigint REFERENCES ingest_files (id),
            superseded_by_file_id bigint REFERENCES ingest_files (id),
            FOREIGN KEY (source, lot_number) REFERENCES lots (source, lot_number) ON DELETE CASCADE
        );
        CREATE INDEX lot_versions_lot ON lot_versions (source, lot_number, valid_to DESC);

        CREATE TABLE column_registry (
            source          text NOT NULL,
            normalized_name text NOT NULL,
            source_name     text NOT NULL,
            mapped_to       text,
            status          text NOT NULL CHECK (status IN ('active', 'missing')),
            first_seen_at   timestamptz NOT NULL DEFAULT now(),
            last_seen_at    timestamptz NOT NULL DEFAULT now(),
            first_file_id   bigint REFERENCES ingest_files (id),
            last_file_id    bigint REFERENCES ingest_files (id),
            PRIMARY KEY (source, normalized_name)
        );

        CREATE TABLE rejected_rows (
            id       bigserial PRIMARY KEY,
            file_id  bigint NOT NULL REFERENCES ingest_files (id) ON DELETE CASCADE,
            line_no  integer NOT NULL,
            reason   text NOT NULL,
            raw      jsonb NOT NULL
        );
        CREATE INDEX rejected_rows_file ON rejected_rows (file_id, line_no);

        CREATE TABLE alerts (
            id           bigserial PRIMARY KEY,
            severity     text NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
            type         text NOT NULL,
            title        text NOT NULL,
            details      jsonb NOT NULL DEFAULT '{}',
            source       text,
            fingerprint  text NOT NULL,
            occurrences  integer NOT NULL DEFAULT 1,
            file_id      bigint REFERENCES ingest_files (id) ON DELETE SET NULL,
            created_at   timestamptz NOT NULL DEFAULT now(),
            last_seen_at timestamptz NOT NULL DEFAULT now(),
            acked_at     timestamptz,
            acked_by     text,
            resolved_at  timestamptz,
            resolved_by  text
        );
        -- At most one open alert per condition; repeats bump `occurrences`.
        CREATE UNIQUE INDEX alerts_open_fingerprint ON alerts (fingerprint)
            WHERE resolved_at IS NULL;
        CREATE INDEX alerts_created_at ON alerts (created_at DESC);

        CREATE TABLE worker_heartbeat (
            worker_id       text PRIMARY KEY,
            started_at      timestamptz NOT NULL,
            last_beat_at    timestamptz NOT NULL,
            state           text NOT NULL,
            current_file_id bigint REFERENCES ingest_files (id) ON DELETE SET NULL
        );
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE worker_heartbeat, alerts, rejected_rows, column_registry,
                   lot_versions, lots, ingest_files;
        """
    )
