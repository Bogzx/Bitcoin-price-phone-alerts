# Changelog

## Unreleased (2026-10-01)

### Added
- **Phone-number verification** with one-time SMS codes through Twilio Verify, or
  logged codes in `NOTIFY_DRY_RUN` mode. `REQUIRE_PHONE_VERIFICATION` defaults to the
  value of `ALLOW_REGISTRATION`.
  - Unverified users are redirected to `/verify-phone`, cannot create alerts, and their
    alerts never fire.
  - Limits: 3 codes per user per hour, 5 guesses per code, a 10-minute lifetime, and
    per-IP rate limits.
  - Local codes are stored as an HMAC keyed by `SECRET_KEY`.
- **Account settings** (`/settings`): change the phone number (which resets
  verification), change the password (which signs out other devices), delete the
  account. Every change needs the current password and is rate-limited.
- **Session tokens:** sessions and remember-me cookies identify the user by a random
  per-user token instead of the id, and user ids use SQLite AUTOINCREMENT. Before this,
  a deleted account's id went to the next registration, and the deleted user's other
  sessions opened that new account. Old databases get tokens and an AUTOINCREMENT
  `user` table on startup, with the id sequence above every id still referenced. Users
  are signed out once by the upgrade.
- **Registration never reopens by itself:** the deployment records that it had an owner,
  so deleting every account with `ALLOW_REGISTRATION=false` no longer hands it to the
  next visitor.
- `VERIFY_MAX_SENDS_PER_HOUR_TOTAL` (default 20) caps verification SMS for the whole
  deployment. Deleting an account keeps its notification log and sent-code rows,
  detached from any account (the code rows lose their phone numbers), so deleting and
  re-registering cannot reset the caps.
- A notification replayed after a restart is skipped when the user's current number is
  not verified.
- **Quiet band:** trade ticks that cannot change any alert skip the database (about
  2 ms → 0.02 ms per tick with 5 alerts). `ALERT_FULL_SCAN_SECONDS` bounds how long.
- `scripts/bench_ticks.py`, and the `docs/` screenshots taken from a dry-run instance.
- CI fails if a venv, a database or `.env` is tracked, and boots the Docker image.

### Changed
- `app.py` is now an entry point, and the application lives in the `btc_alerts`
  package with an app factory. Importing the package starts nothing.
- The default `PRICE_FEED_LOCK_PORT` moved from 47653 to 29653. 47653 is inside Linux's
  ephemeral port range, so a client socket holding it could stop the feed from starting.
  That failure is now logged as an error.
- Triggered alerts show "fired" instead of "sent". The delivery status is in the
  notification log; dry runs and queued deliveries were labelled "sent" before.

## 2026-09-30

- **Dead-feed detection:**
  - 20 s / 10 s pings, and a watchdog that recycles a socket silent for
    `FEED_STALE_SECONDS`;
  - exponential backoff with jitter;
  - a configurable `BINANCE_WS_URL` with a hint for HTTP 451;
  - `GET /healthz`, 503 when stale.
- **Durable notifications:**
  - a delivery log, replayed after a restart;
  - channels already delivered are never repeated;
  - re-arm hysteresis (`REARM_HYSTERESIS_PERCENT`);
  - a deployment-wide `MAX_NOTIFICATIONS_PER_DAY`;
  - a 30 s Twilio timeout;
  - `NOTIFY_DRY_RUN`.
- Registration is open only until the first account exists (`ALLOW_REGISTRATION`).
  POST-only logout, a minimum password length, and masked phone numbers in logs.
- Additive SQLite column upgrades for old databases.
- Flask 3.1 stack (`pip-audit` clean), Dockerfile, CI on Python 3.10/3.12/3.14, ruff.

## 2026-08-17

- **Toll-fraud brakes:**
  - a cap on active alerts per user;
  - a per-user notification cooldown;
  - E.164 phone numbers with an optional country-code allowlist.
- **Fixes:**
  - The feed started only under `python app.py`, so under gunicorn alerts never fired.
    It now starts in the serving process, guarded by a single-owner lock.
  - The reconnect loop no longer recurses until `RecursionError`.
  - Alert state is committed before the Twilio call, and failures are recorded and shown
    instead of swallowed.
- **Auth hardening:**
  - CSRF on every form;
  - secure, `SameSite=Lax` cookies;
  - login and registration rate limits;
  - authenticated Socket.IO with per-user rooms (events were broadcast to everyone).
- Calls speak the threshold and the price. There are now SMS and "both" channels,
  repeating alerts, and percent-change triggers.
- `.env` untracked.

## 2025-04

- First version: Flask, the Binance trade stream and Twilio calls.
