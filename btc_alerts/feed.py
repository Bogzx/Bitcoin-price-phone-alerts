"""The Binance trade stream: one socket, a reconnect loop and a watchdog.

If the feed dies silently no alert can fire, so every way it can die is
handled here: pings for dead peers, a watchdog for half-open sockets that stop
delivering trades, exponential backoff for outages and geo-blocks, and a
freshness check that /healthz and the UI report.
"""

import json
import math
import random
import socket
import time

import websocket

from .extensions import socketio
from .models import utcnow

# Binance sends a ping every ~20s; ours detects a dead peer from our side too.
WS_PING_INTERVAL_SECONDS = 20
WS_PING_TIMEOUT_SECONDS = 10
# Every trade is evaluated, but browsers only need a couple of updates a second.
PRICE_EMIT_MIN_INTERVAL_SECONDS = 0.5


class FeedStatus:
    """What the price feed is doing. Read by the watchdog, /healthz and the UI."""

    def __init__(self):
        self.connected = False
        self.running = False  # the reconnect loop has been started in this process
        self.session_started = None  # time.monotonic() of the current session
        self.last_tick_monotonic = None
        self.last_tick_at = None  # naive UTC wall clock of the last price
        self.consecutive_failures = 0
        self.reconnects = 0
        self.last_error = None

    def age_seconds(self, now=None):
        """Seconds since the last price, or None before the first one."""
        if self.last_tick_monotonic is None:
            return None
        now = time.monotonic() if now is None else now
        return now - self.last_tick_monotonic


class PriceFeed:
    """Keeps the latest BTC price and hands every trade to `on_price`.

    `on_price(price)` runs inside an application context on the socket thread.
    """

    def __init__(self, app, on_price):
        self.app = app
        self.on_price = on_price
        self.status = FeedStatus()
        self.price = None
        self._ws = None
        self._last_emit = 0.0

    @property
    def logger(self):
        return self.app.logger

    def is_fresh(self, now=None):
        """True when the last known price is recent enough to act on."""
        age = self.status.age_seconds(now)
        return age is not None and age <= self.app.config["FEED_STALE_SECONDS"]

    def record_price(self, price, now=None):
        """Stores a new price and marks the feed alive."""
        self.price = price
        self.status.last_tick_monotonic = time.monotonic() if now is None else now
        self.status.last_tick_at = utcnow()

    # -- websocket-client callbacks ---------------------------------------------------

    def on_message(self, ws, message):
        """Handles one Binance trade message."""
        try:
            data = json.loads(message)
            price = float(data.get("p", 0))
            if not math.isfinite(price) or price <= 0:
                return
            self.record_price(price)
            self.logger.debug(f"Current BTC Price: {price}")

            # Emit the updated BTC price to connected clients, throttled.
            now = time.monotonic()
            if now - self._last_emit >= PRICE_EMIT_MIN_INTERVAL_SECONDS:
                self._last_emit = now
                socketio.emit("price_update", {"price": price})

            with self.app.app_context():
                self.on_price(price)
        except Exception as exc:
            self.logger.error(f"Error in on_message: {exc}")

    def on_error(self, ws, error):
        self.status.last_error = str(error)[:255]
        if getattr(error, "status_code", None) == 451:
            self.logger.error(
                "Binance refused the connection with HTTP 451 (unavailable for legal "
                "reasons): stream.binance.com blocks US IP addresses. Set BINANCE_WS_URL "
                "to wss://stream.binance.us:9443/ws/btcusdt@trade if you are in the US."
            )
            return
        self.logger.error(f"WebSocket error: {error}")

    def on_close(self, ws, close_status_code, close_msg):
        # No reconnect here: run() owns the reconnect loop. Reconnecting from this
        # callback would recurse, because Binance closes long-lived streams daily.
        self.status.connected = False
        self.logger.info(f"WebSocket connection closed ({close_status_code} {close_msg}).")

    def on_open(self, ws):
        self.status.connected = True
        self.logger.info("WebSocket connection established.")

    # -- Sessions and reconnects ------------------------------------------------------

    def run_session(self, ws_factory=None):
        """Runs one Binance WebSocket session (returns when the stream closes).

        Returns True when the session delivered at least one price.
        """
        ws_factory = ws_factory or websocket.WebSocketApp
        ws = ws_factory(
            self.app.config["BINANCE_WS_URL"],
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close,
            on_open=self.on_open,
        )
        started = time.monotonic()
        self.status.session_started = started
        self._ws = ws
        try:
            ws.run_forever(
                ping_interval=WS_PING_INTERVAL_SECONDS,
                ping_timeout=WS_PING_TIMEOUT_SECONDS,
            )
        finally:
            self._ws = None
            self.status.connected = False
        last = self.status.last_tick_monotonic
        return last is not None and last >= started

    def reconnect_delay(self, failures, rand=None):
        """Exponential backoff with jitter: base * 2**failures, capped, times 0.5-1."""
        rand = rand or random.random
        # At least 1s: a base of 0 would reconnect in a tight loop with no sleep.
        base = max(self.app.config["FEED_RECONNECT_BASE_SECONDS"], 1)
        cap = max(self.app.config["FEED_RECONNECT_MAX_SECONDS"], base)
        delay = min(cap, base * (2 ** min(failures, 16)))
        return delay * (0.5 + rand() / 2)

    def run(self, max_sessions=None, sleep=time.sleep, ws_factory=None):
        """Reconnect loop for the Binance feed.

        A session that delivered prices (e.g. Binance's routine 24h disconnect)
        reconnects almost immediately; repeated failures back off up to the cap so
        a geo-block or an outage is not hammered every few seconds.
        """
        status = self.status
        sessions = 0
        while max_sessions is None or sessions < max_sessions:
            sessions += 1
            got_prices = False
            try:
                got_prices = self.run_session(ws_factory)
            except Exception as exc:
                status.last_error = str(exc)[:255]
                self.logger.error(f"Binance WebSocket crashed: {exc}")
            if got_prices:
                status.consecutive_failures = 0
            else:
                status.consecutive_failures += 1
            status.reconnects += 1
            delay = self.reconnect_delay(status.consecutive_failures)
            self.logger.info(
                f"Binance stream ended ({status.consecutive_failures} consecutive "
                f"failed sessions). Reconnecting in {delay:.1f}s..."
            )
            sleep(delay)

    # -- Watchdog ---------------------------------------------------------------------

    def check_watchdog(self, now=None):
        """Tears down a connected-but-silent socket. Returns True when it did.

        websocket-client only notices a dead peer through ping timeouts, and a
        half-open connection can otherwise sit forever while alerts never fire.
        """
        ws = self._ws
        if ws is None:
            return False
        now = time.monotonic() if now is None else now
        last = max(self.status.session_started or now, self.status.last_tick_monotonic or 0)
        silent_for = now - last
        if silent_for <= self.app.config["FEED_STALE_SECONDS"]:
            return False
        self.status.last_error = f"no price for {silent_for:.0f}s; reconnecting"
        self.logger.warning(
            f"Binance feed silent for {silent_for:.0f}s; closing the socket to reconnect."
        )
        try:
            ws.close()
        except Exception as exc:
            self.logger.error(f"Could not close the stale WebSocket: {exc}")
        return True

    def run_watchdog(self, interval=5):  # pragma: no cover - background thread
        while True:
            time.sleep(interval)
            self.check_watchdog()


