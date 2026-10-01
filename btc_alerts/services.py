"""The per-app runtime services and the background threads that drive them."""

import threading
from dataclasses import dataclass
from typing import Any, Optional

from flask import current_app

from .engine import AlertEngine
from .extensions import socketio
from .feed import FeedLockUnavailable, PriceFeed, acquire_feed_lock, ephemeral_port_range
from .notifications import Notifier
from .verification import PhoneVerifier


@dataclass
class Services:
    notifier: Notifier
    engine: AlertEngine
    feed: PriceFeed
    verifier: PhoneVerifier
    feed_lock: Optional[Any] = None


def build_services(app):
    notifier = Notifier(app)
    engine = AlertEngine(app, notifier)
    feed = PriceFeed(app, on_price=engine.process_tick)
    verifier = PhoneVerifier(app, notifier)
    return Services(notifier=notifier, engine=engine, feed=feed, verifier=verifier)


def get_services(app=None):
    return (app or current_app).extensions["btc_alerts"]


def start_background_services(app):
    """Starts the price feed, its watchdog and the notification worker.

    Called by the entry point (app.py), never on import, and at most once per
    process: the feed holds a localhost port lock, so a second gunicorn worker
    skips the feed instead of placing duplicate calls.
    Returns True when this process now owns the feed.
    """
    services = get_services(app)
    if services.feed.status.running:
        return True
    if not app.config["RUN_PRICE_FEED"]:
        app.logger.warning("RUN_PRICE_FEED is disabled: this process will not evaluate alerts.")
        return False
    port = app.config["PRICE_FEED_LOCK_PORT"]
    ephemeral = ephemeral_port_range()
    if ephemeral and ephemeral[0] <= port <= ephemeral[1]:
        app.logger.warning(
            f"PRICE_FEED_LOCK_PORT {port} is inside this host's ephemeral port range "
            f"({ephemeral[0]}-{ephemeral[1]}): an outgoing connection can occupy it and "
            "stop the feed from starting. Pick a port below the range."
        )
    try:
        lock = acquire_feed_lock(port)
    except FeedLockUnavailable as exc:
        (app.logger.warning if exc.held_by_feed else app.logger.error)(str(exc))
        return False
    services.feed_lock = lock
    services.notifier.requeue_pending()
    threading.Thread(target=services.notifier.run_worker, daemon=True).start()
    threading.Thread(target=services.feed.run_watchdog, daemon=True).start()
    socketio.start_background_task(target=services.feed.run)
    services.feed.status.running = True
    app.logger.info(f"Binance price feed started ({app.config['BINANCE_WS_URL']}).")
    return True
