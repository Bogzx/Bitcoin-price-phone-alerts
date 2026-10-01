"""Concurrency around phone verification, with a deliberately slow fake Twilio
Verify: a code check must never verify a number the code was not sent to, and
parallel "send code" requests must not exceed the send caps.

Like test_account_security.py, these tests never hold an app context across
requests (Flask would share Flask-Login's cached user between clients), and
they use a file database so each thread gets its own connection.
"""

import json
import threading
import time
from types import SimpleNamespace

import pytest

from btc_alerts import get_services
from btc_alerts.models import Alert, PhoneVerification, User, db, utcnow

PASSWORD = "password-123"
PHONE = "+14155550161"
OTHER_PHONE = "+447700900161"
GOOD_CODE = "424242"


class SlowVerify:
    """Stands in for client.verify.v2.services(sid), with configurable latency."""

    def __init__(self, send_delay=0.0, check_delay=0.0):
        self.send_delay = send_delay
        self.check_delay = check_delay
        self.sent = []
        self.checked = []
        self._lock = threading.Lock()
        self.verifications = SimpleNamespace(create=self._send)
        self.verification_checks = SimpleNamespace(create=self._check)

    def _send(self, to, channel):
        time.sleep(self.send_delay)
        with self._lock:
            self.sent.append(to)
        return SimpleNamespace(status="pending")

    def _check(self, to, code):
        time.sleep(self.check_delay)
        with self._lock:
            self.checked.append((to, code))
        return SimpleNamespace(status="approved" if code == GOOD_CODE else "pending")


@pytest.fixture
def race(make_app, tmp_path):
    app = make_app(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'race.db'}",
        REQUIRE_PHONE_VERIFICATION=True,
        NOTIFY_DRY_RUN=False,
        TWILIO_VERIFY_SERVICE_SID="VAtest",
        NOTIFY_COOLDOWN_SECONDS=0,
    )
    verify = SlowVerify()
    services = get_services(app)
    services.notifier.twilio_client = SimpleNamespace(
        verify=SimpleNamespace(v2=SimpleNamespace(services=lambda sid: verify))
    )
    services.feed.record_price(70000.0)
    return SimpleNamespace(app=app, verify=verify, services=services)


def in_db(app, fn):
    with app.app_context():
        try:
            return fn()
        finally:
            db.session.remove()


def make_user(app, name, phone):
    def create():
        user = User(username=name, email=f"{name}@example.com", phone_number=phone)
        user.set_password(PASSWORD)
        db.session.add(user)
        db.session.commit()
        return user.get_id()

    return in_db(app, create)


def session_for(app, token):
    """A separate device (own cookie jar) logged in as the user with `token`."""
    client = app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = token
    return client


def run_in_threads(*calls):
    results = [None] * len(calls)

    def runner(i, call):
        results[i] = call()

    threads = [threading.Thread(target=runner, args=(i, c)) for i, c in enumerate(calls)]
    for thread in threads:
        thread.start()
    return threads, results


def test_number_changed_during_a_slow_check_is_not_verified(race, monkeypatch):
    """The reported race: the check for P is in flight at Twilio (0.5 s) when
    another session changes the number to V (after 150 ms). V must not end up
    verified, and no alert may call it."""
    token = make_user(race.app, "val", PHONE)
    checking, settings = session_for(race.app, token), session_for(race.app, token)
    checking.post("/verify-phone/send")
    race.verify.check_delay = 0.5

    threads, results = run_in_threads(
        lambda: checking.post("/verify-phone", data={"code": GOOD_CODE},
                              follow_redirects=True).get_data(as_text=True)
    )
    time.sleep(0.15)
    settings.post("/settings/phone",
                  data={"phone_number": OTHER_PHONE, "current_password": PASSWORD})
    for thread in threads:
        thread.join()

    assert race.verify.checked == [(PHONE, GOOD_CODE)]  # the check really was in flight
    assert "Phone number verified" not in results[0]
    assert "changed while" in results[0]

    def state():
        user = User.query.one()
        return (user.phone_number, user.phone_verified,
                {row.status for row in PhoneVerification.query})

    phone, verified, statuses = in_db(race.app, state)
    assert phone == OTHER_PHONE
    assert verified is False
    assert "approved" not in statuses

    calls = []
    monkeypatch.setattr(race.services.notifier, "call_user",
                        lambda number, message: calls.append(number))

    def add_alert_and_tick():
        db.session.add(Alert(price_threshold=69000.0, alert_type="above",
                             user_id=User.query.one().id))
        db.session.commit()
        race.services.feed.on_message(None, json.dumps({"e": "trade", "p": "70000"}))

    in_db(race.app, add_alert_and_tick)
    race.services.notifier.drain(retry_delay=0)
    assert calls == []


