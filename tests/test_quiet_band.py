"""Ticks inside the quiet band must not touch the database, and anything that
could change the outcome must end the quiet band."""

import json

import pytest
from sqlalchemy import event, text

from btc_alerts.models import Alert, User, db


@pytest.fixture
def setup(app, services, monkeypatch):
    app.config.update(
        NOTIFY_COOLDOWN_SECONDS=0,
        REPEAT_ALERT_COOLDOWN_SECONDS=0,
        REARM_HYSTERESIS_PERCENT=0.25,
        ALERT_FULL_SCAN_SECONDS=5.0,
    )
    placed = []
    monkeypatch.setattr(
        services.notifier, "call_user", lambda phone, message: placed.append(message)
    )
    user = User(username="ivy", email="ivy@example.com", phone_number="+14155550123")
    user.set_password("pw-pw-pw-pw")
    db.session.add(user)
    db.session.commit()
    return services, user, placed


@pytest.fixture
def queries():
    """Counts SQL statements sent to the database."""
    seen = []

    def record(conn, cursor, statement, *args):
        seen.append(statement)

    event.listen(db.engine, "before_cursor_execute", record)
    yield seen
    event.remove(db.engine, "before_cursor_execute", record)


@pytest.fixture
def tick(services):
    def tick(price):
        services.feed.on_message(None, json.dumps({"e": "trade", "p": str(price)}))
        services.notifier.drain(retry_delay=0)

    return tick


def add_alert(user, threshold, alert_type, **kwargs):
    alert = Alert(price_threshold=threshold, alert_type=alert_type, user_id=user.id, **kwargs)
    db.session.add(alert)
    db.session.commit()
    return alert


def test_ticks_inside_the_band_skip_the_database(setup, queries, tick):
    services, user, placed = setup
    add_alert(user, 70000.0, "above")
    add_alert(user, 60000.0, "below")

    tick(65000.0)  # first tick: full evaluation, builds the band
    queries.clear()
    for price in (65001.0, 64000.0, 69999.99, 60000.01):
        tick(price)

    assert queries == []
    assert placed == []


def test_leaving_the_band_fires(setup, tick):
    services, user, placed = setup
    add_alert(user, 70000.0, "above")
    tick(65000.0)
    tick(70000.0)
    assert len(placed) == 1


def test_a_new_alert_ends_the_band(setup, tick):
    services, user, placed = setup
    add_alert(user, 70000.0, "above")
    tick(65000.0)

    add_alert(user, 65500.0, "above")  # inside the old band
    tick(65600.0)

    assert len(placed) == 1
    assert "65,500" in placed[0]


def test_a_deleted_alert_does_not_fire(setup, tick):
    services, user, placed = setup
    near = add_alert(user, 66000.0, "above")
    add_alert(user, 70000.0, "above")
    tick(65000.0)

    db.session.delete(near)
    db.session.commit()
    tick(66500.0)

    assert placed == []


def test_disarmed_repeating_alert_rearms_through_the_band(setup, tick):
    services, user, placed = setup
    add_alert(user, 70000.0, "above", repeat=True)

    tick(70010.0)  # fires and disarms; re-arms below 70000 - 175
    tick(69900.0)  # inside the hysteresis band: quiet
    tick(70020.0)
    assert len(placed) == 1

    tick(69800.0)  # leaves the band: re-arms
    tick(70030.0)  # fires again
    assert len(placed) == 2


def test_changes_from_another_process_are_seen_after_the_full_scan_interval(setup, tick):
    services, user, placed = setup
    add_alert(user, 70000.0, "above")
    tick(65000.0)

    # A row written without this process's ORM (another worker, a SQL shell).
    db.session.execute(
        text(
            "INSERT INTO alert (price_threshold, alert_type, triggered, user_id, repeat, "
            "armed, notify_channel, created_at) "
            "VALUES (65500, 'above', 0, :uid, 0, 1, 'call', CURRENT_TIMESTAMP)"
        ),
        {"uid": user.id},
    )
    db.session.commit()

    tick(65600.0)
    assert placed == []  # still inside the remembered band

    services.engine.band.computed_at -= 10  # the full-scan interval passes
    tick(65600.0)
    assert len(placed) == 1


def test_changing_the_hysteresis_setting_ends_the_band(app, setup, queries, tick):
    services, user, placed = setup
    add_alert(user, 70000.0, "above")
    tick(65000.0)
    queries.clear()

    app.config["REARM_HYSTERESIS_PERCENT"] = 1.0
    tick(65001.0)
    assert queries


def test_an_alert_held_by_the_cooldown_keeps_the_band_open(app, setup, tick):
    """A due alert that could not fire yet must be retried on the next tick."""
    services, user, placed = setup
    app.config["NOTIFY_COOLDOWN_SECONDS"] = 300
    add_alert(user, 70000.0, "above")
    add_alert(user, 70001.0, "above")

    tick(70100.0)
    assert len(placed) == 1  # the second one is held by the per-user cooldown

    app.config["NOTIFY_COOLDOWN_SECONDS"] = 0
    tick(70100.0)
    assert len(placed) == 2
