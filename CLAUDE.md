# Cars Secrets Data Ingest

Continuous ingestion of vehicle-auction CSV exports into PostgreSQL, with a React dashboard
for monitoring and alerts. Copart is the first provider; others (IAAI, …) plug in without a
migration. The Copart downloader (`ingest/downloader`, runs on the host via launchd) saves
an export into `data/inbox/<provider>/` every hour; saving the file triggers ingestion.

## Tech stack

- **Backend** (`backend/`): Python 3.12, psycopg 3 (`COPY` for bulk loads), FastAPI + uvicorn,
  watchdog (file events), pydantic-settings, Alembic, structlog. Package: `ingest`.
- **Database**: PostgreSQL 17.
- **Frontend** (`frontend/`): React 19 + TypeScript, Vite, TanStack Query, React Router,
  Recharts. Served by nginx, which also proxies `/api`.
- **Runtime**: Docker Compose — `db`, `migrate` (one-shot), `worker`, `api`, `web`.
- **Tests**: pytest + testcontainers (real Postgres), Vitest + Testing Library.

## Commands

```bash
./start.sh                      # build + start everything; creates .env with secrets on first run
./start.sh --logs               # same, then follow worker/api logs
docker compose down             # stop (add -v to also drop the database)
python3 scripts/simulate_export.py   # drop a realistic "next hour" export into the inbox
./scripts/install_downloader.sh      # (re)install the always-on Copart downloader; start.sh does it
(cd backend && .venv/bin/python -m ingest.downloader login|run --force|status|daemon)

# backend (from backend/)
uv sync                         # create .venv with dev dependencies
.venv/bin/pytest                # all tests; starts a throwaway Postgres via Docker
TEST_DATABASE_URL=postgresql://… .venv/bin/pytest   # reuse an existing Postgres (it is wiped)
.venv/bin/ruff check ingest tests migrations

# frontend (from frontend/)
npm ci
npm test                        # vitest
npm run typecheck
npm run build
npm run dev                     # dev server on :5173, proxies /api to localhost:8000
```

## Architecture rules (important)

- **Flow**: `worker` watches `inbox/<provider>/` (filesystem events + a 30 s rescan safety
  net), waits until the file stops changing, then `pipeline.process_file` registers it in
  `ingest_files`, skips byte-identical files (sha256), and `loader.load_file` loads it in
  **one transaction**: COPY into a temp `stage` table → change detection → `lot_versions`
  snapshot → upsert `lots` → removals → column registry. Success archives the file gzipped;
  failure moves it to `failed/` and raises a critical alert. Never split the load across
  transactions: a lot must never be half-updated.
- **Provider-neutral schema**: `lots` has typed canonical columns (`schema.CANONICAL_COLUMNS`,
  must match the migration — a test enforces it) keyed by `(source, lot_number)`, plus
  `attributes` JSONB holding every source column. Provider specifics live only in
  `ingest/providers/<name>.py` (header aliases, key parsing, derived columns, ignored and
  volatile columns, removal scope). Adding a provider = new module + registry entry, no
  migration. Adding a canonical column = migration + `schema.py` + mappings.
- **Schema drift never fails a file**: unknown columns go to `attributes` and the registry
  with a warning; a missing mapped column becomes NULL with a warning. Only a missing lot
  key rejects the file. Renamed headers are handled by adding an alias to the provider.
- **Malformed rows**: Copart does not escape quotes inside values (`29'9"`);
  `Provider.split_record` re-reads such records Copart's way, kept only when the field count
  matches. A row with the wrong field count is rejected (never loaded shifted), but its lot
  is not treated as missing for removals.
- **Change detection**: row hash over canonicalized, non-volatile attributes (`Id` is
  ignored, `Create Date/Time` is volatile, `8119.0` == `8119`). If the column set changed
  since the last file, only columns present in both files are compared, so drift never marks
  every lot as updated. Only real changes write `lot_versions` (full history).