def test_a_check_that_finishes_first_verifies_the_old_number_only(race):
    """The other ordering: the check completes, then the number changes. The old
    number was verified (correctly), and the new one starts unverified."""
    token = make_user(race.app, "wes", PHONE)
    client = session_for(race.app, token)
    client.post("/verify-phone/send")
    page = client.post("/verify-phone", data={"code": GOOD_CODE},
                       follow_redirects=True).get_data(as_text=True)
    assert "Phone number verified" in page
    client.post("/settings/phone",
                data={"phone_number": OTHER_PHONE, "current_password": PASSWORD})

    def state():
        user = User.query.one()
        return user.phone_number, user.phone_verified, user.phone_verified_number

    assert in_db(race.app, state) == (OTHER_PHONE, False, None)


def test_verified_number_must_match_the_current_number(race, monkeypatch):
    """Backstop: even if a stale write left phone_verified_at set, a number that
    differs from the verified one is treated as unverified everywhere."""
    calls = []
    monkeypatch.setattr(race.services.notifier, "call_user",
                        lambda number, message: calls.append(number))

    def stale_state():
        user = User(username="sid", email="sid@example.com", phone_number=OTHER_PHONE,
                    phone_verified_at=utcnow(), phone_verified_number=PHONE)
        user.set_password(PASSWORD)
        db.session.add(user)
        db.session.flush()
        db.session.add(Alert(price_threshold=69000.0, alert_type="above", user_id=user.id))
        db.session.commit()
        assert user.phone_verified is False
        race.services.feed.on_message(None, json.dumps({"e": "trade", "p": "70000"}))

    in_db(race.app, stale_state)
    race.services.notifier.drain(retry_delay=0)
    assert calls == []


def test_parallel_sends_respect_the_per_user_cap(race):
    race.app.config["VERIFY_MAX_SENDS_PER_HOUR"] = 1
    race.verify.send_delay = 0.3
    token = make_user(race.app, "pat", PHONE)
    devices = [session_for(race.app, token) for _ in range(4)]

    threads, _ = run_in_threads(*(lambda d=d: d.post("/verify-phone/send") for d in devices))
    for thread in threads:
        thread.join()

    assert race.verify.sent == [PHONE]
    assert in_db(race.app, lambda: PhoneVerification.query.count()) == 1


def test_parallel_sends_respect_the_deployment_cap(race):
    race.app.config["VERIFY_MAX_SENDS_PER_HOUR_TOTAL"] = 2
    race.verify.send_delay = 0.3
    devices = [
        session_for(race.app, make_user(race.app, f"u{i}", f"+1415555017{i}"))
        for i in range(5)
    ]

    threads, _ = run_in_threads(*(lambda d=d: d.post("/verify-phone/send") for d in devices))
    for thread in threads:
        thread.join()

    assert len(race.verify.sent) == 2
    assert in_db(race.app, lambda: PhoneVerification.query.count()) == 2


def test_number_change_between_the_alert_query_and_the_send(make_app, tmp_path, monkeypatch):
    """The engine selects alerts of verified users, then builds the job. A number
    change committed in between (hooked right after the query, from another
    connection) must not redirect the call to the new, unverified number."""
    from sqlalchemy import text

    from btc_alerts.engine import AlertEngine

    app = make_app(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'gap.db'}",
        REQUIRE_PHONE_VERIFICATION=True,
        NOTIFY_DRY_RUN=True,
        NOTIFY_COOLDOWN_SECONDS=0,
    )
    services = get_services(app)
    calls = []
    monkeypatch.setattr(services.notifier, "call_user",
                        lambda number, message: calls.append(number))

    def verified_user_with_alert():
        user = User(username="gap", email="gap@example.com", phone_number=PHONE)
        user.set_password(PASSWORD)
        user.mark_phone_verified()
        db.session.add(user)
        db.session.flush()
        db.session.add(Alert(price_threshold=69000.0, alert_type="above", user_id=user.id))
        db.session.commit()
        return user.id

    user_id = in_db(app, verified_user_with_alert)
    evaluate = AlertEngine._evaluate
    changed = []

    def change_number_then_evaluate(alert, *args):
        if not changed:  # another session commits a number change in the gap
            with db.engine.begin() as conn:
                conn.execute(
                    text('UPDATE "user" SET phone_number = :phone, phone_verified_at = NULL, '
                         "phone_verified_number = NULL WHERE id = :id"),
                    {"phone": OTHER_PHONE, "id": user_id},
                )
            changed.append(True)
        return evaluate(alert, *args)

    monkeypatch.setattr(AlertEngine, "_evaluate", staticmethod(change_number_then_evaluate))
    services.feed.on_message(None, json.dumps({"e": "trade", "p": "70000"}))
    services.notifier.drain(retry_delay=0)

    assert changed == [True]
    assert in_db(app, lambda: User.query.one().phone_number) == OTHER_PHONE
    # The alert was due for the number verified when it was selected; the new,
    # unverified number is never called.
    assert calls == [PHONE]
