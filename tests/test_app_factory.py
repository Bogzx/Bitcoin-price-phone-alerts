"""The package builds apps without side effects; only the entry point starts the feed."""

import importlib
import socket
import sys
import threading

import pytest

from btc_alerts import get_services, start_background_services
from btc_alerts.config import load_config
from btc_alerts.feed import ephemeral_port_range


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def no_threads(monkeypatch):
    """Replaces the background loops with no-ops so nothing touches the network."""

    def patch(services):
        monkeypatch.setattr(services.feed, "run", lambda: None)
        monkeypatch.setattr(services.feed, "run_watchdog", lambda: None)
        monkeypatch.setattr(services.notifier, "run_worker", lambda: None)

    return patch


def test_create_app_starts_no_threads_and_no_feed(make_app):
    before = threading.active_count()
    app = make_app(RUN_PRICE_FEED=True)
    assert threading.active_count() == before
    assert get_services(app).feed.status.running is False


def test_apps_are_independent(make_app):
    first, second = make_app(), make_app(MAX_ACTIVE_ALERTS_PER_USER=9)
    assert get_services(first) is not get_services(second)
    assert first.config["MAX_ACTIVE_ALERTS_PER_USER"] == 5
    assert second.config["MAX_ACTIVE_ALERTS_PER_USER"] == 9


def test_missing_secret_key_gets_a_random_one_with_a_warning(make_app, caplog):
    app = make_app(SECRET_KEY=None)
    assert len(app.config["SECRET_KEY"]) == 32
    assert "SECRET_KEY is not set" in caplog.text


def test_run_price_feed_false_keeps_the_feed_off(make_app):
    app = make_app(RUN_PRICE_FEED=False)
    assert start_background_services(app) is False
    assert get_services(app).feed.status.running is False


def test_feed_starts_once_and_holds_the_lock(make_app, no_threads):
    port = free_port()
    owner = make_app(RUN_PRICE_FEED=True, PRICE_FEED_LOCK_PORT=port)
    no_threads(get_services(owner))
    try:
        assert start_background_services(owner) is True
        assert get_services(owner).feed.status.running is True
        assert start_background_services(owner) is True  # idempotent

        # A second worker in the same host finds the port taken and stays off.
        second = make_app(RUN_PRICE_FEED=True, PRICE_FEED_LOCK_PORT=port)
        no_threads(get_services(second))
        assert start_background_services(second) is False
        assert get_services(second).feed.status.running is False
    finally:
        get_services(owner).feed_lock.close()


def test_default_lock_port_is_below_every_ephemeral_range():
    # Linux hands out 32768+ as client source ports, macOS and Windows 49152+.
    assert load_config({})["PRICE_FEED_LOCK_PORT"] < 32768


def test_unrelated_socket_on_the_lock_port_is_an_error_not_a_second_owner(
    make_app, no_threads, caplog
):
    """A client connection that got the lock port as its ephemeral port blocks the
    bind. That is not another feed: it must be reported as an error that stops
    alerts, not as the routine "another worker owns the feed"."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    client = socket.create_connection(server.getsockname())
    port = client.getsockname()[1]  # an ephemeral port, held but not listening
    try:
        app = make_app(RUN_PRICE_FEED=True, PRICE_FEED_LOCK_PORT=port)
        no_threads(get_services(app))
        assert start_background_services(app) is False
    finally:
        client.close()
        server.close()
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors and "not a feed lock" in errors[0].getMessage()
    if ephemeral_port_range():
        assert "ephemeral port range" in caplog.text


def test_entry_point_builds_the_app(monkeypatch, tmp_path):
    """`gunicorn app:app` imports app.py; with the feed disabled it must still build."""
    monkeypatch.setenv("RUN_PRICE_FEED", "false")
    monkeypatch.setenv("SECRET_KEY", "entry-point-test")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'entry.db'}")
    monkeypatch.chdir(tmp_path)  # keep load_dotenv() away from a developer's .env
    sys.modules.pop("app", None)
    try:
        entry = importlib.import_module("app")
        assert entry.app.config["SECRET_KEY"] == "entry-point-test"
        assert get_services(entry.app).feed.status.running is False
        assert entry.app.instance_path.endswith("instance")
    finally:
        sys.modules.pop("app", None)