- **Removals are scoped and wait for the auction**: Copart downloads can be a subset of
  yards, so a lot missing from a file is marked removed only if its yard
  (`Provider.removal_scope`) is in the file **and** the file holds ≥ 50 % of that yard's
  active lots (otherwise the yard is skipped + info alert), **and** its `sale_at` +
  `REMOVAL_GRACE_MINUTES` (120) has passed — bidding ends after the sale opens. Deferred lots
  stay active and are removed by the first later file that still lacks them. Lots without a
  sale time have no pending auction and are removed immediately.
- **Lot photos**: `Provider.fetch_images` lists photos on demand (API
  `/api/lots/{source}/{lot}/images`, cached 15 min). It only uses URLs stored for the lot and
  only contacts/returns allow-listed hosts (Copart: `inventoryv2.copart.io` for the list,
  `cs.copart.com` for images, which the CSP `img-src` allows). Browsers load images directly.
- **Alerts**: rules are pure functions (`alerts.evaluate_*`, `monitor.check_*`) so they are
  unit-testable; storage dedupes by `fingerprint` (one open alert per condition). Conditions
  that can clear (worker down, no new file, missing column) are auto-resolved. The API
  process runs the periodic checks, so a dead worker is still reported.
- **Live dashboard**: every state change calls `db.notify` (`pg_notify`) inside the same
  transaction; the API LISTENs and fans out over SSE (`/api/events`); the UI invalidates
  queries per event type (`frontend/src/lib/events.ts`).
- Timestamps that measure work use `clock_timestamp()`, not `now()` (transaction start).
- **Downloader** (`ingest/downloader`, see `docs/downloader.md`): real Chrome via Playwright
  with a persistent profile; a person logs in once with `login` and saves the password in
  Chrome. Pure logic lives in `core.py` (tested with a fake browser); `browser.py` is the
  only code that touches Copart. It must never type or store credentials, solve or bypass a
  CAPTCHA/bot check, rotate IPs or spoof fingerprints. It runs unattended: when the session
  expired, a run clicks "Sign in" **once** if Chrome's password manager filled both fields; a
  login page it cannot pass is a failed run, and `MAX_FAILED_LOGINS` (3) in a row pause
  downloads (account lockout). A challenge page pauses at once. Pauses raise a critical alert
  until a person runs `login`. Pop-ups are closed only through close-type controls
  (`core.CLOSE_LABEL`, never accept/agree), never on a challenge page;
  `COPART_DISMISS_SELECTORS` adds selectors. It waits a random 1.5–4 s before each click. A
  Chrome crash relaunches it within the run (`BROWSER_ATTEMPTS`), after stopping orphaned
  Chrome processes on the profile. The export is read from the response over CDP
  (`Fetch.takeResponseBodyAsStream`), never through Chrome's download manager, which crashes
  Chrome 153 on macOS when finalizing large files. Browser tests use a fake Copart on `copart.test` behind a catch-all route (no
  HTTP redirects, which bypass routing), so they never reach the real site. Pacing (≥ 45 min between downloads, exponential backoff) is enforced in
  code, not only by the scheduler. Downloads are validated, then handed to the inbox via a
  `.part` rename. It runs as an always-on process (`daemon.py`, launchd `KeepAlive`, installed
  or updated by `start.sh`) that plans its own downloads, heartbeats and publishes its state to
  `downloader_status` (best effort); the dashboard's "Download now" sets `run_requested_at`,
  which it picks up within seconds. A file lock keeps one Chrome per profile.

## Security rules

- Never commit `.env`, real exports under `data/`, or database dumps. `.env` is generated by
  `start.sh` with random secrets and mode 600.
- Every `/api` route except `/api/health` and `/api/auth/login` requires the session cookie
  (signed with `SESSION_SECRET`, http-only, `SameSite=Strict`). New routes go on the
  authenticated router in `api/app.py`, never on `public`.
