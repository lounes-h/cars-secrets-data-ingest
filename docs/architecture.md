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

 worker ── after each file + every 15 min ──► API database ◄── cars-secrets-api (reads)
          (changed lots only, one transaction)  active lots + auctions not yet over
```

| Module (`backend/ingest/`) | Responsibility |
|---|---|
| `worker.py` | Watches the inbox, queues complete files, processes them one at a time, heartbeats, purges the archive |
| `files.py` | Candidate filtering, provider routing by folder, completeness check, hashing, archive/move |
| `pipeline.py` | One file end to end: register, duplicate check, load, alerts, archive or fail |
| `parsing.py` | Streaming CSV parser: header normalization, row validation, typed conversion, row hash |
| `loader.py` | The single load transaction: staging, change detection, history, upsert, removals, registry |
| `api_sync.py` | Keeps the API's database in step: its tables, the incremental copy, the purge of finished auctions |
| `pricing.py` | Final price model: training file with generated sold prices, XGBoost training, pricing the active lots |
| `auction_apis.py`, `api_check.py` | Clients for apibara.tech and apiauctions.io (third-party Copart/IAA data with final prices), and the check of their data against our lots |
| `providers/` | Everything provider-specific (see below) |
| `schema.py` | Canonical typed columns of `lots` |
| `alerts.py` | Alert rules (pure) and storage (dedupe by fingerprint, auto-resolve) |
| `monitor.py` | Periodic checks run by the API: worker heartbeat, staleness per provider |
| `api/` | Dashboard REST API, SSE, session auth |

## Data model

| Table | Holds |
|---|---|
| `ingest_files` | One row per file received: status, counts, removed lot numbers, duration, header, error, archive path |
| `lots` | Current state per `(source, lot_number)`: typed canonical columns + `attributes` JSONB (every source column) + `row_hash`, `first_seen_at`, `last_seen_at`, `last_changed_at`, `removed_at`, `sync_version`, and the price prediction (`predicted_final_price`, `prediction_model`, `predicted_version`) |
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

## The API database

`cars-secrets-api` does not read this database. It reads its own, which the worker keeps in
step (`api_sync.py`) and which stays small. Around 200k lots are active at any time, while this
database keeps every lot ever seen, with its history, and grows every day.

| API database table | Holds |
|---|---|
| `lots` | Every active lot, and each removed lot until `sale_at` + `API_KEEP_AFTER_SALE_MINUTES` (240): the lot's auction runs 2–4 h after its sale time. Columns: `source`, `lot_number`, the canonical columns, `first_seen_at`, `last_changed_at`, `removed_at`, `predicted_final_price` (no `attributes`, hashes or file ids) |
| `ingest_sync` | One row: the `sync_version` the table is at, and when it got there |

The worker creates both tables and their indexes (`api_sync.ensure_schema`). A canonical
column added later is added there too, and its values arrive through a full copy. The API's
own tables (`app` schema) are migrated by `cars-secrets-api`.

**How a sync works.** Every write to `lots` that changes a served column draws a new
`sync_version` from `lots_sync_version_seq`: an insert, a real change, a typed column filled
by a new mapping, a removal, a reappearance. Refreshing `last_seen_at` does not. Loads hold
one global advisory lock, so a version never becomes visible after a higher one. A sync then:

1. locks the API database's sync (two workers never interleave) and reads its watermark;
2. streams `COPY (SELECT … WHERE sync_version > watermark)` from one repeatable-read snapshot
   of this database into a temp table on the API database;
3. upserts the lots to keep, deletes the changed lots it must not keep, deletes removed lots
   whose auction is now over, and stores the new watermark, all in **one transaction**.

API readers see either the previous state or the new one, never half a sync. A sync that fails
(API database unreachable, a constraint) changes nothing there. The ingest is unaffected, the
worker raises `api_sync_failed` (warning), and the next sync copies everything since the old
watermark and resolves the alert.

**When it runs.** Right after each successfully ingested file (hourly, with the downloader),
at worker start, and every `API_SYNC_INTERVAL_SECONDS` (900). The timer retries failed syncs
and purges finished auctions while no export arrives. A sync with nothing to copy or purge
writes nothing to the API database.

**Full copy.** When the API database has no watermark (new or emptied), its `lots` table is
missing or lacks a column, or its watermark is ahead of this database (restored from a
backup), the sync replaces its contents with every lot it should hold, still in one
transaction.

## Price model

`pricing.py` predicts each active lot's final auction price with XGBoost; `api_sync` serves it as
`predicted_final_price`. **No sold prices exist yet**: the training target is generated
(`pricing.synthesize`: retail value discounted for damage, title, condition, age and mileage,
less part of the repair cost, with noise), so predictions only exercise the pipeline until real
sale prices replace it in the same training file.

1. `python -m ingest.pricing synth` writes `data/training/lots.csv.gz`: the most recent 300k
   lots, removed ones included, with only the model's columns (`pricing.COLUMNS`: no ids, VINs,
   URLs or addresses) plus `final_bid_sale`.
2. `python -m ingest.pricing train <file>` fits 70 %, stops early on 15 %, prints MAE, MAPE and
   R² on the last 15 % next to a naive baseline (median price/retail ratio), and saves
   `data/models/final_price.ubj`. Categories seen in fewer than 50 training rows count as
   missing: every categorical split lists its categories, and keeping rare makes and models
   made the model 240 MB instead of 30 MB. The kept categories are stored in the model, and
   values it never saw are priced as missing.
3. Before each API sync, the worker prices the active lots not yet priced by this model at
   their current `sync_version` (`predicted_version`), 20k per transaction under the loader's
   lock, and gives each batch a new `sync_version` so the sync copies it. The model is
   identified by its file's sha256, so replacing the file reprices every lot. Without the file
   (`PRICE_MODEL_PATH`, default `/data/models/final_price.ubj`) nothing is priced and the column
   stays NULL. A failure raises `pricing_failed` (warning) and never holds up the sync.

On the ~240k lots of 2026-09-23: MAE $493 against $2,032 for the baseline, R² 0.92, 7 s of
training. Pricing all 148k active lots takes 12 s, and a refresh with nothing to do 0.5 s.

### Real sale prices: checking the third-party APIs

apibara.tech and apiauctions.io index Copart and IAA lots with their final prices. Before their
prices train the model (and IAA lots become a provider), `python -m ingest.api_check` measures
them. A run holds four slices: the Copart and IAA lots of one make sold on one day, and those
scheduled for a later day (our lots by Copart's own `sale_date`: a night auction at 01:00 UTC
belongs to the US evening before). A slice is complete, so coverage can be measured, and small
enough for a free quota (apibara: 100 requests a month, 20 lots a page). Fetch both APIs in
the same run and report right after: lots get relisted and added every day.

- `fetch` saves each raw page under `data/checks/<sold>_<active>_<make>/<api>/<slice>/` and
  never requests a saved page again; an API without a key is skipped.
- `report` runs offline. For Copart it compares every lot with ours: coverage both ways, VIN,
  year, make, odometer, damage, the sale time (during its auction, 4 h, or before it but not
  before we last saw it listed), that a lot listed again for a later auction is not called
  sold, and the price, which can never be below the high bid of our last export (bids are
  binding) nor below the API's own last bid. It compares the two APIs with each other (on the
  lots either says sold, for sold slices: whether it sold, the price, the VIN) and with
  `manual.csv`, where a person enters the final prices they read on copart.com / iaai.com.
  Doubtful prices and lots the APIs disagree on head that list. It prints the shares with the
  bar each must reach and writes `summary.json` and `mismatches.csv`.

Only a "Sold" status counts as a sale. apibara's "Sold on Approval" is a bid awaiting the
seller: on Copart most came back unsold, on IAA all were sold. apibara lists no unsold lots,
reports the auction's start as `auction_at` for a lot sold at auction and the moment of sale for
one sold before it, and gives a Buy Now sale's price only as `buy_now_usd`. apiauctions answers
`{"result": [...], "pagination": {"total_pages": …}}` at 50 lots a page at most (its docs say
`data`, `meta` and 100); a lot keeps only its latest sale in `sales_history` ("Sold" or
"Not sold", the price, epoch seconds; an unsold lot carries the refused bid) and a later
auction in `active_bidding` (epoch milliseconds).

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
