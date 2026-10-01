"""BTC price alerts: phone calls and SMS when Bitcoin crosses your thresholds.

`create_app()` builds the Flask app without side effects beyond creating
missing database tables. The Binance feed and the notification worker start
only when the entry point calls `start_background_services(app)`.
"""

import secrets

from flask import Flask

from . import events  # noqa: F401 - registers the Socket.IO handlers
from .alerts import bp as alerts_bp
from .auth import bp as auth_bp
from .config import load_config
from .extensions import csrf, limiter, login_manager, socketio
from .models import db, upgrade_database
from .services import build_services, get_services, start_background_services
from .settings import bp as settings_bp
from .verification import bp as verify_bp, verification_required

__all__ = ["create_app", "get_services", "start_background_services"]


def create_app(overrides=None):
    """Builds the app from the environment, then applies `overrides`."""
    app = Flask(__name__)
    app.config.update(load_config())
    app.config.update(overrides or {})

    # Flask's logger defaults to WARNING outside debug mode, which hid every
    # "alert triggered" / "call placed" line in production.
    try:
        app.logger.setLevel(app.config["LOG_LEVEL"])
    except ValueError:
        app.logger.setLevel("INFO")

    if not app.config.get("SECRET_KEY"):
        app.logger.warning(
            "SECRET_KEY is not set. Using a random key, which changes on every restart "
            "and logs out every user. Set SECRET_KEY in .env."
        )
        app.config["SECRET_KEY"] = secrets.token_hex(16)

    db.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)
    login_manager.init_app(app)
    socketio.init_app(
        app,
        async_mode="threading",
        cors_allowed_origins=app.config["CORS_ALLOWED_ORIGINS"],
    )

    app.register_blueprint(auth_bp)
    app.register_blueprint(alerts_bp)
    app.register_blueprint(verify_bp)
    app.register_blueprint(settings_bp)
    _warn_about_unsafe_verification_settings(app)

    # Create missing tables and bring an older database up to date.
    with app.app_context():
        db.create_all()
        upgrade_database(db.engine, app.logger)

    app.extensions["btc_alerts"] = build_services(app)
    return app


def _warn_about_unsafe_verification_settings(app):
    config = app.config
    if not verification_required(app):
        if config["ALLOW_REGISTRATION"]:
            app.logger.warning(
                "ALLOW_REGISTRATION is on but REQUIRE_PHONE_VERIFICATION is off: anyone "
                "who registers can make this deployment call any number on your Twilio "
                "balance."
            )
        return
    if not config["NOTIFY_DRY_RUN"] and not config.get("TWILIO_VERIFY_SERVICE_SID"):
        app.logger.error(
            "Phone verification is required but TWILIO_VERIFY_SERVICE_SID is not set: "
            "nobody can verify a number, so no alert will fire."
        )
