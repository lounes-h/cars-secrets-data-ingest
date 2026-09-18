# Copart downloader

Downloads Copart's CSV sales data once an hour from
https://www.copart.com/downloadSalesData, using your member session. It saves each file where
the pipeline picks it up (`data/inbox/copart/`).

> Copart's sales data is covered by its **Image and Data License Agreement** (linked on the
> download page). Make sure automated downloads, and the way the data and photos are used,
> are allowed for your account before scheduling this.

## How it behaves

- **Real Chrome, your session.** The downloader drives the installed Google Chrome through
  Playwright, with its own profile in `~/.cars-ingest/copart-profile`. You log in once by hand
  and later runs reuse the session. No password is stored anywhere.
- **Like a person.** It opens the page and clicks **Download CSV file**. It runs hourly with up
  to 5 min of random delay, and never more than once per 45 min, whatever the scheduler does.
  The window is visible by default (`COPART_HEADED=false` to hide it).
- **Stops rather than pushes.** If Copart shows a login page, a CAPTCHA or any "access denied"
  page, downloads **pause** and a critical alert appears on the dashboard. Nothing retries
  until you run `login` again. The downloader never fills in credentials and never interacts
  with a CAPTCHA.
- **Backs off on errors.** Timeouts, network errors or a changed page layout are retried after
  1 h, 2 h, 4 h, then every 6 h, with a warning alert that becomes critical after 3 failures in
  a row.
- **Only complete, valid files reach the pipeline.** The download lands in `data/.downloads/`
  and is checked: it must be a CSV whose header has `Lot number`, which rules out an HTML
  error page. It is then moved into the inbox as `salesdata_<UTC time>.csv` via a `.part`
  name, so the worker never reads a half-written file. An invalid file goes to
  `data/failed/downloader/` instead.

The pipeline's own checks still apply: identical files are skipped, a truncated file is
flagged by the row-count and per-yard guards, and "no new file for 75 min" catches a
downloader that silently stopped.

## Setup (macOS)

```bash
./start.sh                                   # pipeline must be running (creates .env)
./scripts/install_downloader.sh              # installs Playwright, schedules hourly runs
cd backend && .venv/bin/python -m ingest.downloader login       # log in once, by hand
cd backend && .venv/bin/python -m ingest.downloader run --force # first download now
```

`login` opens a Chrome window on Copart. Log in as usual; the window closes itself once the
download button is reachable.

## Day to day

| Situation | What you see | What to do |
|---|---|---|
| Normal | A new run every hour on the dashboard | Nothing |
| Session expired or verification page | Critical alert "downloads paused until you log in again" | Run `login` |
| Download failing | Warning, then critical, "Copart download failed (N× in a row)" | Check `~/Library/Logs/cars-ingest/downloader.log`; if Copart changed the page, update the button text in `ingest/downloader/browser.py` |
| Mac asleep | Runs resume on wake; "no new file" alert if asleep over 75 min | Nothing, or keep the Mac awake |

```bash
cd backend && .venv/bin/python -m ingest.downloader status   # last attempt/success, next run
./scripts/install_downloader.sh --uninstall                  # stop scheduled downloads
```

## Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `COPART_HEADED` | `true` | Show the Chrome window during downloads |
| `MIN_INTERVAL_MINUTES` | `45` | Never download more often than this |
| `MAX_BACKOFF_HOURS` | `6` | Longest wait between retries after errors |
| `DOWNLOAD_TIMEOUT_SECONDS` | `600` | Give up on a download after this long (files are ~100 MB) |
