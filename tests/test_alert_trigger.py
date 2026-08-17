"""The behaviour the product exists for: a tick that crosses a threshold places
exactly one phone call and flips the alert to triggered.

These tests also pin the failure ordering: a Twilio error must not be able to
consume an alert silently.
"""

import json

import pytest

import app as app_module
from app import app as flask_app
from models import db, Alert, User


@pytest.fixture
def ctx():
    flask_app.config.update(
        TESTING=True,
        WTF_CSRF_ENABLED=False,
        NOTIFY_COOLDOWN_SECONDS=300,
        REPEAT_ALERT_COOLDOWN_SECONDS=0,
    )
    with flask_app.app_context():
        db.drop_all()
        db.create_all()
        yield
        db.session.remove()
        db.drop_all()


@pytest.fixture
def calls(monkeypatch):
    """Mocks Twilio and records every call/SMS placed."""
    placed = {"calls": [], "sms": []}

    def fake_call(phone_number, message):
        placed["calls"].append((phone_number, message))
        return "CA-test"

    def fake_sms(phone_number, message):
        placed["sms"].append((phone_number, message))
        return "SM-test"

    monkeypatch.setattr(app_module, "call_user", fake_call)
    monkeypatch.setattr(app_module, "sms_user", fake_sms)
    return placed


def seed_alert(threshold=70000.0, alert_type="above", **kwargs):
    user = User(username="alice", email="alice@example.com", phone_number="+14155550123")
    user.set_password("hunter2")
    db.session.add(user)
    db.session.commit()
    alert = Alert(
        price_threshold=threshold,
        alert_type=alert_type,
        user_id=user.id,
        **kwargs,
    )
    db.session.add(alert)
    db.session.commit()
    return user, alert


def tick(price):
    """Feeds on_message a synthetic Binance trade message."""
    app_module.on_message(None, json.dumps({"p": str(price), "e": "trade"}))
    app_module.drain_notification_queue(retry_delay=0)


def test_crossing_tick_places_exactly_one_call_and_flips_triggered(ctx, calls):
    user, alert = seed_alert(threshold=70000.0, alert_type="above")

    tick(70100.0)

    assert len(calls["calls"]) == 1, calls
    assert calls["sms"] == []
    phone, message = calls["calls"][0]
    assert phone == "+14155550123"
    assert "70,000" in message and "70,100" in message

    refreshed = db.session.get(Alert, alert.id)
    assert refreshed.triggered is True
    assert refreshed.notify_error is None
    assert refreshed.last_triggered_at is not None


def test_alert_fires_only_once_across_repeated_ticks(ctx, calls):
    seed_alert(threshold=70000.0, alert_type="above")

    tick(70100.0)
    tick(70200.0)
    tick(70300.0)

    assert len(calls["calls"]) == 1, calls


def test_tick_below_threshold_does_not_call(ctx, calls):
    user, alert = seed_alert(threshold=70000.0, alert_type="above")

    tick(69999.99)

    assert calls["calls"] == []
    assert db.session.get(Alert, alert.id).triggered is False


def test_below_alert_triggers_on_drop(ctx, calls):
    seed_alert(threshold=60000.0, alert_type="below")

    tick(59999.0)

    assert len(calls["calls"]) == 1


def test_twilio_failure_does_not_silently_consume_the_alert(ctx, calls, monkeypatch):
    """State is committed before dispatch, and the failure is recorded, not swallowed."""
    def boom(phone_number, message):
        raise RuntimeError("Twilio auth error 20003")

    monkeypatch.setattr(app_module, "call_user", boom)
    user, alert = seed_alert(threshold=70000.0, alert_type="above")

    tick(70100.0)

    refreshed = db.session.get(Alert, alert.id)
    assert refreshed.triggered is True  # state committed before the call
    assert "20003" in refreshed.notify_error  # failure surfaced, not swallowed


def test_repeat_alert_rearms_only_after_price_leaves_the_zone(ctx, calls):
    seed_alert(threshold=70000.0, alert_type="above", repeat=True)

    tick(70100.0)          # fires
    tick(70200.0)          # still above: must not re-fire
    assert len(calls["calls"]) == 1

    tick(69000.0)          # back below: re-arms
    flask_app.config["NOTIFY_COOLDOWN_SECONDS"] = 0
    tick(70500.0)          # crosses again: fires again
    assert len(calls["calls"]) == 2

    alert = Alert.query.first()
    assert alert.triggered is False  # a standing monitor stays active


def test_per_user_cooldown_blocks_a_burst_of_alerts(ctx, calls):
    user, _ = seed_alert(threshold=70000.0, alert_type="above")
    for threshold in (70001.0, 70002.0, 70003.0):
        db.session.add(Alert(price_threshold=threshold, alert_type="above", user_id=user.id))
    db.session.commit()

    tick(80000.0)

    # Four alerts are due, but the per-user cooldown allows one notification.
    assert len(calls["calls"]) == 1, calls


def test_sms_channel_carries_the_price(ctx, calls):
    seed_alert(threshold=70000.0, alert_type="above", notify_channel="sms")

    tick(70100.0)

    assert calls["calls"] == []
    assert len(calls["sms"]) == 1
    assert "70,100" in calls["sms"][0][1]


def test_malformed_tick_is_ignored(ctx, calls):
    seed_alert(threshold=70000.0, alert_type="above")

    app_module.on_message(None, "not json")
    app_module.on_message(None, json.dumps({"p": "nan"}))
    app_module.on_message(None, json.dumps({"p": "inf"}))
    app_module.drain_notification_queue(retry_delay=0)

    assert calls["calls"] == []
