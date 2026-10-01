import pytest

from btc_alerts import create_app, get_services
from btc_alerts.models import db

# create_app() never starts the Binance feed; only app.py does. These settings
# keep the tests off the network, off Twilio and independent of the local .env.
TEST_CONFIG = {
    "TESTING": True,
    "SECRET_KEY": "test-secret-key",
    "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
    "TWILIO_ACCOUNT_SID": "ACtest",
    "TWILIO_AUTH_TOKEN": "test-token",
    "TWILIO_PHONE_NUMBER": "+15005550006",
    "NOTIFY_DRY_RUN": False,
    "RUN_PRICE_FEED": False,
    "WTF_CSRF_ENABLED": False,
    "RATELIMIT_ENABLED": False,
    "SESSION_COOKIE_SECURE": False,
    "REMEMBER_COOKIE_SECURE": False,
    "ALLOW_REGISTRATION": False,
    "LOG_LEVEL": "INFO",
}


@pytest.fixture
def make_app():
    """Builds an app from TEST_CONFIG plus overrides (for non-default setups)."""
    return lambda **overrides: create_app({**TEST_CONFIG, **overrides})


@pytest.fixture
def app(make_app):
    """A fresh app with an empty in-memory database, inside an app context."""
    app = make_app()
    with app.app_context():
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def services(app):
    return get_services(app)
