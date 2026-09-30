"""Delivery against a fake Twilio client: what actually goes over the wire,
dry-run mode, and that phone numbers stay out of the logs.
"""

import json
from types import SimpleNamespace

import pytest

import app as app_module
from app import app as flask_app
from models import Alert, User, db


class FakeTwilioClient:
    """Records calls.create / messages.create kwargs like twilio.rest.Client."""

    def __init__(self, fail_times=0):
        self.created = []
        self.fail_times = fail_times
        self.calls = SimpleNamespace(create=self._make("call", "CA"))
        self.messages = SimpleNamespace(create=self._make("sms", "SM"))

    def _make(self, kind, prefix):
        def create(**kwargs):
            if self.fail_times:
                self.fail_times -= 1
                raise RuntimeError("HTTP 503 from Twilio")
            self.created.append((kind, kwargs))
            return SimpleNamespace(sid=f"{prefix}{len(self.created):032d}")

        return create


@pytest.fixture
def twilio(monkeypatch):
    fake = FakeTwilioClient()
    monkeypatch.setattr(app_module, "twilio_client", fake)
    monkeypatch.setattr(app_module, "twilio_phone_number", "+15005550006")
    flask_app.config.update(
        TESTING=True,
        NOTIFY_DRY_RUN=False,
        NOTIFY_COOLDOWN_SECONDS=0,
        REPEAT_ALERT_COOLDOWN_SECONDS=0,
    )
    with flask_app.app_context():
        db.drop_all()
        db.create_all()
        yield fake
        db.session.remove()
        db.drop_all()
    flask_app.config["NOTIFY_DRY_RUN"] = False


def seed(channel="call", threshold=70000.0):
    user = User(username="dora", email="dora@example.com", phone_number="+14155550123")
    user.set_password("pw")
    db.session.add(user)
    db.session.commit()
    alert = Alert(
        price_threshold=threshold,
        alert_type="above",
        user_id=user.id,
        notify_channel=channel,
    )
    db.session.add(alert)
    db.session.commit()
    return alert


def tick(price):
    app_module.on_message(None, json.dumps({"e": "trade", "p": str(price)}))
    return app_module.drain_notification_queue(retry_delay=0)


def test_call_goes_to_twilio_with_escaped_twiml(twilio):
    seed("call")
    tick(70100.5)

    assert len(twilio.created) == 1
    kind, kwargs = twilio.created[0]
    assert kind == "call"
    assert kwargs["to"] == "+14155550123"
    assert kwargs["from_"] == "+15005550006"
    twiml = kwargs["twiml"]
    assert twiml.startswith("<Response><Say")
    assert "70,100.50" in twiml
    assert twiml.count("<Say") == 2  # message is read twice


def test_twiml_escapes_markup():
    twilio = FakeTwilioClient()
    app_module.twilio_client, saved = twilio, app_module.twilio_client
    saved_number = app_module.twilio_phone_number
    app_module.twilio_phone_number = "+15005550006"
    try:
        app_module.call_user("+14155550123", "<Hangup/> & more")
    finally:
        app_module.twilio_client = saved
        app_module.twilio_phone_number = saved_number
    twiml = twilio.created[0][1]["twiml"]
    assert "<Hangup/>" not in twiml
    assert "&lt;Hangup/&gt; &amp; more" in twiml


def test_both_channel_places_call_and_sms(twilio):
    seed("both")
    tick(70100.0)
    assert [kind for kind, _ in twilio.created] == ["call", "sms"]
    assert "70,100.00" in twilio.created[1][1]["body"]


def test_transient_twilio_failure_is_retried(twilio):
    twilio.fail_times = 2
    alert = seed("call")
    tick(70100.0)
    assert len(twilio.created) == 1
    assert db.session.get(Alert, alert.id).notify_error is None


def test_persistent_failure_is_recorded(twilio):
    twilio.fail_times = 99
    alert = seed("call")
    tick(70100.0)
    assert twilio.created == []
    assert "503" in db.session.get(Alert, alert.id).notify_error


def test_missing_from_number_is_a_clear_error(twilio, monkeypatch):
    monkeypatch.setattr(app_module, "twilio_phone_number", None)
    alert = seed("call")
    tick(70100.0)
    assert "TWILIO_PHONE_NUMBER" in db.session.get(Alert, alert.id).notify_error


def test_dry_run_never_touches_twilio(twilio, caplog):
    flask_app.config["NOTIFY_DRY_RUN"] = True
    caplog.set_level("INFO", logger=flask_app.logger.name)
    alert = seed("both")
    tick(70100.0)

    assert twilio.created == []
    refreshed = db.session.get(Alert, alert.id)
    assert refreshed.triggered is True
    assert refreshed.notify_error is None
    assert "[dry-run] Would call" in caplog.text
    assert "[dry-run] Would text" in caplog.text


