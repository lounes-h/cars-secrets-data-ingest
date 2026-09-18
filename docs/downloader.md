# Copart downloader

Downloads Copart's CSV sales data once an hour from
https://www.copart.com/downloadSalesData, using your member session. It saves each file where
the pipeline picks it up (`data/inbox/copart/`).

> Copart's sales data is covered by its **Image and Data License Agreement** (linked on the
> download page). Make sure automated downloads, and the way the data and photos are used,
> are allowed for your account before scheduling this.

## How it behaves

- **Real Chrome, your session, no one needed.** The downloader drives the installed Google
  Chrome through Playwright, with its own profile in `~/.cars-ingest/copart-profile`. Save
  your Copart password in Chrome's password manager in that profile (during `login`): when
  the session expires, each run waits for Chrome to fill the login form and clicks
  **Sign in** once. The downloader never types or stores your password itself.
- **Closes pop-ups.** Notices shown after login ("what's new", surveys, cookie banners) are
  closed through their close control: a button labelled *Close*, *×*, *No thanks*, *Not now*,
  *Skip*, *Got it*…, or one marked as a close button (`aria-label`, `close` class). It never
  clicks *Accept*, *Agree* or *Continue* for you, never touches a dialog holding a password
  field, and never acts on a verification page. Each closed pop-up is logged
  (`downloader.dialog_closed` with its text). If Copart adds one these rules miss, list its
  close button in `COPART_DISMISS_SELECTORS` (no code change).
- **At a person's pace.** It waits a random 1.5–4 s after each page loads and before each
  click (sign in, close, download) instead of clicking through at machine speed.
- **Survives Chrome crashes.** If Chrome crashes, hangs or its window is closed mid-run, it is
  relaunched (up to 3 attempts, 20 s apart) within the same run. Before each launch, Chrome
  processes a crashed run left on the profile are stopped and the stale profile lock is
  removed, so a crash never blocks the next launch; the "Restore pages?" bubble is hidden.
- **Like a person.** It opens the page and clicks **Download CSV file**. It runs hourly with up
  to 5 min of random delay, and never more than once per 45 min, whatever the scheduler does.
  The window is visible by default (`COPART_HEADED=false` to hide it).
- **Stops rather than pushes.** A CAPTCHA or any "access denied" page **pauses** downloads at
  once with a critical alert; nothing retries until you run `login`, and the downloader never
  interacts with such a page. A login page it cannot get past (Chrome did not fill the form,
  or Copart rejected the saved login) counts as a failed run and is retried with the usual
  backoff; after **3 runs in a row** it pauses too, so a wrong saved password cannot lock
  your account. A successful download resets that count and clears the pause alert.
- **Backs off on errors.** Timeouts, network errors or a changed page layout are retried after
  1 h, 2 h, 4 h, then every 6 h, with a warning alert that becomes critical after 3 failures in
  a row.
- **The window closes by itself** when the file is saved. Closing it early just makes the run
  relaunch Chrome.
- **Only complete, valid files reach the pipeline.** Chrome writes the download into
  `data/.downloads/`, which is cleared at the start of each run. The finished file is renamed
  (never copied) and checked: it must be a CSV whose header has `Lot number`, which rules out an HTML
  error page. It is then moved into the inbox as `salesdata_<UTC time>.csv` via a `.part`
  name, so the worker never reads a half-written file. An invalid file goes to
  `data/failed/downloader/` instead.

The pipeline's own checks still apply: identical files are skipped, a truncated file is
flagged by the row-count and per-yard guards, and "no new file for 75 min" catches a
downloader that silently stopped.

## Setup (macOS)

```bash
./start.sh                                   # starts everything, installs the downloader
cd backend && .venv/bin/python -m ingest.downloader login       # log in to Copart once
```

`login` opens a Chrome window on Copart. If Chrome fills in your saved email and password,
Sign in is clicked for you; otherwise log in as usual **and let Chrome save the password**:
that saved login is what lets later runs renew an expired session with no one around. The
window closes itself once the download button is reachable.

