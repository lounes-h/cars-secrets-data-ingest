# Security

## Authentication

- One dashboard account (`DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD`). `start.sh` generates a
  random password into `.env` (mode 600).
- Login sets an `ingest_session` cookie: an itsdangerous-signed token (`SESSION_SECRET`,
  at least 32 characters) that is `HttpOnly` and `SameSite=Strict`. It is `Secure` when
  `COOKIE_SECURE=true`, and expires after 12 h. Renaming the account invalidates existing
  sessions.
- The API refuses to start without a password or with a short secret.
- Credentials are compared in constant time. After 10 failed logins, a client IP is blocked
  for 5 minutes (in-memory per API process). nginx overwrites `X-Forwarded-For` with the real
  peer address, so clients can't rotate it.
- `SameSite=Strict` plus JSON-only mutating endpoints protect against CSRF.

## Authorization surface

Only `/api/health` and `/api/auth/login` are public. Every other route, including the SSE
stream, is on a router that requires the session. New routes must be added to that router.

## Data handling

- CSV content is untrusted. It is stored as data, rendered by React as text, and never
  interpolated into SQL (all queries are parameterized). The only dynamic SQL uses constant
  column lists defined in code.
- The `retry` endpoint only moves files that resolve inside `FAILED_DIR` back into
  `INBOX_DIR/<known provider>/`, and never overwrites a file.
- nginx sends a strict Content-Security-Policy (`default-src 'self'`), `X-Frame-Options: DENY`,
  `nosniff` and `Referrer-Policy: same-origin`.

## Outbound requests (lot photos)

The API makes one kind of outbound request: listing a lot's photos. It uses only the image URL
stored for that lot, never a URL from the client. The provider code contacts only its
allow-listed API host (Copart: `inventoryv2.copart.io`, always over HTTPS) and returns only
image URLs on its image host (`cs.copart.com`). The response is capped at 2 MB with an 8 s
timeout. The CSP `img-src` allows that image host and nothing else, and images load with
`referrerPolicy="no-referrer"`.

## Auction data APIs

`python -m ingest.api_check` (run by hand, never by the worker or the API) calls
`apibara.tech` and `apiauctions.io` over HTTPS with the keys `APIBARA_API_KEY` and
`APIAUCTIONS_TOKEN`, which live in `.env` only and are passed to the backend containers by
Compose. Their answers are saved under `data/checks/` (git-ignored) and treated as data. They
include VINs, which are also in our own exports. `python -m ingest.apiauctions_import` (also by
hand) calls `apiauctions.io` with the same token and saves its pages under `data/apiauctions/`
(git-ignored); the lots it loads keep only the raw record's text in `attributes`, rendered as
text like any source value. Its pages include buyer ids and states, never names.

## AI damage analysis

`ANTHROPIC_API_KEY` lives in `.env` only and reaches the worker, the only process that calls
the model; the API only queues requests. The worker downloads a lot's photos from the URLs its
provider's `fetch_images` returns (allow-listed hosts only, HTTPS only, at most 15 MB each) and
sends shrunk copies with the lot's metadata to Anthropic. The metadata leaves out the VIN, the
seller, addresses and prices other than the estimated retail value. The model's answer is data:
it is validated against the schema before it is stored, and the page renders its text as text
and its boxes as positions over the auction's own photos. Requesting an analysis needs the
dashboard session like every other route; each request records the user who made it. Every
analysis spends API credit, so only a person triggers one.

## Copart session (downloader)

The downloader keeps Copart's session cookie in a dedicated Chrome profile at
`~/.cars-ingest/copart-profile` (mode 700), outside the repository. No Copart password is
stored by this project. A person logs in: either by typing, or by letting Chrome's own
password manager fill the form in that profile, after which `login` clicks Sign in once. The
downloader never types credentials and never interacts with CAPTCHAs or bot checks. Such a
page pauses it and raises an alert. Scheduled runs never attempt a login.

## API database

The worker writes the API's database (`API_DATABASE_URL`) and nothing else there: the
`public.lots` and `public.ingest_sync` tables it creates. It copies only the columns the API
serves: never `attributes`, the raw export rows. It refuses a URL whose `lots` table has the
ingest's own columns, so a misconfigured URL can never make a full copy delete the ingest's
lots. The URL holds the password and belongs in `.env` only. A hosted database needs
`?sslmode=require`. Sync errors are logged and alerted with libpq's message, which never
contains the password.

## Network

Compose publishes only the dashboard (8080), Postgres (5433) and the local API database
(5434), all on `127.0.0.1`. The API
is reachable only through nginx. Before exposing the dashboard beyond localhost, put it
behind HTTPS and set `COOKIE_SECURE=true`.

## Secrets

`.env`, `data/` and database dumps are git-ignored and must never be committed.
