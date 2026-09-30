# 📈 Bitcoin Price Alert Service

![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)
![Python 3.10+](https://img.shields.io/badge/Python-3.10+-blue.svg)
![Flask](https://img.shields.io/badge/Flask-3.1-red.svg)
[![CI](https://github.com/Bogzx/Bitcoin-price-phone-alerts/actions/workflows/ci.yml/badge.svg)](https://github.com/Bogzx/Bitcoin-price-phone-alerts/actions/workflows/ci.yml)

A real-time Bitcoin price monitoring service that calls your phone when BTC crosses your specified price thresholds.

## ⚡ Quick start (no Twilio needed)

Dry-run mode logs every alert instead of calling, so you can try the whole app in a
couple of minutes without a Twilio account:

```bash
git clone https://github.com/Bogzx/Bitcoin-price-phone-alerts.git
cd Bitcoin-price-phone-alerts
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
SECRET_KEY=dev NOTIFY_DRY_RUN=true SESSION_COOKIE_SECURE=false python app.py
```

Open <http://localhost:5000>, register (the first account is yours; registration then
closes), add an alert a few dollars from the live price, and watch the terminal for
`[dry-run] Would call +1******0123: Bitcoin rose above ...`.

Or with Docker:

```bash
docker build -t btc-alerts .
docker run -p 5000:5000 -v btc-alerts-data:/data \
  -e SECRET_KEY=change-me -e NOTIFY_DRY_RUN=true -e SESSION_COOKIE_SECURE=false btc-alerts
```

In the US, add `-e BINANCE_WS_URL=wss://stream.binance.us:9443/ws/btcusdt@trade`
(see [Binance endpoints](#binance-endpoints)).

![Bitcoin Price Alert Dashboard](https://github.com/Bogzx/Bitcoin-price-phone-alerts/blob/main/Screenshot%202025-04-23%20173519.png?raw=true)

## 📌 Status

This is a learning project. It works on a single machine, and it is **not ready for a
public deployment** as-is.

Before exposing it to the internet you would need, at minimum:

- **Phone number verification.** The phone number is whatever the registrant types;
  nothing verifies that they own it. Twilio Verify (OTP at registration) is the missing
  piece. Registration is therefore closed after the first account unless you set
  `ALLOW_REGISTRATION=true`, and anyone you let register can make this deployment call
  an arbitrary number on your Twilio balance.
- **A spend limit on the Twilio account.** The in-app limits below reduce the blast
  radius; only a Twilio-side budget cap bounds it.

**Fixed in this repo:**

- A hard cap on active alerts per user (`MAX_ACTIVE_ALERTS_PER_USER`, default 5) and a
  per-user notification cooldown (`NOTIFY_COOLDOWN_SECONDS`, default 300s), so one
  account can no longer queue hundreds of calls off a single price move.
- Phone numbers must be valid E.164, with an optional country-code allowlist
  (`ALLOWED_PHONE_COUNTRY_CODES`), which is the cheapest brake on international
  premium-rate abuse.
- The price feed now starts at import time (it previously started only under
  `python app.py`, so under gunicorn alerts silently never fired), guarded by a
  single-owner lock so multiple workers cannot place duplicate calls.
- The Binance reconnect is a loop rather than recursion, so a long-lived instance no
  longer dies of `RecursionError` after enough reconnects.
- Alert state is committed *before* the Twilio call is dispatched, and delivery runs on
  a retrying background worker; a Twilio failure is recorded on the alert and shown in
  the UI instead of being swallowed.
- CSRF protection on every form, secure/`SameSite=Lax` cookies, rate limits on
  login/registration, and authenticated Socket.IO with per-user rooms (alert events used
  to be broadcast, with `user_id`, to every connected client).
- `.env` is no longer tracked by git, and `SECRET_KEY` is documented in `.env.example`.
- **Dead-feed detection.** A socket that stops delivering trades for
  `FEED_STALE_SECONDS` (default 60) is torn down and reconnected, with exponential
  backoff and jitter on repeated failures. `/healthz` returns 503 whenever there is no
  recent price, so an external uptime monitor can tell you alerts are not firing.
- **Hysteresis** on repeating alerts (`REARM_HYSTERESIS_PERCENT`, default 0.25%), so
  chop around a threshold does not produce a call every cooldown period.
- **Registration closes after the first account** (`ALLOW_REGISTRATION`) and a
  deployment-wide `MAX_NOTIFICATIONS_PER_DAY` (default 50) bounds the Twilio bill.
- A delivery log (sent / failed / dry run) on the dashboard, `NOTIFY_DRY_RUN` for
  running without Twilio, and phone numbers masked in logs.

**Known limitations (not fixed):**

- **No phone verification (OTP).** It needs a two-step registration flow; it is the top
  follow-up.
- **Single process only.** The current price and the Socket.IO state live in process
  memory, so run exactly one worker (`gunicorn -w 1 --threads 50 app:app`, as the
  Dockerfile does, or `python app.py` for local use). The app uses Socket.IO's
  `threading` mode, so do not use the eventlet/gevent worker classes. Extra workers will not run the feed (the lock prevents duplicate
  calls) but they will not see live prices either. Multi-worker support needs a Redis
  message queue and a shared price cache.
- **Every alert is re-evaluated on every trade tick** with a full table scan. Fine for a
  handful of users, not for many.
- No real migration tool. On SQLite, missing tables are created and missing columns
  are added automatically at startup (additive only), so an `alerts.db` from an older
  version keeps working. On other databases, add new columns by hand or adopt
  Flask-Migrate.

## ✨ Features

- **Real-time Bitcoin Price Tracking** via Binance WebSocket API
- **Customizable Price Alerts** for prices going above or below your thresholds
- **Percent-change Triggers** (e.g. "+5% from the current price"), converted to an
  absolute threshold at creation time
- **Repeating Alerts** that stay active and re-arm once the price leaves the trigger zone
- **Phone Call or SMS Notifications** via Twilio, carrying the threshold and the price
- **Delivery history** on the dashboard, and a dry-run mode that needs no Twilio account
- **Health endpoint** (`/healthz`) for uptime monitoring of the price feed
- **User Authentication** with secure login system
- **WebSocket Updates** for live UI updates without page refreshes

## 🚀 Installation

### Prerequisites

- Python 3.10 or higher
- Twilio account for phone notifications (not needed with `NOTIFY_DRY_RUN=true`)

### Step-by-Step Setup

1. **Clone the repository**

   ```bash
   git clone https://github.com/Bogzx/Bitcoin-price-phone-alerts.git
   cd Bitcoin-price-phone-alerts
   ```

2. **Create and activate a virtual environment**

   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

4. **Set up environment variables**

   Copy `.env.example` to `.env` and fill it in:

   ```bash
   cp .env.example .env
   ```

   ```
   SECRET_KEY=your_secure_random_key
   DATABASE_URL=sqlite:///alerts.db
   TWILIO_ACCOUNT_SID=your_twilio_sid
   TWILIO_AUTH_TOKEN=your_twilio_auth_token
   TWILIO_PHONE_NUMBER=your_twilio_phone_number
   # Required for local HTTP; the browser drops secure cookies over http://
   SESSION_COOKIE_SECURE=false
   ```

   `.env` is gitignored. Never commit it: a real `TWILIO_AUTH_TOKEN` in a public repo
   is someone else's phone bill.

## 🖥️ Usage

1. **Start the application**

   ```bash
   python app.py                                  # local development
   gunicorn --workers 1 --threads 50 -b 0.0.0.0:5000 app:app   # server
   ```

2. **Access the web interface**

   Open your browser and navigate to: `http://localhost:5000`

3. **Register an account**

   The phone number must be in international (E.164) format, e.g. `+14155550123`.
   Only the first account can register; set `ALLOW_REGISTRATION=true` to allow more.

4. **Create price alerts**

   - Click "Add Alert" button
   - Enter an absolute price threshold, or a percent change from the current price
   - Choose call, SMS, or both, and whether the alert should repeat
   - The system automatically determines if it's an "above" or "below" alert based on
     current price. Alerts cannot be created before the first price arrives.

5. **Receive notifications**

   When Bitcoin crosses your set thresholds, you'll receive a phone call or SMS via
   Twilio, quoting the threshold and the current price.

### Running the tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q
ruff check .
```

The tests use a fake Twilio client and a fake Binance WebSocket; no network, no calls,
no credentials. CI runs them on Python 3.10, 3.12 and 3.14.

## ⚙️ Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `SECRET_KEY` | Flask secret key for sessions | None (required) |
| `DATABASE_URL` | Database connection string | `sqlite:///alerts.db` |
| `TWILIO_ACCOUNT_SID` | Twilio Account SID | None (required unless dry run) |
| `TWILIO_AUTH_TOKEN` | Twilio Auth Token | None (required unless dry run) |
| `TWILIO_PHONE_NUMBER` | Twilio Phone Number | None (required) |
| `NOTIFY_DRY_RUN` | Log alerts instead of calling/texting; no Twilio needed | `false` |
| `ALLOW_REGISTRATION` | Let anyone register. When `false`, only the first account can | `false` |
| `MAX_NOTIFICATIONS_PER_DAY` | Deployment-wide cap on calls/SMS per rolling 24h (0 = none) | `50` |
| `REARM_HYSTERESIS_PERCENT` | How far (in % of the threshold) the price must retreat before a repeating alert re-arms | `0.25` |
| `BINANCE_WS_URL` | Binance trade stream URL | `wss://stream.binance.com:9443/ws/btcusdt@trade` |
| `FEED_STALE_SECONDS` | Seconds without a trade before the socket is recycled and `/healthz` reports stale | `60` |
| `FEED_RECONNECT_BASE_SECONDS` / `FEED_RECONNECT_MAX_SECONDS` | Reconnect backoff bounds | `1` / `60` |
| `LOG_LEVEL` | Python log level | `INFO` |
| `SESSION_COOKIE_SECURE` | Send session/remember cookies over HTTPS only. Set `false` for local HTTP | `true` |
| `MAX_ACTIVE_ALERTS_PER_USER` | Cap on untriggered alerts per account | `5` |
| `NOTIFY_COOLDOWN_SECONDS` | Minimum seconds between two notifications for one user | `300` |
| `REPEAT_ALERT_COOLDOWN_SECONDS` | Minimum seconds before a repeating alert fires again | `900` |
| `ALLOWED_PHONE_COUNTRY_CODES` | Optional E.164 country code allowlist, e.g. `1,44,40` | empty (any) |
| `RUN_PRICE_FEED` | Whether this process may run the Binance feed | `true` |
| `PRICE_FEED_LOCK_PORT` | Localhost port used as the single-owner lock for the feed | `47653` |
| `CORS_ALLOWED_ORIGINS` | Comma separated Socket.IO origins | `http://localhost:5000,http://127.0.0.1:5000` |
| `LOGIN_RATE_LIMIT` | Flask-Limiter expression for `POST /login` | `10 per minute; 60 per hour` |
| `REGISTER_RATE_LIMIT` | Flask-Limiter expression for `POST /register` | `5 per hour` |

### Twilio Setup

1. Create a [Twilio account](https://www.twilio.com/try-twilio)
2. Get your Account SID and Auth Token from the dashboard
3. Purchase or use an existing Twilio phone number
4. Add these credentials to your `.env` file

### Binance endpoints

| Where you run it | `BINANCE_WS_URL` |
|---|---|
| Outside the US (default) | `wss://stream.binance.com:9443/ws/btcusdt@trade` |
| Outside the US, market-data-only mirror | `wss://data-stream.binance.vision/ws/btcusdt@trade` |
| United States | `wss://stream.binance.us:9443/ws/btcusdt@trade` |

`stream.binance.com` answers **HTTP 451** to US IP addresses (and `binance.us` answers
451 outside the US). The app logs a specific hint when it sees a 451 and keeps retrying
with backoff; `/healthz` stays 503 until prices arrive.

### Monitoring

`GET /healthz` (no login) returns `200 {"status": "ok", ...}` while prices are
arriving and `503 {"status": "stale", ...}` otherwise. Point any uptime checker at it:
a dead feed means no alert can fire, and nothing else would tell you.

## 🔍 How It Works

- Bitcoin prices are obtained from Binance's WebSocket API in real-time. The socket
  pings every 20s, a watchdog recycles it after `FEED_STALE_SECONDS` of silence, and
  reconnects back off exponentially (1s to 60s) while Binance is unreachable
- When a price threshold is crossed, the alert state is committed first
- The Twilio call or SMS is then dispatched by a background worker, which retries and
  records the error on the alert if delivery fails
- The UI is updated in real-time (per-user Socket.IO rooms) to show triggered alerts

The Binance listener runs in exactly one process, enforced by binding
`PRICE_FEED_LOCK_PORT` on localhost: a second worker finds the port taken and skips the
feed rather than placing duplicate calls.

## 📁 Project Structure

```
Bitcoin-price-phone-alerts/
├── app.py               # Main application entry point
├── config.py            # Configuration settings
├── models.py            # Database models
├── requirements.txt     # Python dependencies (requirements-dev.txt adds pytest, ruff)
├── Dockerfile           # Single-worker gunicorn image with /healthz HEALTHCHECK
├── .env.example         # Example environment variables
├── static/              # Static assets (CSS, JS)
├── templates/           # HTML templates
└── tests/               # Pytest suite (fake Twilio client, fake Binance socket)
```

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## ❓ FAQ

### Is this service free to use?

The software is free and open-source. However, you will need your own Twilio account for phone call notifications, which may incur charges.

### How accurate are the price alerts?

The alerts are based on real-time trades from Binance and trigger on the first trade at
or beyond your threshold; the notification is dispatched within a second or so, plus
Twilio's call setup time. If the feed is down, alerts fire on the first trade after it
recovers (if the price is still beyond the threshold), and `/healthz` reports the outage.

---

