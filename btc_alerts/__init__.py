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
from .models import add_missing_columns, db
from .services import build_services, get_services, start_background_services

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

    # Create missing tables and add columns an older database lacks.
    with app.app_context():
        db.create_all()
        add_missing_columns(db.engine, app.logger)

    app.extensions["btc_alerts"] = build_services(app)
    return app