- The API refuses to start without `DASHBOARD_PASSWORD` or with a `SESSION_SECRET` under 32
  characters. Credentials are compared in constant time; failed logins are rate-limited per
  client IP (nginx overwrites `X-Forwarded-For`, so it cannot be spoofed).
- SQL is always parameterized. The only f-string SQL interpolates code constants (column
  lists, the change predicate) — never request or file data.
- File operations from the API stay inside configured directories (`retry` checks the path
  is under `FAILED_DIR`). CSV content is data: in the UI render it as text, never HTML.
- Ports bind to `127.0.0.1` only. Set `COOKIE_SECURE=true` when serving over HTTPS.
- The downloader's Chrome profile (`~/.cars-ingest/copart-profile`, mode 700) holds the Copart
  session cookie: keep it out of the repo and never copy it elsewhere.

## Migrations

- Alembic, raw SQL via `op.execute`, in `backend/migrations/versions/NNNN_description.py`.
  Migrations must not import application code (they are frozen history).
- Applied automatically by the `migrate` service before `worker`/`api` start, or manually:
  `cd backend && DATABASE_URL=… .venv/bin/alembic upgrade head`.
- Never edit a migration that has run anywhere shared; add a new one. Large backfills (e.g.
  promoting an `attributes` key to a typed column) go in batches.

## Tests

Always add tests when adding or changing behaviour. Tests run against a real
Postgres, not SQLite — the schema uses partial unique indexes and `now()`
semantics that SQLite does not share.

`backend/tests/fixtures/sample.csv` holds 300 real Copart rows plus 2 malformed (shifted)
rows; build variations with `make_export` in `conftest.py` rather than adding fixtures.

## Unit Test Requirements

Whenever you make any change to application code, you must add or update unit tests that cover the changed behavior.

This applies to:

- New features
- Bug fixes
- Refactors
- Business logic changes
- Validation changes
- Error handling changes
- API behavior changes
- Utility/helper changes
- Data transformation changes

For every code change:

1. Identify the behavior that changed.
2. Add or update unit tests covering that behavior.
3. Include success, failure, and edge cases when relevant.
4. Run the relevant unit tests.
5. Make sure existing related tests still pass.

### Bug fixes

When fixing a bug, add a regression test that:

1. Reproduces the incorrect behavior.
2. Would fail before the fix.
3. Passes after the fix.

### Refactors

If existing tests already fully cover the affected behavior, new tests are not required solely because code was refactored.

However, if coverage is missing or weak, add the necessary tests.

### Exceptions

Unit tests may be skipped only when they are not reasonably applicable, such as:

- Documentation-only changes
- Comment-only changes
- Formatting-only changes
- Static asset changes
- Pure configuration changes with no testable code behavior

Do not skip tests simply because a change is small.

If no unit test is added, explicitly explain why in the final response.

A code change is not considered complete until the relevant unit tests have been added or updated and executed successfully.

## Current state

- Working end to end: watcher → ingest → Postgres, change history, schema-drift handling,
  yard-scoped removals deferred until the auction is over, alerts (dashboard only, no
  email/Slack by decision), dashboard with login, live updates, run retry, lot explorer with
  photos. One provider: Copart.
- Measured: 142k-row export (89 MB) ingests in ~12 s first time; a 24k-row export in ~3.5 s.
- Real exports differ in size: 2026-09-15 had 142k rows / 224 yards, 2026-09-17 had 24.5k
  rows / 44 yards. That is why removals are scoped per yard and the row-count warning fires
  when download size changes; tune `ROW_COUNT_DEVIATION_RATIO` if it is noisy.
- Single worker by design (per-provider advisory lock makes a second one safe but idle).
- Copart downloader built (always-on under launchd, signs in with Chrome's saved login,
  closes pop-ups, relaunches Chrome after crashes; pauses and alerts on bot pages or a saved
  login that keeps failing).
  Copart's Image and Data License Agreement governs the data and photos; the owner is
  checking that automated downloads are allowed.
