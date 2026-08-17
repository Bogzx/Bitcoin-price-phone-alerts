import os
import sys

# The Binance feed starts at import time in app.py; tests must never open it.
os.environ["RUN_PRICE_FEED"] = "0"
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15005550006")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
