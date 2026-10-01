"""Sessions follow accounts, not ids; deleting accounts never reopens the
deployment; the caps that protect the Twilio bill survive account deletion.

These tests drive several "devices" (test clients) against one app, so they
must not hold an app context across requests: Flask would reuse it, and with
it Flask-Login's cached user in `g`, for every client. Database access happens
in short `with app.app_context()` blocks instead.
"""

import sqlite3
from datetime import timedelta

import pytest
from sqlalchemy import text

from btc_alerts import get_services
from btc_alerts.models import (
    DELETED_USER_ID,
    Alert,
    NotificationLog,
    PhoneVerification,
    User,
    db,
    utcnow,
)

PASSWORD = "password-123"


def make(make_app, **overrides):
    app = make_app(**overrides)
    get_services(app).feed.record_price(70000.0)
    return app


@pytest.fixture
def open_app(make_app):
    app = make(make_app, ALLOW_REGISTRATION=True, REQUIRE_PHONE_VERIFICATION=False,
               NOTIFY_DRY_RUN=True)
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()


def in_db(app, fn):
    """Runs fn() inside an app context and returns its result."""
    with app.app_context():
        try:
            return fn()
        finally:
            db.session.remove()


def user_id(app, username):
    return in_db(app, lambda: User.query.filter_by(username=username).one().id)


def register(client, name, phone="+14155550101"):
    return client.post("/register", data={
        "username": name, "email": f"{name}@example.com",
        "phone_number": phone, "password": PASSWORD,
    }, follow_redirects=True)


def login(client, name, remember=False):
    data = {"username": name, "password": PASSWORD}
    if remember:
        data["remember"] = "on"
    return client.post("/login", data=data)


def logged_in_as(client):
    """The username the dashboard greets, or None when sent to the login page."""
    response = client.get("/")
    if response.status_code != 200:
        return None
    return response.get_data(as_text=True).split("Hello, ", 1)[1].split("<", 1)[0].strip()


def delete_account(client):
    return client.post("/settings/delete", data={"current_password": PASSWORD})


# --- The reported takeover ----------------------------------------------------------

def test_deleted_users_session_does_not_open_the_next_account(open_app):
    """alice deletes her account on device 1, bob registers, and alice's device 2
    must not land in bob's dashboard (it did while ids were reused)."""
    device1, device2, bobs = (open_app.test_client() for _ in range(3))
    register(device1, "alice")
    login(device1, "alice")
    login(device2, "alice")
    alice_id = user_id(open_app, "alice")
    assert logged_in_as(device2) == "alice"

    delete_account(device1)
    register(bobs, "bob", phone="+14155550202")

    assert user_id(open_app, "bob") != alice_id
    assert logged_in_as(device2) is None
    device2.post("/add_alert", data={"mode": "absolute", "price_threshold": "80000"})
    assert in_db(open_app, lambda: Alert.query.count()) == 0  # nothing can be aimed at bob's phone


def test_session_token_alone_stops_the_takeover_even_if_an_id_is_reused(open_app):
    """Defence in depth for databases that still reuse ids: put bob on alice's
    old id by hand; her old session must still not resolve to him."""
    device = open_app.test_client()
    register(device, "alice")
    login(device, "alice")
    alice_id = user_id(open_app, "alice")
    assert logged_in_as(device) == "alice"

    def replace_alice_with_bob():
        db.session.execute(text('DELETE FROM "user" WHERE id = :id'), {"id": alice_id})
        bob = User(id=alice_id, username="bob", email="bob@example.com",
                   phone_number="+14155550202")
        bob.set_password(PASSWORD)
        db.session.add(bob)
        db.session.commit()

    in_db(open_app, replace_alice_with_bob)
    assert logged_in_as(device) is None


def test_remember_me_cookie_of_a_deleted_user_dies(open_app):
    device, other = open_app.test_client(), open_app.test_client()
    register(device, "alice")
    login(device, "alice", remember=True)
    login(other, "alice")
    device.delete_cookie("session")
    assert logged_in_as(device) == "alice"  # the remember-me cookie works

    delete_account(other)
    register(open_app.test_client(), "bob", phone="+14155550202")
    device.delete_cookie("session")

    assert logged_in_as(device) is None


def test_ids_are_never_reused(open_app):
    client = open_app.test_client()
    register(client, "alice")
    login(client, "alice")
    first_id = user_id(open_app, "alice")
    delete_account(client)
    register(open_app.test_client(), "bob")
    assert user_id(open_app, "bob") > first_id


def test_a_numeric_id_in_an_old_session_is_rejected(open_app):
    """Sessions from before the upgrade carry the id; they must not log anyone in."""
    client = open_app.test_client()
    register(client, "alice")
    with client.session_transaction() as session:
        session["_user_id"] = str(user_id(open_app, "alice"))
    assert logged_in_as(client) is None


# --- Password change -------------------------------------------------------------------