## Always running, restarted automatically

The downloader is a small always-on process (`python -m ingest.downloader daemon`):

- **launchd keeps it alive.** It starts when you log in to the Mac, and launchd restarts it
  within a minute if it crashes or is killed (`KeepAlive`).
- **`./start.sh` checks it.** If it is not installed, or installed in an older form, the
  script installs it (`SKIP_DOWNLOADER=1 ./start.sh` skips this).
- **It plans its own downloads.** Every hour plus a random 0–5 min, never within 45 min of
  the previous one, later after failures (backoff). If the Mac slept through a planned
  download, it downloads once, a minute after waking (so the network is back first).
- **It reports a heartbeat** every 30 s, with its plan, to the `downloader_status` table.

On the dashboard:
- **Overview** has a *Downloader running · next download at …* line next to the worker's.
  "Not running" or "paused" turns the pipeline health red.
- **Runs page:** a live countdown and a **Download now** button. The process picks the request
  up within seconds; the button isn't offered while downloads are paused for a login.
- **Alerts:** `downloader_down` (critical) when the heartbeat stops for 2.5 min; it resolves
  itself when the process is back. Right after the Mac wakes, the check waits a round so a
  sleeping Mac isn't reported as a failure.

Only one process uses the Chrome profile at a time (a lock), so a manual `run` or `login`
while the always-on process is downloading is refused with "try again in a minute".

What automation cannot do: if the Mac is **off, asleep or logged out**, nothing runs, not even
the dashboard, because Docker sleeps with it. Everything resumes on its own when you're back.

## Day to day

| Situation | What you see | What to do |
|---|---|---|
| Normal | A new run every hour on the dashboard | Nothing |
| Session expired | Nothing: the next run signs in with the saved login | Nothing |
| Saved login missing or rejected | Warning "…retrying (N/3 before pausing)", then critical "downloads paused" | Run `login` (save the password in Chrome) |
| Verification page (CAPTCHA, access denied) | Critical alert "downloads paused until you log in again" | Run `login` |
| Chrome crashed | `downloader.browser_retry` in the log; the run still succeeds | Nothing; after 3 crashes in one run, a failure alert |
| New pop-up that isn't closed | Download failures ("download button not found" or a click timeout) | Add its close button to `COPART_DISMISS_SELECTORS` |
| Download failing | Warning, then critical, "Copart download failed (N× in a row)" | Check `~/Library/Logs/cars-ingest/downloader.log`; if Copart changed the page, update the button text in `ingest/downloader/browser.py` |
| Mac asleep | One catch-up download a minute after waking | Nothing, or keep the Mac awake |
| Downloader not running | Critical alert, red line on Overview | Usually nothing: launchd restarts it. Otherwise `./start.sh` |

```bash
cd backend && .venv/bin/python -m ingest.downloader status   # last attempt/success, next run
cd backend && .venv/bin/python -m ingest.downloader run --force   # one download now (or the button)
./scripts/install_downloader.sh --uninstall                  # stop and remove the downloader
```

## Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `COPART_HEADED` | `true` | Show the Chrome window during downloads |
| `MIN_INTERVAL_MINUTES` | `45` | Never download more often than this |
| `MAX_BACKOFF_HOURS` | `6` | Longest wait between retries after errors |
| `DOWNLOAD_TIMEOUT_SECONDS` | `600` | Give up on a download after this long (files are ~100 MB) |
| `BROWSER_ATTEMPTS` | `3` | Chrome launches per run when it crashes or closes |
| `BROWSER_RETRY_SECONDS` | `20` | Pause before relaunching Chrome |
| `MAX_FAILED_LOGINS` | `3` | Runs in a row stuck on the login page before downloads pause |
| `ACTION_DELAY_MIN_SECONDS` / `ACTION_DELAY_MAX_SECONDS` | `1.5` / `4` | Random pause before each click and after each page load |
| `COPART_DISMISS_SELECTORS` | `[]` | Extra close buttons of pop-ups, as a JSON list of CSS selectors, e.g. `'["#promo .btn-close"]'` |
