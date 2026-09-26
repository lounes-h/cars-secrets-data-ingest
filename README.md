# Cars Secrets Data Ingest

Continuous ingestion of vehicle-auction CSV exports (Copart first) into PostgreSQL, with a
live monitoring dashboard and alerts.

Save an export into `data/inbox/copart/` and it is ingested within seconds. Duplicate rows
across exports are detected, and every real change to a lot is kept as history. The database
schema survives CSV column changes. Anything that goes wrong shows up as an alert on the
dashboard.

## Run it

With Docker running, start the service from the repository root:

```bash
./start.sh
```

To start the service and follow the worker and API logs:

```bash
./start.sh --logs
```

Needs Docker with Compose v2, nothing else. On first run the script generates `.env` with
random credentials, builds the images, starts Postgres, runs migrations, and starts the
worker, the API and the dashboard. It prints the URL (http://localhost:8080) and the login.

```bash
cp ~/Downloads/salesdata.csv data/inbox/copart/   # ingest an export you downloaded
python3 scripts/simulate_export.py                # a realistic "next hour" export from the latest one
docker compose logs -f worker api            # watch it happen
docker compose down                          # stop (add -v to delete the database too)
```

Exports reach the inbox through the **Copart downloader**, which clicks "Download CSV file"
hourly in Chrome using your logged-in session. `./start.sh` installs it and launchd keeps it
running; see [docs/downloader.md](docs/downloader.md). Any
other tool works as well if it saves into `data/inbox/copart/`. Writing to a temporary name
and renaming it when done is best: `.part`, `.tmp` and `.crdownload` files are ignored.

## What happens to a file

1. **Picked up.** The worker notices the file through a filesystem event (or a 30 s rescan)
   and waits until it is complete.
2. **Deduplicated.** A byte-identical export is recorded as `duplicate` and skipped.
3. **Loaded in one transaction.** Rows are bulk-copied into a staging table and compared with
   the current state of each lot:
   - **new** lots are inserted,
   - **updated** lots have their previous version saved to `lot_versions`,
   - **unchanged** lots only get `last_seen_at` refreshed,
   - **removed**: lots missing from the file are marked removed, but only in yards the file
     actually covers, and only once the lot's sale time plus 2 hours has passed (the auction
     opens at the sale time but bidding on the lot ends later). Until then the lot stays
     active; the run shows it as "removal deferred".
4. **Archived.** The file is gzipped into `data/archive/` (kept 7 days). If the load fails,
   the file goes to `data/failed/` instead, raises a critical alert, and can be retried from
   the dashboard.
5. **Copied to the API database.** The lots the file changed are copied to the database
   that `cars-secrets-api` reads (`API_DATABASE_URL`, a local `api-db` container by default).
   It holds only active lots, plus removed lots until their auction is over (sale time + 4 h),
   so it stays small. See [docs/architecture.md](docs/architecture.md#the-api-database).

## Dashboard

| Page | What it shows |
|---|---|
| Overview | Pipeline health, worker and downloader status (running, next download), per-provider last file, lot counts, changes per run, ingest duration, open alerts |
| Runs | A live countdown to the next download with a **Download now** button (or why none is coming: paused, not running, not set up), then every file received: status, row counts, duration. Detail view shows rejected rows, run alerts, columns, and a retry button for failed files |
| Alerts | Open/resolved alerts with severity filter; acknowledge and resolve |
| Schema | Every column a provider has sent, what it maps to, and when it appeared or disappeared |
| Lots | Search by lot number, VIN, make or model, with thumbnails; per lot: photo gallery (full size + HD), a link to the lot on the auction site (all photos, bidding) and field-by-field change history |

New warning and critical alerts pop up on whatever page is open (Server-Sent Events).

## Alerts

| Alert | Severity | When |
|---|---|---|
| `ingest_failed` / `file_rejected` | critical | A file could not be loaded (DB error, missing lot number column, empty file) |
| `no_new_file` | critical | No export ingested for 75 min (`STALE_AFTER_MINUTES`); resolves on the next file |
| `worker_down` | critical | No worker heartbeat for 2 min; resolves when it's back |
| `rejected_rows` | warning / critical | ≥ 1 % / ≥ 10 % of a file's rows unusable |
| `field_conversion_errors` | warning | ≥ 1 % of a typed column couldn't be parsed (format change?) |
| `schema_column_added` / `schema_column_removed` | warning | The export's header changed |
| `row_count_anomaly` | warning | Row count differs from the recent average by ≥ 30 % |
| `removal_skipped` | info | A yard in the file holds under half its active lots, so its missing lots were kept (routine with partial downloads) |
| `slow_ingest` | warning | A load took more than 5 min |
| `unroutable_file` | warning | A CSV was saved outside `inbox/<provider>/` |
| `archive_failed` | warning | The file was loaded but could not be archived |
| `duplicate_file` | info | Identical export received again |
| `downloader_attention` | critical | Copart showed a login or verification page; downloads paused until you run `login` |
| `downloader_failed` | warning → critical | The download failed (timeout, invalid file, changed page); retried with backoff |
| `api_sync_failed` | warning | The API database could not be updated; retried after the next file or within 15 min, then resolves itself |
| `pricing_failed` | warning | Lots could not be priced (unreadable model file); retried with each sync, which goes on without new prices |
| `downloader_down` | critical | The always-on downloader stopped reporting for 2.5 min; resolves itself when launchd restarts it |

## Configuration

All settings are environment variables, set in `.env`. See `.env.example` for the ones used
by Compose, and `backend/ingest/config.py` for every threshold.

## Development

See [CLAUDE.md](CLAUDE.md) for commands, architecture rules and conventions, and
[docs/architecture.md](docs/architecture.md) for the data model and how to add a provider.
