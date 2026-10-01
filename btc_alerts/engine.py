"""Evaluates alerts against each trade price.

BTCUSDT trades several times a second, and loading every alert from SQLite on
every trade cost 1 ms per tick with 5 alerts and 31 ms with 1,000 (enough to
fall behind the stream during a fast move, which is exactly when alerts
matter). After each full evaluation the engine therefore remembers the "quiet
band": the price range in which no alert can fire or re-arm. Ticks inside it
skip the database entirely. The band is dropped whenever an alert or user row
changes in this process, and alerts are re-read at least every
ALERT_FULL_SCAN_SECONDS anyway, which covers changes made by another process.
"""

import itertools
import math
import threading
import time

from sqlalchemy import event
from sqlalchemy.orm import Session

from .extensions import socketio
from .models import Alert, NotificationLog, User, db, utcnow
from .notifications import notification_cost, notifications_in_last_day

# Bumped after every flush that touches an Alert or a User, from any thread.
# A quiet band computed before the latest bump is stale.
_version = 0
_version_lock = threading.Lock()


def data_version():
    return _version


@event.listens_for(Session, "after_flush")
def _invalidate_on_alert_changes(session, flush_context):
    global _version
    changed = itertools.chain(session.new, session.dirty, session.deleted)
    if any(isinstance(obj, (Alert, User)) for obj in changed):
        with _version_lock:
            _version += 1


class _DailyBudget:
    """The deployment-wide MAX_NOTIFICATIONS_PER_DAY, counted once per tick."""

    def __init__(self, limit, now):
        self.limit = limit
        self.now = now
        self.used = None  # counted lazily, only when something is due

    def try_spend(self, cost):
        if self.limit <= 0:
            return True
        if self.used is None:
            self.used = notifications_in_last_day(self.now)
        if self.used + cost > self.limit:
            return False
        self.used += cost
        return True


class QuietBand:
    """The open price range in which no alert can fire and none can re-arm."""

    def __init__(self, version, hysteresis, computed_at):
        self.version = version
        self.hysteresis = hysteresis
        self.computed_at = computed_at
        # Armed alerts fire at price >= fire_ceiling ("above") or
        # price <= fire_floor ("below").
        self.fire_floor = -math.inf
        self.fire_ceiling = math.inf
        # Disarmed repeating alerts re-arm at price < rearm_floor ("above") or
        # price > rearm_ceiling ("below").
        self.rearm_floor = -math.inf
        self.rearm_ceiling = math.inf

    def add(self, alert, hysteresis):
        threshold = alert.price_threshold
        if alert.repeat and not alert.armed:
            band = threshold * max(hysteresis or 0.0, 0.0) / 100.0
            if alert.alert_type == "above":
                self.rearm_floor = max(self.rearm_floor, threshold - band)
            else:
                self.rearm_ceiling = min(self.rearm_ceiling, threshold + band)
        elif alert.alert_type == "above":
            self.fire_ceiling = min(self.fire_ceiling, threshold)
        else:
            self.fire_floor = max(self.fire_floor, threshold)

    def contains(self, price):
        return (
            self.fire_floor < price < self.fire_ceiling
            and self.rearm_floor <= price <= self.rearm_ceiling
        )


