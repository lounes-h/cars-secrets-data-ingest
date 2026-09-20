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
| `ingest_files` | One row per file received: status, counts, removed lot numbers, duration, header, error, archive path |
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

### Malformed rows

Copart writes every text field as `"` + value + `"` and never escapes a quote inside a
value: inch marks (`M-240ML-SLIDE 29'9"` is written `"M-240ML-SLIDE 29'9""`) or quoted
notes. A standard CSV parser rejects some of these rows (the field swallows the next one)
and silently mangles others (`126" SLEEPER CAB` read as `126 SLEEPER CAB"`). The provider's
`split_record` hook re-reads any record with a quote inside a field the way Copart wrote it:
a quoted field ends at the first quote followed by a comma or the line end. The result is
used only if it has exactly one field per header column. In a real 139,545-row export this
fixed 28 rows (6 had been rejected) and changed nothing else.

A row that still has the wrong field count is rejected (`rejected_rows`), since every column
after the fault would be shifted. Its lot is still in the export, though, so it is never
marked removed because of that file.

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

A lot absent from an export is marked removed only if all of these hold (`loader._mark_removed`):

- its **yard** appears in the export (`Provider.removal_scope`), because Copart downloads can
  cover a subset of yards;
- the export holds at least `REMOVAL_MIN_RATIO` (50 %) of that yard's active lots, which
  protects against truncated files;
- its auction is over: `sale_at + REMOVAL_GRACE_MINUTES` (120) is in the past. A lot leaves
  the list when its sale opens, but bidding on it ends later. Lots still pending are counted
  in `ingest_files.rows_removal_deferred` and removed by the first later export that still
  lacks them. Lots with no sale time are never removed: Copart's export drops lots its site
  still shows as upcoming (e.g. lot 68264956), so their absence proves nothing.

A removed lot that reappears is active again.

The load transaction saves the exact removed `lot_number` values in
`ingest_files.removed_lot_numbers`. The list stays unchanged if a lot later reappears. An
empty list means no removals; NULL means the list was not recorded (including runs from
before migration 0005). For older runs, the API recovers lot numbers whose removal timestamps
fall within that run alone, provided their count matches `rows_removed`. If lots reappeared or
run intervals overlap, a complete list may no longer be recoverable; the UI labels it
unavailable. Copart's CSV `Id` row counter is never used as a lot identifier.

## What a run did

`GET /api/runs/{run_id}` returns the run and its alerts only: an export can create, change or
remove six figures of lots, so the four lists are paged separately by
`GET /api/runs/{run_id}/rows?kind=new|updated|removed|rejected&limit=&offset=`, which answers
`{items, total, available, reason}`. The run details page shows them as tabs, each with its
count, one page (50 rows) at a time.

| kind | rows | where they come from |
|---|---|---|
| `new` | lots | `lots.first_file_id = run_id` (never rewritten once set) |
| `updated` | lots | `lot_versions.superseded_by_file_id = run_id`, one row per changed lot |
| `removed` | lots | `ingest_files.removed_lot_numbers`, sliced by `offset`/`limit` |
| `rejected` | raw rows | `rejected_rows`, with the per-row reason |

Migration 0006 indexes the first two lookups (`lots (first_file_id, lot_number)`,
`lot_versions (superseded_by_file_id, lot_number)`); without them each page scans every lot.

`total` reuses the counts the load transaction wrote, except for rejected rows: only the first
`MAX_STORED_REJECTS` (1,000) of a file are kept, so those are counted and the UI says how many
more were rejected without being stored. `reason` carries the removal rule in words (missing
from the export, sale time plus `REMOVAL_GRACE_MINUTES` passed), which is the same for every
lot a run removed; rejections carry their own reason per row. `available` is false only when
an older run's removed lots cannot be reconstructed.

## Lot photos

Exports only carry a thumbnail and a link to the provider's image API. `Provider.fetch_images`
turns the stored lot into a list of photos (thumbnail / full / HD). For Copart it calls
`https://inventoryv2.copart.io/v1/lotImages/<lot>`. For some lots, about 1 in 6–12 (they look
like lots on Copart's newer image store), that API answers 404 even though copart.com shows
their photos. Copart's site loads those through member-only endpoints, which we deliberately
don't call. In that case the full-size photo is derived from the export's thumbnail
(`_thb` → `_ful`) and the result is marked `complete: false`. The lot page then shows a
"See all photos on the auction site" link.

The endpoint `GET /api/lots/{source}/{lot_number}/images` returns `{images, complete}`, cached
for 15 minutes. Browsers load the photos straight from `cs.copart.com`. Each lot also carries
`listing_url` (`Provider.listing_url`, Copart: `https://www.copart.com/lot/<n>`): its page on
the auction site, where dealers go to bid.

## Adding a provider

1. Create `backend/ingest/providers/<name>.py` defining a `Provider`: key aliases and key
   parser, `FieldMapping`s onto canonical columns, `ignored_columns`, `volatile_columns`, an
   optional `derive` for columns built from several source values, `removal_scope`
   (`None` if every export is a full snapshot) and optionally `fetch_images` and
   `listing_url`.
2. Register it in `providers/__init__.py`.
3. Add it to `ENABLED_PROVIDERS` so the staleness check covers it.
4. Save its files into `data/inbox/<name>/`. The worker creates the folder on start.

No migration is needed. Add tests with a small real fixture, as `tests/fixtures/sample.csv`
does for Copart.

## Throughput

With a local Postgres, the first load of 142k rows (89 MB) takes ~12 s. Incremental loads of
24k rows take ~3.5 s. Parsing is ~4 s per 142k rows; most of the rest is the upsert. Memory
stays flat because rows stream from the file straight into `COPY`.