- Not done yet: external alert channels, a second provider.

## Branching

Create a branch before starting work. The first Git action of a task is creating its branch, not committing.

`main` is the production branch — what ships. That is the whole reason task
work never lands on it directly: a commit there is a release, not a save.
Render deploys `main`, so a push to it goes live.

Check where the working tree is first:

```bash
git branch --show-current
```

Always branch off `main`, whatever happens to be checked out. A branch cut from
another task's branch carries that task's unmerged commits into this one's diff
and its pull request.

```bash
git checkout main
git pull
git checkout -b <prefix>/<short-name>
```

Pull `main` before cutting the branch, every time — the `git pull` above is not
optional. Branching from a stale `main` puts commits somebody else already
merged into this task's diff, and turns the eventual merge into a conflict to
resolve rather than a fast-forward.

Do this before making any changes, so there is nothing to carry across.

Already on the branch for this task: stay on it rather than creating another.

Name the branch after the work, using the same prefixes as the commit messages:

- `feat/user-authentication`
- `fix/expired-access-tokens`
- `docs/api-setup-instructions`

Do not commit task work directly to `main`. This is the repository workflow that
the branch rule under "Push Rules" refers to. `main` is what a branch is cut
FROM; what it is pushed TO is always the current branch — see "Push Rules".

## Git Workflow Requirements

For every coding task in this repository, completing the implementation is **not enough**. The task is only considered complete after the changes have been committed and successfully pushed to the configured GitHub repository.

### Required workflow

After making the requested changes:

1. Run the appropriate tests, type checks, linting, formatting, or build commands for the project.
2. Review the Git diff and verify that only intended changes are included.
3. Check `git status` before committing.
4. Stage the relevant files.
5. Create a Git commit with a clear, concise commit message describing the change.
6. Push the commit to the current remote branch on GitHub.
7. Verify that the push succeeded.
8. Open a pull request for the branch, or update the one it already has.
9. Report the commit hash, the branch name and the pull request URL in the final
   response.

Do not stop after editing files, after creating a local commit, or after
pushing without a pull request.

## Commit Rules

Use descriptive commit messages.

Prefer messages such as:

- `feat: add user authentication`
- `fix: handle expired access tokens`
- `refactor: simplify payment validation`
- `test: add coverage for checkout flow`
- `docs: update API setup instructions`
- `chore: update dependencies`

Do not use vague commit messages such as:

- `changes`
- `updates`
- `fix`
- `work`
- `stuff`

Before committing, inspect the diff so accidental files are not included.

## Push Rules

After committing, push the commit to the GitHub remote.

Determine the current branch with:

```bash
git branch --show-current
```

Determine the configured remote with:

```bash
git remote -v
```

Normally push using:

```bash
git push
```

If the branch does not yet have an upstream, use:

```bash
git push -u origin <branch-name>
```

Do not assume the branch is `main`.

Do not create or switch branches unless the task requires it or the existing repository workflow clearly expects it.

## Pull Request Rules

Every push of task work is followed by a pull request. A pushed branch with no
PR is work that reaches nobody: `main` is what ships, and a pull request is the
only way a branch gets there.

Open it with the `gh` CLI, immediately after the push has succeeded:

```bash
gh pr create --base main --head "$(git branch --show-current)" \
  --title "<same prefix and subject as the commit>" \
  --body "<what changed, why, and what validation was run>"
```

- **One pull request per branch.** A push to a branch that already has an open
  PR updates that PR — check with `gh pr view` first and never open a second.
- **The base is always `main`**, never another task's branch.
- The title uses the same prefix as the commits (`feat:`, `fix:`, `docs:`, …).
- The body says what changed, why, and how it was validated.
- **Never merge the pull request.** Opening it is the end of the task; merging
  is the user's decision.
- If `gh` is not authenticated, or the remote refuses the pull request, stop and
  report the exact blocker together with the branch that was pushed — the same
  as a failed push.

