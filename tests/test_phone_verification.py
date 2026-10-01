"""Phone ownership: unverified users cannot create alerts or be called, codes
expire and run out, and Twilio Verify is driven correctly."""

import json
import re
from datetime import timedelta
from types import SimpleNamespace

import pytest

from btc_alerts.config import load_config
from btc_alerts.models import Alert, PhoneVerification, User, db, utcnow
from btc_alerts.verification import verification_required

PHONE = "+14155550123"


@pytest.fixture
def verified_app(app, services):
    app.config.update(
        REQUIRE_PHONE_VERIFICATION=True,
        NOTIFY_DRY_RUN=True,
        NOTIFY_COOLDOWN_SECONDS=0,
    )
    services.feed.record_price(70000.0)
    return app


@pytest.fixture
def user(verified_app):
    user = User(username="vic", email="vic@example.com", phone_number=PHONE)
    user.set_password("correct-horse")
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def client(verified_app, user):
    with verified_app.test_client() as client:
        with client.session_transaction() as session:
            session["_user_id"] = str(user.id)
            session["_fresh"] = True
        yield client


def logged_code(caplog):
    """The code from the last "[dry-run] Verification code for …: 123456" line."""
    codes = re.findall(r"Verification code for \S+: (\d{6})", caplog.text)
    assert codes, caplog.text
    return codes[-1]


def send_and_read_code(client, caplog):
    caplog.set_level("INFO")
    client.post("/verify-phone/send")
    return logged_code(caplog)


def test_unverified_user_is_sent_to_the_verification_page(client):
    for path in ("/", "/add_alert"):
        response = client.get(path)
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/verify-phone")


def test_unverified_user_cannot_create_alerts(client):
    response = client.post("/add_alert", data={"mode": "absolute", "price_threshold": "80000"})
    assert response.status_code == 302
    assert Alert.query.count() == 0


def test_settings_logout_and_healthz_stay_reachable(client):
    assert client.get("/settings").status_code == 200
    assert client.get("/healthz").status_code == 200
    assert client.get("/verify-phone").status_code == 200


def test_dry_run_code_verifies_the_phone(client, user, caplog):
    code = send_and_read_code(client, caplog)
    assert PHONE not in caplog.text  # the number is masked even in dry-run logs

    response = client.post("/verify-phone", data={"code": code}, follow_redirects=True)

    assert "Phone number verified" in response.get_data(as_text=True)
    assert db.session.get(User, user.id).phone_verified
    assert client.get("/").status_code == 200
    assert PhoneVerification.query.one().status == "approved"


def test_code_is_stored_only_as_a_keyed_hash(client, caplog):
    code = send_and_read_code(client, caplog)
    row = PhoneVerification.query.one()
    assert code not in row.code_hash and len(row.code_hash) == 64


def test_wrong_code_is_refused_and_counted(client, user, caplog):
    code = send_and_read_code(client, caplog)
    wrong = "000000" if code != "000000" else "111111"

    body = client.post("/verify-phone", data={"code": wrong}, follow_redirects=True)

    assert "not right" in body.get_data(as_text=True)
    assert not db.session.get(User, user.id).phone_verified
    assert PhoneVerification.query.one().attempts == 1


def test_attempts_run_out_even_for_the_right_code(verified_app, client, user, caplog):
    verified_app.config["VERIFY_MAX_ATTEMPTS"] = 3
    code = send_and_read_code(client, caplog)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(3):
        client.post("/verify-phone", data={"code": wrong})

    body = client.post("/verify-phone", data={"code": code}, follow_redirects=True)

    assert "Too many wrong codes" in body.get_data(as_text=True)
    assert not db.session.get(User, user.id).phone_verified


def test_expired_code_is_refused(client, user, caplog):
    code = send_and_read_code(client, caplog)
    row = PhoneVerification.query.one()
    row.expires_at -= timedelta(hours=1)
    db.session.commit()

    body = client.post("/verify-phone", data={"code": code}, follow_redirects=True)

    assert "expired" in body.get_data(as_text=True)
    assert not db.session.get(User, user.id).phone_verified


def test_a_new_code_replaces_the_old_one(client, user, caplog):
    first = send_and_read_code(client, caplog)
    second = send_and_read_code(client, caplog)
    if first != second:
        client.post("/verify-phone", data={"code": first})
        assert not db.session.get(User, user.id).phone_verified
    client.post("/verify-phone", data={"code": second})
    assert db.session.get(User, user.id).phone_verified


def test_sends_per_hour_are_capped(verified_app, client, caplog):
    verified_app.config["VERIFY_MAX_SENDS_PER_HOUR"] = 2
    caplog.set_level("INFO")
    client.post("/verify-phone/send")
    client.post("/verify-phone/send")
    body = client.post("/verify-phone/send", follow_redirects=True).get_data(as_text=True)

    assert "requested 2 codes" in body
    assert PhoneVerification.query.count() == 2


def test_check_without_a_code_asks_to_send_one(client):
    body = client.post("/verify-phone", data={"code": "123456"}, follow_redirects=True)
    assert "Send yourself a code first" in body.get_data(as_text=True)


