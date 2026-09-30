from datetime import datetime, timedelta, timezone

from sqlalchemy import inspect, text

from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin

db = SQLAlchemy()


def utcnow():
    """Naive UTC timestamp (datetime.utcnow() is deprecated in Python 3.12)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    phone_number = db.Column(db.String(20), nullable=False)
    # 255 rather than 128: Werkzeug 3 defaults to scrypt, whose hashes are ~162
    # characters, so a 128 column raises a DataError on registration after upgrade.
    password_hash = db.Column(db.String(255), nullable=False)
    # Timestamp of the last outbound call/SMS, used for the per-user cooldown.
    last_notified_at = db.Column(db.DateTime, nullable=True)
    alerts = db.relationship('Alert', backref='user', lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def notification_cooldown_active(self, cooldown_seconds, now=None):
        """True when this user was notified too recently to notify again."""
        if not self.last_notified_at or cooldown_seconds <= 0:
            return False
        now = now or utcnow()
        return now - self.last_notified_at < timedelta(seconds=cooldown_seconds)


class Alert(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    price_threshold = db.Column(db.Float, nullable=False)
    # "above" triggers when BTC is equal or above threshold,
    # "below" triggers when BTC is equal or below threshold.
    alert_type = db.Column(db.String(10), nullable=False, default="above")
    triggered = db.Column(db.Boolean, default=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

    # Repeating alerts stay active after firing: `triggered` remains False and the
    # alert re-arms once the price leaves the trigger zone and the cooldown expires.
    repeat = db.Column(db.Boolean, nullable=False, default=False)
    armed = db.Column(db.Boolean, nullable=False, default=True)
    last_triggered_at = db.Column(db.DateTime, nullable=True)

    # "call", "sms" or "both".
    notify_channel = db.Column(db.String(10), nullable=False, default="call")
    # Last delivery error, surfaced in the UI instead of being swallowed.
    notify_error = db.Column(db.String(255), nullable=True)

    # Set when the alert was created as a percent move from the price at creation.
    percent_change = db.Column(db.Float, nullable=True)
    base_price = db.Column(db.Float, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    def condition_met(self, price):
        """True when the current price satisfies this alert's threshold."""
        if self.alert_type == "above":
            return price >= self.price_threshold
        return price <= self.price_threshold

    def rearm_ready(self, price, hysteresis_percent=0.0):
        """True when a fired repeating alert may re-arm at `price`.

        The price has to leave the trigger zone by a band of `hysteresis_percent`
        of the threshold. Without the band, BTC chopping a few dollars around the
        threshold re-arms the alert on every dip and it calls again as soon as the
        cooldown expires.
        """
        band = self.price_threshold * max(hysteresis_percent or 0.0, 0.0) / 100.0
        if self.alert_type == "above":
            return price < self.price_threshold - band
        return price > self.price_threshold + band

    def is_due(self, price, cooldown_seconds=0, now=None):
        """True when this alert should fire a notification for `price` right now."""
        if self.triggered:
            return False
        if not self.condition_met(price):
            return False
        if not self.repeat:
            return True
        # Repeating alert: needs to have re-armed and to be out of cooldown.
        if not self.armed:
            return False
        if self.last_triggered_at and cooldown_seconds > 0:
            now = now or utcnow()
            if now - self.last_triggered_at < timedelta(seconds=cooldown_seconds):
                return False
        return True

    def describe(self):
        """Human readable summary used in the call/SMS body."""
        direction = "rose above" if self.alert_type == "above" else "fell below"
        return f"Bitcoin {direction} {self.price_threshold:,.2f} US dollars"

    def __repr__(self):
        # Note: We use alert.user.phone_number when needed.
        return f"<Alert User:{self.user_id} {self.alert_type} {self.price_threshold}>"


class NotificationLog(db.Model):
    """One outbound notification: what was sent, to whom, and how it ended.

    Doubles as the ledger for the global daily notification budget.
    """
    id = db.Column(db.Integer, primary_key=True)
    # Plain integers, not foreign keys: the log outlives deleted alerts.
    alert_id = db.Column(db.Integer, nullable=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    channel = db.Column(db.String(10), nullable=False)
    # "queued", "sent", "failed" or "dry_run".
    status = db.Column(db.String(10), nullable=False, default="queued")
    message = db.Column(db.String(255), nullable=True)
    detail = db.Column(db.String(255), nullable=True)
    # Channels already delivered ("call", "sms" or "call,sms"). A restart re-sends
    # a still-"queued" row; this stops it ringing the phone a second time when
    # the call went out and only the SMS was still being retried.
    delivered = db.Column(db.String(20), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)


def add_missing_columns(engine, logger=None):
    """Adds columns that newer versions of the models define to an old SQLite DB.

    create_all() creates missing tables but never alters existing ones, so a
    database from before repeat/SMS/percent alerts crashed on the first query.
    Additive only: nothing is dropped or rewritten. New columns are added as
    nullable with the model's scalar default for existing rows. Other databases
    are left alone; use a real migration tool there.
    Returns the list of "table.column" names added.
    """
    if engine.dialect.name != "sqlite":
        return []
    added = []
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in db.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" '
                ddl += column.type.compile(dialect=engine.dialect)
                default = column.default
                if default is not None and default.is_scalar:
                    value = default.arg
                    if isinstance(value, bool):
                        value = int(value)
                    ddl += " DEFAULT " + (
                        f"'{value}'" if isinstance(value, str) else str(value)
                    )
                conn.execute(text(ddl))
                added.append(f"{table.name}.{column.name}")
    if added and logger is not None:
        logger.warning(f"Upgraded the database schema, added: {', '.join(added)}")
    return added