Report the pull request URL in the final response.

## Safety Rules

Never use destructive Git commands unless explicitly requested.

Do **not** use:

```bash
git push --force
git push --force-with-lease
git reset --hard
git clean -fd
```

Do not rewrite published Git history.

Do not amend an existing commit unless explicitly requested.

Do not overwrite, revert, discard, or commit unrelated changes made by the user or another agent.

If unrelated uncommitted changes already exist, preserve them and commit only the files or hunks associated with the current task.

## Secrets and Sensitive Files

Before committing, make sure no secrets or sensitive information have been introduced.

Never commit files containing credentials such as:

- `.env`
- API keys
- access tokens
- private keys
- passwords
- cloud credentials
- GitHub tokens
- production secrets

If sensitive information is discovered, do not commit or push it. Remove it from the proposed changes and notify the user.

## Validation Before Commit

Before committing, run the most relevant validation available for the repository.

Examples include:

```bash
npm test
npm run lint
npm run typecheck
npm run build
```

or the equivalent commands for the project's language and tooling.

Use the repository's existing scripts and documentation rather than inventing new validation commands.

If a validation command fails because of the changes you made, fix the problem before committing.

If validation cannot be run because of an environment limitation or an unrelated pre-existing issue, clearly mention that in the final response. This should not prevent committing and pushing otherwise valid requested changes unless doing so would create a broken repository.

## Handling Push Failures

If `git push` fails:

1. Read the Git error.
2. Diagnose whether the issue is authentication, upstream configuration, remote changes, permissions, branch protection, or another problem.
3. Resolve non-destructive issues when possible.
4. Retry the push.

Do not use force push to work around a rejected push.

If the remote branch contains new commits, fetch the latest state and integrate it safely without destroying local or remote work.

If GitHub authentication, repository permissions, branch protection, or another external restriction prevents the push, stop and clearly explain the exact blocker.

A local commit alone does **not** count as successful completion when pushing is possible.

## Documentation Requirements

Documentation is part of the change, not a follow-up. Whenever a change makes an
existing document wrong, incomplete or misleading, update it in the same commit.

Update the document that owns the subject:

| Subject | Document |
|---|---|
| rules, commands, current state | `CLAUDE.md` |
| layering, the feature registry, sync collections | `docs/architecture.md` |
| auth, join codes, isolation, anything returning household data | `docs/security.md` |

Prefer correcting the existing document to adding a new one. A stale sentence is
worse than no sentence, because it is trusted — so delete what stopped being
true rather than appending the new version next to it.

A contract change is the one that reaches furthest: renaming a field or changing
a status code makes the app's documentation wrong as well as this repository's.
Say so in the pull request even when the other repository's change is not yours
to make.

Not every change needs a documentation update: a refactor with no visible
behaviour change, a test-only change or a typo fix usually does not. If nothing
needed updating, say so explicitly in the final response.

## Definition of Done

A coding task is complete only when all applicable items below are true:

- The requested implementation is finished.
- Relevant validation has been run.
- The documentation affected by the change has been updated, or the final
  response states that no document was affected.
- The Git diff has been reviewed.
- Relevant changes have been committed, documentation included.
- The commit has been successfully pushed to GitHub.
- A pull request has been opened for the branch, or the branch's existing pull
  request has been updated by the push.
- The final response identifies the pushed branch, the commit and the pull
  request URL.

At the end of the task, provide a concise summary similar to:

```text
Implemented the requested changes.

Validation:
- Tests passed
- Lint passed

Docs:
- Updated docs/security.md  (or: no documentation affected)

Git:
- Branch: feature/example
- Commit: a1b2c3d
- Pushed to: origin/feature/example
- Pull request: https://github.com/<owner>/<repo>/pull/123
```

Unless explicitly instructed otherwise, always update the affected
documentation, commit, push and open a pull request for completed coding
changes.
