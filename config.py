import os
import secrets

from dotenv import load_dotenv

# Load environment variables from a .env file if available
load_dotenv()


def _env_bool(name, default):
    """Reads a boolean-ish environment variable."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name, default):
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY")
    if not SECRET_KEY:
        print(
            "WARNING: SECRET_KEY is not set. Using a randomly generated key, which "
            "changes on every restart and logs out every user. Set SECRET_KEY in .env."
        )
        SECRET_KEY = secrets.token_hex(16)
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", "sqlite:///alerts.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Twilio configuration (set these in your environment or .env file)
    TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
    TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
    TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")

    # --- Cookie / session hardening -------------------------------------------------
    # Secure cookies are the default. Serving the app over plain HTTP (e.g. the
    # localhost dev setup in the README) requires SESSION_COOKIE_SECURE=false,
    # otherwise the browser refuses to store the session cookie and login "fails"
    # silently.
    SESSION_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE", True)
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE", True)
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"

    # --- Abuse / spend limits -------------------------------------------------------
    # A registrant supplies their own phone number and it is not verified, so an
    # unbounded number of alerts on an attacker-controlled premium-rate number is a
    # direct billing risk for whoever deploys this. These two limits are the cheap
    # brakes; phone verification is the real fix (see README "Status").
    MAX_ACTIVE_ALERTS_PER_USER = _env_int("MAX_ACTIVE_ALERTS_PER_USER", 5)
    # Minimum seconds between two outbound notifications for the same user.
    NOTIFY_COOLDOWN_SECONDS = _env_int("NOTIFY_COOLDOWN_SECONDS", 300)
    # Minimum seconds before a repeating alert can fire again.
    REPEAT_ALERT_COOLDOWN_SECONDS = _env_int("REPEAT_ALERT_COOLDOWN_SECONDS", 900)
    # Optional allowlist of E.164 country calling codes, e.g. "1,44,40".
    # Empty means "any country", which is the widest toll-fraud surface.
    ALLOWED_PHONE_COUNTRY_CODES = [
        code.strip()
        for code in os.getenv("ALLOWED_PHONE_COUNTRY_CODES", "").split(",")
        if code.strip()
    ]

    # Sanity bounds for a BTC price threshold (rejects 0, negatives, inf and nan).
    MIN_PRICE_THRESHOLD = 0.01
    MAX_PRICE_THRESHOLD = 10_000_000.0

    # --- Price feed -----------------------------------------------------------------
    # The Binance listener must run in exactly one process. See README "Status".
    RUN_PRICE_FEED = _env_bool("RUN_PRICE_FEED", True)
    # A localhost port used purely as a cross-process mutex for the feed.
    PRICE_FEED_LOCK_PORT = _env_int("PRICE_FEED_LOCK_PORT", 47653)

    # Comma separated list of origins allowed to open a Socket.IO connection.
    CORS_ALLOWED_ORIGINS = [
        origin.strip()
        for origin in os.getenv(
            "CORS_ALLOWED_ORIGINS", "http://localhost:5000,http://127.0.0.1:5000"
        ).split(",")
        if origin.strip()
    ]

    # Rate limits (Flask-Limiter syntax).
    LOGIN_RATE_LIMIT = os.getenv("LOGIN_RATE_LIMIT", "10 per minute; 60 per hour")
    REGISTER_RATE_LIMIT = os.getenv("REGISTER_RATE_LIMIT", "5 per hour")
