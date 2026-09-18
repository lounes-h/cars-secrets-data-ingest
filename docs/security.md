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

## Network

Compose publishes only the dashboard (8080) and Postgres (5433), both on `127.0.0.1`. The API
is reachable only through nginx. Before exposing the dashboard beyond localhost, put it
behind HTTPS and set `COOKIE_SECURE=true`.

## Secrets

`.env`, `data/` and database dumps are git-ignored and must never be committed.
