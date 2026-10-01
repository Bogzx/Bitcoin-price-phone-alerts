"""Configuration, read from the environment each time an app is created.

Nothing here runs at import time: `create_app()` calls `load_config()`, and the
entry point (`app.py`) loads `.env` before that.
"""

import os


def _env_bool(env, name, default):
    """Reads a boolean-ish environment variable."""
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(env, name, default):
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(env, name, default):
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_list(env, name, default=""):
    return [item.strip() for item in env.get(name, default).split(",") if item.strip()]


def load_config(env=None):
    """Returns the app configuration as a dict, read from `env` (os.environ)."""
    env = os.environ if env is None else env
    session_cookie_secure = _env_bool(env, "SESSION_COOKIE_SECURE", True)
    return {
        # Required. Without it create_app() generates a random key per start,
        # which logs out every user and voids every remember-me cookie.
        "SECRET_KEY": env.get("SECRET_KEY") or None,
        "SQLALCHEMY_DATABASE_URI": env.get("DATABASE_URL", "sqlite:///alerts.db"),
        "SQLALCHEMY_TRACK_MODIFICATIONS": False,
        "LOG_LEVEL": env.get("LOG_LEVEL", "INFO").upper(),

        # --- Twilio ------------------------------------------------------------------
        "TWILIO_ACCOUNT_SID": env.get("TWILIO_ACCOUNT_SID"),
        "TWILIO_AUTH_TOKEN": env.get("TWILIO_AUTH_TOKEN"),
        "TWILIO_PHONE_NUMBER": env.get("TWILIO_PHONE_NUMBER"),
        # Log alerts instead of calling Twilio. Lets you run and demo the whole app
        # without a Twilio account or spending money.
        "NOTIFY_DRY_RUN": _env_bool(env, "NOTIFY_DRY_RUN", False),

        # --- Cookie / session hardening ----------------------------------------------
        # Secure cookies are the default. Plain HTTP (the localhost setup in the
        # README) needs SESSION_COOKIE_SECURE=false, otherwise the browser drops the
        # session cookie and login "fails" silently.
        "SESSION_COOKIE_SECURE": session_cookie_secure,
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "REMEMBER_COOKIE_SECURE": session_cookie_secure,
        "REMEMBER_COOKIE_HTTPONLY": True,
        "REMEMBER_COOKIE_SAMESITE": "Lax",

        # --- Abuse / spend limits ----------------------------------------------------
        # Every alert is a billable phone call to a number the registrant typed.
        "MAX_ACTIVE_ALERTS_PER_USER": _env_int(env, "MAX_ACTIVE_ALERTS_PER_USER", 5),
        # Minimum seconds between two outbound notifications for the same user.
        "NOTIFY_COOLDOWN_SECONDS": _env_int(env, "NOTIFY_COOLDOWN_SECONDS", 300),
        # Deployment-wide cap on calls/SMS in any rolling 24 hours. 0 = no cap.
        "MAX_NOTIFICATIONS_PER_DAY": _env_int(env, "MAX_NOTIFICATIONS_PER_DAY", 50),
        # Registration is open only while no account exists (the owner's first
        # signup), unless this is true.
        "ALLOW_REGISTRATION": _env_bool(env, "ALLOW_REGISTRATION", False),
        # Minimum seconds before a repeating alert can fire again.
        "REPEAT_ALERT_COOLDOWN_SECONDS": _env_int(env, "REPEAT_ALERT_COOLDOWN_SECONDS", 900),
        # A fired repeating alert re-arms only after the price moves this percent of
        # the threshold back out of the trigger zone (0.25% of $100k = $250).
        "REARM_HYSTERESIS_PERCENT": _env_float(env, "REARM_HYSTERESIS_PERCENT", 0.25),
        # Users must confirm their phone number with a one-time SMS code before
        # alerts can call it. Unset means "on whenever ALLOW_REGISTRATION is on":
        # strangers are the risk, the owner's own number is not.
        "REQUIRE_PHONE_VERIFICATION": (
            _env_bool(env, "REQUIRE_PHONE_VERIFICATION", False)
            if (env.get("REQUIRE_PHONE_VERIFICATION") or "").strip()
            else None
        ),
        # Twilio Verify service that sends and checks the codes (Console > Verify >
        # Services). Not needed with NOTIFY_DRY_RUN, where codes are logged instead.
        "TWILIO_VERIFY_SERVICE_SID": env.get("TWILIO_VERIFY_SERVICE_SID"),
        # Codes a user may request per rolling hour, and wrong guesses per code.
        "VERIFY_MAX_SENDS_PER_HOUR": _env_int(env, "VERIFY_MAX_SENDS_PER_HOUR", 3),
        "VERIFY_MAX_ATTEMPTS": _env_int(env, "VERIFY_MAX_ATTEMPTS", 5),
        # Lifetime of a code (Twilio Verify's default is also 10 minutes).
        "VERIFY_CODE_TTL_SECONDS": _env_int(env, "VERIFY_CODE_TTL_SECONDS", 600),
        # Optional allowlist of E.164 country calling codes, e.g. "1,44,40".
        # Empty means "any country", which is the widest toll-fraud surface.
        "ALLOWED_PHONE_COUNTRY_CODES": _env_list(env, "ALLOWED_PHONE_COUNTRY_CODES"),

        # Sanity bounds for a BTC price threshold (rejects 0, negatives, inf and nan).
        "MIN_PRICE_THRESHOLD": 0.01,
        "MAX_PRICE_THRESHOLD": 10_000_000.0,

        # --- Price feed --------------------------------------------------------------
        # The Binance listener must run in exactly one process (see README).
        "RUN_PRICE_FEED": _env_bool(env, "RUN_PRICE_FEED", True),
        # A localhost port used purely as a cross-process mutex for the feed. Keep it
        # below the ephemeral range (32768+ on Linux, 49152+ on macOS and Windows):
        # a client socket that happens to get the port would block the lock.
        "PRICE_FEED_LOCK_PORT": _env_int(env, "PRICE_FEED_LOCK_PORT", 29653),
        # stream.binance.com answers HTTP 451 to US IP addresses; from the US use
        # wss://stream.binance.us:9443/ws/btcusdt@trade instead.
        "BINANCE_WS_URL": env.get(
            "BINANCE_WS_URL", "wss://stream.binance.com:9443/ws/btcusdt@trade"
        ),
        # A socket that delivers no trade for this long is treated as dead and torn
        # down; a half-open TCP connection would otherwise stop all alerts silently.
        "FEED_STALE_SECONDS": _env_int(env, "FEED_STALE_SECONDS", 60),
        # Reconnect backoff: doubles from the base up to the cap, with jitter.
        "FEED_RECONNECT_BASE_SECONDS": _env_int(env, "FEED_RECONNECT_BASE_SECONDS", 1),
        "FEED_RECONNECT_MAX_SECONDS": _env_int(env, "FEED_RECONNECT_MAX_SECONDS", 60),
        # Alerts are re-read from the database at least this often even when the
        # price stays inside the quiet band (catches changes made by another process).
        "ALERT_FULL_SCAN_SECONDS": _env_float(env, "ALERT_FULL_SCAN_SECONDS", 5.0),

        # Comma separated list of origins allowed to open a Socket.IO connection.
        "CORS_ALLOWED_ORIGINS": _env_list(
            env, "CORS_ALLOWED_ORIGINS", "http://localhost:5000,http://127.0.0.1:5000"
        ),

        # --- Rate limits (Flask-Limiter) ---------------------------------------------
        # Set explicitly: Flask-Limiter otherwise inherits the previous app's value.
        "RATELIMIT_ENABLED": _env_bool(env, "RATELIMIT_ENABLED", True),
        "LOGIN_RATE_LIMIT": env.get("LOGIN_RATE_LIMIT", "10 per minute; 60 per hour"),
        "REGISTER_RATE_LIMIT": env.get("REGISTER_RATE_LIMIT", "5 per hour"),
        # Per IP, on top of the per-user limits above.
        "VERIFY_SEND_RATE_LIMIT": env.get("VERIFY_SEND_RATE_LIMIT", "5 per hour"),
        "VERIFY_CHECK_RATE_LIMIT": env.get("VERIFY_CHECK_RATE_LIMIT", "10 per minute"),
    }