class FeedLockUnavailable(Exception):
    """The feed lock port could not be bound; the message says why.

    `held_by_feed` is True when another process holds the lock (expected with
    several workers), False when an unrelated socket blocks the port.
    """

    def __init__(self, message, held_by_feed):
        super().__init__(message)
        self.held_by_feed = held_by_feed


def acquire_feed_lock(port):
    """Cross-process mutex so only one worker ever runs the price feed.

    Two workers running the feed would each evaluate every alert and place
    duplicate calls. Binding a localhost port is a portable way to make the
    single-owner constraint enforceable rather than merely documented.
    Returns the bound socket (keep it open), or True when the lock is disabled
    (port <= 0). Raises FeedLockUnavailable when the port cannot be bound.
    """
    if port <= 0:
        return True
    lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock_socket.bind(("127.0.0.1", port))
        lock_socket.listen(1)
    except OSError as exc:
        lock_socket.close()
        if _is_listening(port):
            raise FeedLockUnavailable(
                f"Another process already owns the price feed lock on port {port}; "
                "this worker will not run the feed.",
                held_by_feed=True,
            ) from exc
        # Nothing listens there, so it is not another feed: some socket that is
        # not a lock holds the port, typically an outgoing connection that got it
        # as its ephemeral source port (or one still in TIME_WAIT).
        raise FeedLockUnavailable(
            f"Port {port} is held by a socket that is not a feed lock ({exc.strerror}), "
            "so this worker will not run the feed and no alert can fire. Set "
            "PRICE_FEED_LOCK_PORT to a free port outside the ephemeral range "
            "(below 32768 on Linux).",
            held_by_feed=False,
        ) from exc
    return lock_socket


def _is_listening(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def ephemeral_port_range():
    """The kernel's ephemeral port range on Linux, or None elsewhere."""
    try:
        with open("/proc/sys/net/ipv4/ip_local_port_range") as handle:
            low, high = (int(part) for part in handle.read().split())
    except (OSError, ValueError):
        return None
    return low, high