def test_password_change_signs_out_other_sessions_and_keeps_this_one(open_app):
    here, laptop, phone = (open_app.test_client() for _ in range(3))
    register(here, "alice")
    login(here, "alice", remember=True)
    login(laptop, "alice")
    login(phone, "alice", remember=True)
    assert logged_in_as(laptop) == "alice"

    def token():
        return in_db(open_app, lambda: User.query.one().session_token)

    old_token = token()
    here.post("/settings/password", data={
        "current_password": PASSWORD, "new_password": "new-password-1",
        "confirm_password": "new-password-1",
    })

    assert token() != old_token
    assert logged_in_as(here) == "alice"
    here.delete_cookie("session")
    assert logged_in_as(here) == "alice"  # its remember-me cookie was re-issued
    assert logged_in_as(laptop) is None
    phone.delete_cookie("session")
    assert logged_in_as(phone) is None  # an old remember-me cookie is dead too


# --- Deleting accounts never reopens the deployment ------------------------------------

@pytest.fixture
def owner_only(make_app, tmp_path):
    """A file database, so a restart can be simulated with a second app."""
    url = f"sqlite:///{tmp_path / 'alerts.db'}"
    return lambda: make(make_app, SQLALCHEMY_DATABASE_URI=url, ALLOW_REGISTRATION=False)


def test_deleting_the_only_account_keeps_registration_closed(owner_only):
    app = owner_only()
    owner = app.test_client()
    register(owner, "owner")
    login(owner, "owner")
    assert logged_in_as(owner) == "owner"
    delete_account(owner)
    assert in_db(app, lambda: User.query.count()) == 0

    visitor = app.test_client()
    assert "Register here" not in visitor.get("/login").get_data(as_text=True)
    page = register(visitor, "visitor", phone="+19005550100").get_data(as_text=True)
    assert "Registration is closed" in page
    assert in_db(app, lambda: User.query.count()) == 0

    restarted = owner_only()  # the flag is in the database, not in memory
    register(restarted.test_client(), "visitor")
    assert in_db(restarted, lambda: User.query.count()) == 0


def test_existing_deployment_is_marked_bootstrapped_on_upgrade(owner_only):
    def owner_from_before_this_version():
        db.session.add(User(username="owner", email="o@example.com",
                            phone_number="+14155550101", password_hash="x"))
        db.session.execute(text("DELETE FROM instance_state"))
        db.session.commit()

    in_db(owner_only(), owner_from_before_this_version)
    upgraded = owner_only()

    def delete_everyone():
        db.session.execute(text('DELETE FROM "user"'))
        db.session.commit()

    in_db(upgraded, delete_everyone)
    register(upgraded.test_client(), "visitor")
    assert in_db(upgraded, lambda: User.query.count()) == 0


def test_allow_registration_still_lets_the_owner_start_over(owner_only):
    app = owner_only()
    client = app.test_client()
    register(client, "owner")
    login(client, "owner")
    delete_account(client)
    app.config["ALLOW_REGISTRATION"] = True
    register(app.test_client(), "owner2")
    assert in_db(app, lambda: User.query.count()) == 1


# --- Upgrading a database from before this change ---------------------------------------

OLD_SCHEMA = """
CREATE TABLE user (
    id INTEGER PRIMARY KEY,
    username VARCHAR(64) NOT NULL UNIQUE,
    email VARCHAR(120) NOT NULL UNIQUE,
    phone_number VARCHAR(20) NOT NULL,
    password_hash VARCHAR(128) NOT NULL
);
CREATE TABLE alert (
    id INTEGER PRIMARY KEY,
    price_threshold FLOAT NOT NULL,
    alert_type VARCHAR(10) NOT NULL,
    triggered BOOLEAN,
    user_id INTEGER NOT NULL REFERENCES user(id)
);
INSERT INTO user VALUES (1, 'old', 'old@example.com', '+14155550123', 'x');
INSERT INTO user VALUES (2, 'old2', 'old2@example.com', '+14155550124', 'y');
INSERT INTO alert VALUES (1, 70000.0, 'above', 0, 1);
-- An alert still pointing at a user deleted by hand long ago.
INSERT INTO alert VALUES (2, 71000.0, 'above', 0, 7);
"""


def test_old_user_table_is_rebuilt_with_autoincrement_and_tokens(make_app, tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.close()
    app = make_app(SQLALCHEMY_DATABASE_URI=f"sqlite:///{path}")

    def check():
        users = User.query.order_by(User.id).all()
        assert [(u.id, u.username) for u in users] == [(1, "old"), (2, "old2")]
        tokens = {u.session_token for u in users}
        assert None not in tokens and len(tokens) == 2
        assert Alert.query.count() == 2
        newcomer = User(username="new", email="new@example.com",
                        phone_number="+14155550125", password_hash="z")
        db.session.add(newcomer)
        db.session.commit()
        return newcomer.id

    assert in_db(app, check) == 8  # above every id still referenced (alert -> user 7)

    conn = sqlite3.connect(path)
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'user'").fetchone()[0]
    indexes = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE tbl_name = 'user' AND type = 'index'"
    ).fetchone()[0]
    conn.close()
    assert "AUTOINCREMENT" in sql.upper()
    assert indexes == 3  # username, email and session_token stay unique

    again = make_app(SQLALCHEMY_DATABASE_URI=f"sqlite:///{path}")  # idempotent
    assert in_db(again, lambda: User.query.count()) == 3