class AlertEngine:
    """Turns price ticks into committed alert state and queued notifications."""

    def __init__(self, app, notifier):
        self.app = app
        self.notifier = notifier
        self.band = None
        self._budget_warned_at = None

    def is_quiet(self, price, now=None):
        """True when `price` cannot change any alert, so the DB can be skipped."""
        band = self.band
        if band is None or band.version != data_version():
            return False
        if band.hysteresis != self.app.config["REARM_HYSTERESIS_PERCENT"]:
            return False
        now = time.monotonic() if now is None else now
        if now - band.computed_at >= self.app.config["ALERT_FULL_SCAN_SECONDS"]:
            return False
        return band.contains(price)

    def process_tick(self, price):
        """Evaluates every active alert against `price`.

        Alert state is committed *before* the notification is dispatched, so a
        Twilio failure can never silently consume an alert.
        Must be called inside an application context. Returns the queued jobs.
        """
        if self.is_quiet(price):
            return []
        # Read before the query: a change committed by another thread after this
        # point bumps the version and invalidates the band built below.
        version = data_version()
        started = time.monotonic()

        config = self.app.config
        now = utcnow()
        repeat_cooldown = config["REPEAT_ALERT_COOLDOWN_SECONDS"]
        hysteresis = config["REARM_HYSTERESIS_PERCENT"]
        budget = _DailyBudget(config["MAX_NOTIFICATIONS_PER_DAY"], now)

        band = QuietBand(version, hysteresis, started)
        pending = []
        changed = False
        for alert in Alert.query.filter_by(triggered=False).all():
            due = self._evaluate(alert, price, now, hysteresis, repeat_cooldown)
            if due is None:
                changed = True  # re-armed
            elif due and self._may_notify(alert, now, budget):
                pending.append(self._fire(alert, price, now))
                changed = True
            if not alert.triggered:
                band.add(alert, hysteresis)

        # Avoid a write transaction on every trade tick when nothing changed.
        if changed:
            db.session.commit()
        self.band = band

        for job in pending:
            job["log_id"] = job.pop("log").id
            socketio.emit(
                "alert_triggered",
                {
                    "alert_id": job["alert_id"],
                    "price_threshold": job["price_threshold"],
                    "alert_type": job["alert_type"],
                    "price": price,
                    "repeat": job["repeat"],
                },
                to=f"user_{job['user_id']}",
            )
            self.notifier.queue.put(job)
        return pending

    @staticmethod
    def _evaluate(alert, price, now, hysteresis, repeat_cooldown):
        """True when due, None when it just re-armed, False otherwise."""
        # A repeating alert re-arms once the price leaves its trigger zone by
        # the hysteresis band.
        if alert.repeat and not alert.armed and alert.rearm_ready(price, hysteresis):
            alert.armed = True
            return None
        return alert.is_due(price, repeat_cooldown, now)

    def _may_notify(self, alert, now, budget):
        """Applies the per-user cooldown and the global daily budget."""
        user = alert.user
        cooldown = self.app.config["NOTIFY_COOLDOWN_SECONDS"]
        if user.notification_cooldown_active(cooldown, now):
            self.app.logger.info(
                f"Alert {alert.id} is due but user {user.id} is within the "
                f"{cooldown}s notification cooldown; skipping."
            )
            return False
        if not budget.try_spend(notification_cost(alert.notify_channel)):
            self._warn_budget_exhausted(budget.limit)
            return False
        return True

    def _fire(self, alert, price, now):
        """Marks `alert` fired and returns its notification job (not yet queued)."""
        user = alert.user
        self.app.logger.info(
            f"Triggering alert {alert.id} for user {user.username}: "
            f"BTC {alert.alert_type} {alert.price_threshold}"
        )
        alert.last_triggered_at = now
        alert.notify_error = None
        if alert.repeat:
            alert.armed = False
        else:
            alert.triggered = True
        # Setting this here also caps a user to one notification per tick.
        user.last_notified_at = now
        message = f"{alert.describe()}. The current price is {price:,.2f}."
        log_entry = NotificationLog(
            alert_id=alert.id,
            user_id=user.id,
            channel=alert.notify_channel,
            status="queued",
            message=message[:255],
            created_at=now,
        )
        db.session.add(log_entry)
        return {
            "log": log_entry,
            "alert_id": alert.id,
            "user_id": user.id,
            "phone_number": user.phone_number,
            "channel": alert.notify_channel,
            "message": message,
            "repeat": alert.repeat,
            "price_threshold": alert.price_threshold,
            "alert_type": alert.alert_type,
        }

    def _warn_budget_exhausted(self, budget):
        """Logs the exhausted global budget at most once every 10 minutes."""
        now = time.monotonic()
        if self._budget_warned_at is not None and now - self._budget_warned_at < 600:
            return
        self._budget_warned_at = now
        self.app.logger.warning(
            f"Global notification budget of {budget} per 24h is used up; due alerts "
            "are held until it frees up. Raise MAX_NOTIFICATIONS_PER_DAY if expected."
        )