def test_a_code_for_the_old_number_does_not_verify_a_new_one(client, user, caplog):
    code = send_and_read_code(client, caplog)
    user.phone_number = "+14155550199"
    db.session.commit()

    body = client.post("/verify-phone", data={"code": code}, follow_redirects=True)

    assert "Send yourself a code first" in body.get_data(as_text=True)
    assert not db.session.get(User, user.id).phone_verified


# --- Twilio Verify -------------------------------------------------------------------

class FakeVerifyService:
    def __init__(self, approve_code="424242", fail_send=False):
        self.sent = []
        self.checked = []
        self.approve_code = approve_code
        self.fail_send = fail_send
        self.verifications = SimpleNamespace(create=self._send)
        self.verification_checks = SimpleNamespace(create=self._check)

    def _send(self, to, channel):
        if self.fail_send:
            raise RuntimeError("HTTP 400: invalid parameter To")
        self.sent.append((to, channel))
        return SimpleNamespace(status="pending")

    def _check(self, to, code):
        self.checked.append((to, code))
        return SimpleNamespace(status="approved" if code == self.approve_code else "pending")


@pytest.fixture
def twilio_verify(verified_app, services):
    verified_app.config.update(NOTIFY_DRY_RUN=False, TWILIO_VERIFY_SERVICE_SID="VAtest")
    service = FakeVerifyService()
    requested = []

    def services_(sid):
        requested.append(sid)
        return service

    services.notifier.twilio_client = SimpleNamespace(
        verify=SimpleNamespace(v2=SimpleNamespace(services=services_))
    )
    service.requested_sids = requested
    return service


def test_twilio_verify_sends_and_checks(client, user, twilio_verify):
    client.post("/verify-phone/send")
    assert twilio_verify.sent == [(PHONE, "sms")]
    assert twilio_verify.requested_sids == ["VAtest"]
    assert PhoneVerification.query.one().code_hash is None  # Twilio holds the code

    client.post("/verify-phone", data={"code": "111111"})
    assert not db.session.get(User, user.id).phone_verified

    client.post("/verify-phone", data={"code": "424 242"})
    assert twilio_verify.checked[-1] == (PHONE, "424242")
    assert db.session.get(User, user.id).phone_verified


def test_twilio_send_failure_is_reported_not_recorded(client, twilio_verify):
    twilio_verify.fail_send = True
    body = client.post("/verify-phone/send", follow_redirects=True).get_data(as_text=True)
    assert "could not be sent" in body
    assert PhoneVerification.query.count() == 0


def test_missing_verify_service_sid_is_a_clear_error(verified_app, client, services):
    verified_app.config.update(NOTIFY_DRY_RUN=False, TWILIO_VERIFY_SERVICE_SID=None)
    body = client.post("/verify-phone/send", follow_redirects=True).get_data(as_text=True)
    assert "TWILIO_VERIFY_SERVICE_SID" in body


def test_startup_reports_required_verification_without_a_service(make_app, caplog):
    make_app(REQUIRE_PHONE_VERIFICATION=True, NOTIFY_DRY_RUN=False)
    assert "nobody can verify a number" in caplog.text


def test_startup_warns_about_open_registration_without_verification(make_app, caplog):
    make_app(ALLOW_REGISTRATION=True, REQUIRE_PHONE_VERIFICATION=False)
    assert "anyone who registers can make this deployment call" in caplog.text


# --- Alerts and configuration ----------------------------------------------------------

def test_alerts_of_unverified_users_never_fire(verified_app, services, user, monkeypatch):
    calls = []
    monkeypatch.setattr(services.notifier, "call_user", lambda phone, msg: calls.append(phone))
    db.session.add(Alert(price_threshold=69000.0, alert_type="above", user_id=user.id))
    db.session.commit()

    def tick(price):
        services.feed.on_message(None, json.dumps({"e": "trade", "p": str(price)}))
        services.notifier.drain(retry_delay=0)

    tick(70000.0)
    assert calls == []

    user.phone_verified_at = utcnow()
    db.session.commit()
    tick(70000.0)  # the user row changed, so the quiet band is dropped
    assert calls == [PHONE]


def test_verification_follows_open_registration_unless_set():
    assert load_config({})["REQUIRE_PHONE_VERIFICATION"] is None
    for raw, expected in (("false", False), ("true", True)):
        config = load_config({"REQUIRE_PHONE_VERIFICATION": raw})
        assert config["REQUIRE_PHONE_VERIFICATION"] is expected


@pytest.mark.parametrize(
    "required, allow_registration, expected",
    [(None, False, False), (None, True, True), (False, True, False), (True, False, True)],
)
def test_verification_required_resolution(app, required, allow_registration, expected):
    app.config.update(REQUIRE_PHONE_VERIFICATION=required, ALLOW_REGISTRATION=allow_registration)
    assert verification_required(app) is expected


def test_owner_only_default_needs_no_verification(app, services):
    """The default deployment (registration closed) works exactly as before."""
    services.feed.record_price(70000.0)
    owner = User(username="own", email="own@example.com", phone_number=PHONE)
    owner.set_password("correct-horse")
    db.session.add(owner)
    db.session.commit()
    with app.test_client() as client:
        with client.session_transaction() as session:
            session["_user_id"] = str(owner.id)
        assert client.get("/").status_code == 200