# --- Verification codes ----------------------------------------------------------------

@pytest.fixture
def verifying(make_app):
    app = make(make_app, REQUIRE_PHONE_VERIFICATION=True, NOTIFY_DRY_RUN=True,
               VERIFY_MAX_ATTEMPTS=3)

    def make_user(name, phone):
        def create():
            user = User(username=name, email=f"{name}@example.com", phone_number=phone)
            user.set_password(PASSWORD)
            db.session.add(user)
            db.session.commit()
            return user.get_id()

        token = in_db(app, create)
        client = app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = token
        return client

    make_user.app = app
    yield make_user
    with app.app_context():
        db.session.remove()
        db.drop_all()


def test_attempts_never_exceed_the_cap(verifying):
    client = verifying("ann", "+14155550111")
    client.post("/verify-phone/send")
    for _ in range(6):
        client.post("/verify-phone", data={"code": "123456"})
    assert in_db(verifying.app, lambda: PhoneVerification.query.one().attempts) == 3


def test_deployment_wide_hourly_cap_on_code_sends(verifying):
    verifying.app.config["VERIFY_MAX_SENDS_PER_HOUR_TOTAL"] = 2
    clients = [verifying(f"u{i}", f"+1415555012{i}") for i in range(3)]
    clients[0].post("/verify-phone/send")
    clients[1].post("/verify-phone/send")
    page = clients[2].post("/verify-phone/send", follow_redirects=True).get_data(as_text=True)
    assert "too many verification codes" in page
    assert in_db(verifying.app, lambda: PhoneVerification.query.count()) == 2
    owners = in_db(verifying.app, lambda: {p.user_id for p in PhoneVerification.query})
    assert len(owners) == 2  # two different users, each under their own limit


def test_deleting_accounts_does_not_reset_the_hourly_cap(verifying):
    verifying.app.config["VERIFY_MAX_SENDS_PER_HOUR_TOTAL"] = 1
    first = verifying("gone", "+14155550131")
    first.post("/verify-phone/send")
    first.post("/settings/delete", data={"current_password": PASSWORD})
    assert in_db(verifying.app, lambda: User.query.filter_by(username="gone").count()) == 0

    second = verifying("next", "+14155550132")
    page = second.post("/verify-phone/send", follow_redirects=True).get_data(as_text=True)
    assert "too many verification codes" in page


# --- Restart recovery --------------------------------------------------------------------

def test_requeue_skips_users_whose_current_number_is_not_verified(app, services, monkeypatch):
    app.config["REQUIRE_PHONE_VERIFICATION"] = True
    calls = []
    monkeypatch.setattr(services.notifier, "call_user", lambda phone, msg: calls.append(phone))
    verified = User(username="v", email="v@example.com", phone_number="+14155550141")
    verified.mark_phone_verified()
    changed = User(username="c", email="c@example.com", phone_number="+14155550142")
    for user in (verified, changed):
        user.set_password(PASSWORD)
        db.session.add(user)
    db.session.commit()
    now = utcnow()
    for user in (verified, changed):
        db.session.add(NotificationLog(user_id=user.id, channel="call", status="queued",
                                       message=f"for {user.username}", created_at=now))
    db.session.commit()

    assert services.notifier.requeue_pending(now=now + timedelta(seconds=1)) == (1, 1)
    services.notifier.drain(retry_delay=0)

    assert calls == ["+14155550141"]
    skipped = NotificationLog.query.filter_by(user_id=changed.id).one()
    assert skipped.status == "failed" and "not verified" in skipped.detail


def test_deleted_users_ledger_is_shown_to_no_one(open_app):
    alice = open_app.test_client()
    register(alice, "alice")
    login(alice, "alice")
    alice_id = user_id(open_app, "alice")

    def add_entry():
        db.session.add(NotificationLog(user_id=alice_id, channel="call", status="sent",
                                       message="Bitcoin rose above 77,777.00 for alice"))
        db.session.commit()

    in_db(open_app, add_entry)
    delete_account(alice)

    bob = open_app.test_client()
    register(bob, "bob", phone="+14155550202")
    login(bob, "bob")
    assert logged_in_as(bob) == "bob"
    assert "77,777.00" not in bob.get("/").get_data(as_text=True)
    assert in_db(open_app, lambda: NotificationLog.query.one().user_id) == DELETED_USER_ID
