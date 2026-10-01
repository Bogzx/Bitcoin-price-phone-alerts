"""The feed is the alerting path: if it dies silently, no alert ever fires.

These tests drive the reconnect loop, backoff and staleness watchdog with a fake
WebSocketApp, so no network is touched.
"""

import json

import pytest
import websocket

from btc_alerts import feed as feed_module


class FakeWebSocketApp:
    """Stands in for websocket.WebSocketApp.

    `script` is a list of actions run inside run_forever: "open" fires on_open,
    a number is delivered as a trade price, and an Exception instance is passed
    to on_error (which ends the session, as websocket-client does).
    """

    instances = []

    def __init__(self, url, on_message, on_error, on_close, on_open, script=()):
        self.url = url
        self.on_message = on_message
        self.on_error = on_error
        self.on_close = on_close
        self.on_open = on_open
        self.script = list(script)
        self.closed = False
        self.run_kwargs = None
        FakeWebSocketApp.instances.append(self)

    def run_forever(self, **kwargs):
        self.run_kwargs = kwargs
        for action in self.script:
            if isinstance(action, Exception):
                self.on_error(self, action)
                return False
            if action == "open":
                self.on_open(self)
            else:
                self.on_message(self, json.dumps({"e": "trade", "p": str(action)}))
        self.on_close(self, 1000, "bye")
        return False

    def close(self):
        self.closed = True


def factory(*scripts):
    """Returns a ws_factory handing out one scripted session per call."""
    pending = list(scripts)

    def make(url, **callbacks):
        return FakeWebSocketApp(url, script=pending.pop(0), **callbacks)

    return make


@pytest.fixture
def feed(app, services):
    app.config.update(
        FEED_STALE_SECONDS=60,
        FEED_RECONNECT_BASE_SECONDS=1,
        FEED_RECONNECT_MAX_SECONDS=60,
    )
    FakeWebSocketApp.instances = []
    return services.feed


def test_session_prices_update_state_and_ping_is_enabled(app, feed):
    got = feed.run_session(factory(["open", 70000, 70010]))

    assert got is True
    assert feed.price == 70010.0
    assert feed.is_fresh()
    ws = FakeWebSocketApp.instances[0]
    assert ws.url == app.config["BINANCE_WS_URL"]
    # Without a ping interval websocket-client never notices a dead peer.
    assert ws.run_kwargs["ping_interval"] > 0
    assert ws.run_kwargs["ping_timeout"] > 0
    assert feed.status.connected is False  # closed at the end


def test_session_without_prices_reports_failure(feed):
    assert feed.run_session(factory(["open"])) is False


def test_reconnect_backs_off_on_failures_and_resets_after_prices(feed, monkeypatch):
    monkeypatch.setattr(feed_module.random, "random", lambda: 1.0)  # no jitter
    sleeps = []
    feed.run(
        max_sessions=5,
        sleep=sleeps.append,
        ws_factory=factory(
            [ConnectionError("refused")],
            [ConnectionError("refused")],
            [ConnectionError("refused")],
            ["open", 70000],  # recovers
            [ConnectionError("refused")],
        ),
    )
    # 2, 4, 8 while failing; back to the base after a good session; then 2.
    assert sleeps == [2, 4, 8, 1, 2]
    assert feed.status.reconnects == 5


def test_backoff_is_capped_and_jittered(app, feed):
    app.config["FEED_RECONNECT_MAX_SECONDS"] = 30
    assert feed.reconnect_delay(50, rand=lambda: 1.0) == 30
    assert feed.reconnect_delay(50, rand=lambda: 0.0) == 15
    assert feed.reconnect_delay(0, rand=lambda: 1.0) == 1


def test_crash_in_session_does_not_kill_the_loop(feed):
    def exploding(url, **callbacks):
        raise OSError("DNS failure")

    sleeps = []
    feed.run(max_sessions=3, sleep=sleeps.append, ws_factory=exploding)
    assert len(sleeps) == 3
    assert "DNS failure" in feed.status.last_error


def test_http_451_geo_block_logs_the_binance_us_hint(feed, caplog):
    blocked = websocket.WebSocketBadStatusException("Handshake status 451", 451)
    feed.run_session(factory([blocked]))
    assert "stream.binance.us" in caplog.text
    assert "451" in feed.status.last_error


def test_watchdog_closes_a_connected_but_silent_socket(feed):
    ws = FakeWebSocketApp("wss://x", None, None, None, None)
    feed._ws = ws
    feed.status.session_started = 1000.0
    feed.status.last_tick_monotonic = 1000.0

    assert feed.check_watchdog(now=1030.0) is False
    assert ws.closed is False

    assert feed.check_watchdog(now=1061.0) is True
    assert ws.closed is True
    assert "no price" in feed.status.last_error


def test_watchdog_catches_a_session_that_never_delivers(feed):
    ws = FakeWebSocketApp("wss://x", None, None, None, None)
    feed._ws = ws
    feed.status.session_started = 5000.0
    feed.status.last_tick_monotonic = None

    assert feed.check_watchdog(now=5061.0) is True


def test_watchdog_is_idle_without_a_socket(feed):
    feed._ws = None
    assert feed.check_watchdog(now=10**9) is False


def test_price_goes_stale(feed):
    feed.record_price(70000.0, now=100.0)
    assert feed.is_fresh(now=150.0)
    assert not feed.is_fresh(now=161.0)


def test_healthz_reports_stale_then_ok(app, feed):
    client = app.test_client()
    response = client.get("/healthz")
    assert response.status_code == 503
    assert response.get_json()["status"] == "stale"

    feed.record_price(70000.0)
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "ok"
    assert body["last_price_age_seconds"] < 5


def test_zero_backoff_base_cannot_spin(app, feed):
    """FEED_RECONNECT_BASE_SECONDS=0 used to give a zero delay: a tight reconnect loop."""
    app.config["FEED_RECONNECT_BASE_SECONDS"] = 0
    assert feed.reconnect_delay(0, rand=lambda: 0.0) >= 0.5
