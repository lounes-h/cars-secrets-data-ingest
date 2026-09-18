# Architecture

## Components

```
downloader ──► data/inbox/<provider>/*.csv
                      │ watchdog event + 30 s rescan, wait until the file stops changing
                      ▼
              ┌──────────────┐  COPY / upsert (1 txn)  ┌────────────┐
              │ worker       │ ──────────────────────► │ PostgreSQL │
              │ ingest.worker│ ◄── advisory lock ───── │            │
              └──────┬───────┘                         └─────┬──────┘
          archive/ or failed/                  pg_notify     │ LISTEN
                                                             ▼
                               ┌──────────────┐  SSE   ┌────────────┐
                               │ api (FastAPI)│ ─────► │ web (React │
                               │ + monitor    │ ◄───── │  via nginx)│
                               └──────────────┘  JSON  └────────────┘
```

| Module (`backend/ingest/`) | Responsibility |
|---|---|
| `worker.py` | Watches the inbox, queues complete files, processes them one at a time, heartbeats, purges the archive |
| `files.py` | Candidate filtering, provider routing by folder, completeness check, hashing, archive/move |
| `pipeline.py` | One file end to end: register, duplicate check, load, alerts, archive or fail |
| `parsing.py` | Streaming CSV parser: header normalization, row validation, typed conversion, row hash |
| `loader.py` | The single load transaction: staging, change detection, history, upsert, removals, registry |
| `providers/` | Everything provider-specific (see below) |
| `schema.py` | Canonical typed columns of `lots` |
| `alerts.py` | Alert rules (pure) and storage (dedupe by fingerprint, auto-resolve) |
| `monitor.py` | Periodic checks run by the API: worker heartbeat, staleness per provider |
| `api/` | Dashboard REST API, SSE, session auth |

## Data model

| Table | Holds |
|---|---|
| `ingest_files` | One row per file received: status, counts, duration, header, error, archive path |
| `lots` | Current state per `(source, lot_number)`: typed canonical columns + `attributes` JSONB (every source column) + `row_hash`, `first_seen_at`, `last_seen_at`, `last_changed_at`, `removed_at` |
| `lot_versions` | Previous versions of changed lots (`valid_from`/`valid_to`, attributes, superseding file) |
| `column_registry` | Every column per provider: source header, mapping, `active`/`missing`, first/last seen |
| `rejected_rows` | Up to 1,000 unusable rows per file with the reason |
| `alerts` | Alerts; at most one unresolved row per `fingerprint` |
| `worker_heartbeat` | Liveness of the worker |

### Why the schema survives CSV changes

Nothing about a file's columns is assumed except the lot key. Headers are normalized
(`"Sale Date M/D/CY"` → `sale_date_m_d_cy`) and matched against each canonical column's
aliases. Every value, mapped or not, is stored in `attributes`. The typed columns exist so
common queries can use indexes. Concretely:

- **New column**: stored in `attributes`, registered as unmapped, warning alert.
- **Renamed column**: shows as one column removed and one added. Add the new header as an
  alias in the provider, and the typed column fills again from the next file.
- **Removed column**: its typed column becomes NULL, warning alert. The alert resolves itself
  if the column comes back.
- **Lot key missing**: the file is rejected and moved to `failed/`, critical alert.

### Change detection and duplicates

Each row's hash covers its attributes except the provider's *ignored* columns (`Id`, the
export's row counter, which is not even stored) and *volatile* columns (`Create Date/Time`,
stamped with the export time on every row). Decimals are canonicalized (`8119.0` → `8119`)
because exports format numbers inconsistently. Without these rules, every lot looked changed
between two real Copart downloads. With them, the real changes are bids, sale dates and
update timestamps.

When the header differs from the previous file's, hashes are not comparable. For that run
only, the loader compares just the columns present in both files.

### Removals

A lot absent from an export is marked removed only if all of these hold:

- its **yard** appears in the export (`Provider.removal_scope`), because Copart downloads can
  cover a subset of yards;
- the export holds at least `REMOVAL_MIN_RATIO` (50 %) of that yard's active lots, which
  protects against truncated files.

A removed lot that reappears is active again.

## Adding a provider

1. Create `backend/ingest/providers/<name>.py` defining a `Provider`: key aliases and key
   parser, `FieldMapping`s onto canonical columns, `ignored_columns`, `volatile_columns`, an
   optional `derive` for columns built from several source values, and `removal_scope`
   (`None` if every export is a full snapshot).
2. Register it in `providers/__init__.py`.
3. Add it to `ENABLED_PROVIDERS` so the staleness check covers it.
4. Save its files into `data/inbox/<name>/`. The worker creates the folder on start.

No migration is needed. Add tests with a small real fixture, as `tests/fixtures/sample.csv`
does for Copart.

## Throughput

With a local Postgres, the first load of 142k rows (89 MB) takes ~12 s. Incremental loads of
24k rows take ~3.5 s. Parsing is ~4 s per 142k rows; most of the rest is the upsert. Memory
stays flat because rows stream from the file straight into `COPY`.