def test_dry_run_works_without_any_twilio_client(twilio, monkeypatch):
    monkeypatch.setattr(app_module, "twilio_client", None)
    flask_app.config["NOTIFY_DRY_RUN"] = True
    alert = seed("call")
    tick(70100.0)
    assert db.session.get(Alert, alert.id).notify_error is None


def test_logs_mask_the_phone_number(twilio, caplog):
    caplog.set_level("INFO", logger=flask_app.logger.name)
    seed("call")
    tick(70100.0)
    assert "+14155550123" not in caplog.text
    assert "0123" in caplog.text


@pytest.mark.parametrize(
    "raw, masked",
    [("+14155550123", "+1******0123"), ("+40712345678", "+4******5678"), ("", "***")],
)
def test_mask_phone(raw, masked):
    assert app_module.mask_phone(raw) == masked


def test_retry_after_partial_failure_does_not_repeat_the_call(twilio):
    """'both': call succeeded, SMS failed once. The retry must only send the SMS."""
    real_sms = twilio.messages.create
    failures = {"left": 1}

    def flaky_sms(**kwargs):
        if failures["left"]:
            failures["left"] -= 1
            raise RuntimeError("SMS 500")
        return real_sms(**kwargs)

    twilio.messages.create = flaky_sms
    seed("both")
    tick(70100.0)
    assert [kind for kind, _ in twilio.created] == ["call", "sms"]


def test_notification_log_records_each_outcome(twilio):
    from models import NotificationLog

    seed("call")
    tick(70100.0)
    entry = NotificationLog.query.one()
    assert entry.status == "sent"
    assert entry.channel == "call"
    assert "70,100.00" in entry.message


def test_notification_log_records_failures(twilio):
    from models import NotificationLog

    twilio.fail_times = 99
    seed("call")
    tick(70100.0)
    entry = NotificationLog.query.one()
    assert entry.status == "failed"
    assert "503" in entry.detail


def test_global_daily_budget_caps_notifications_across_users(twilio, caplog):
    from datetime import timedelta

    from models import NotificationLog

    flask_app.config["MAX_NOTIFICATIONS_PER_DAY"] = 2
    try:
        for i in range(3):
            user = User(username=f"u{i}", email=f"u{i}@x.test", phone_number=f"+1415555010{i}")
            user.set_password("pw")
            db.session.add(user)
            db.session.flush()
            db.session.add(Alert(price_threshold=70000.0, alert_type="above", user_id=user.id))
        db.session.commit()

        tick(70100.0)
        assert len(twilio.created) == 2
        assert "budget" in caplog.text
        assert Alert.query.filter_by(triggered=False).count() == 1  # held, not lost

        # A day later the budget frees up and the held alert fires.
        for entry in NotificationLog.query.all():
            entry.created_at -= timedelta(days=1, seconds=1)
        db.session.commit()
        tick(70100.0)
        assert len(twilio.created) == 3
    finally:
        flask_app.config["MAX_NOTIFICATIONS_PER_DAY"] = 50


def test_dry_run_is_logged_as_dry_run(twilio):
    from models import NotificationLog

    flask_app.config["NOTIFY_DRY_RUN"] = True
    seed("call")
    tick(70100.0)
    assert NotificationLog.query.one().status == "dry_run"


def test_restart_requeues_recent_and_expires_old_notifications(twilio):
    from datetime import timedelta

    from models import NotificationLog, utcnow

    alert = seed("call")
    now = utcnow()
    db.session.add_all(
        [
            NotificationLog(alert_id=alert.id, user_id=alert.user_id, channel="call",
                            status="queued", message="recent", created_at=now),
            NotificationLog(alert_id=alert.id, user_id=alert.user_id, channel="call",
                            status="queued", message="stale",
                            created_at=now - timedelta(hours=1)),
            NotificationLog(alert_id=alert.id, user_id=alert.user_id, channel="call",
                            status="sent", message="done", created_at=now),
        ]
    )
    db.session.commit()

    assert app_module.requeue_pending_notifications(now=now) == (1, 1)
    app_module.drain_notification_queue(retry_delay=0)

    assert len(twilio.created) == 1
    assert "recent" in twilio.created[0][1]["twiml"]
    statuses = {e.message: e.status for e in NotificationLog.query.all()}
    assert statuses == {"recent": "sent", "stale": "failed", "done": "sent"}
    assert "restarted" in db.session.get(Alert, alert.id).notify_error
