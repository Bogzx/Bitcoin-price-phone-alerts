"""Smoke tests for the HTTP surface: templates render, CSRF is enforced,
the per-user alert cap holds, and thresholds are validated.
"""

import pytest

from btc_alerts import auth
from btc_alerts.extensions import socketio
from btc_alerts.models import Alert, NotificationLog, User, db


@pytest.fixture
def client(app, services):
    # CSRF is on here, unlike the rest of the suite. The auth rate limits stay
    # off (they are tested separately); they would make these tests order-dependent.
    app.config.update(WTF_CSRF_ENABLED=True, MAX_ACTIVE_ALERTS_PER_USER=5)
    services.feed.record_price(70000.0)
    with app.test_client() as client:
        yield client


@pytest.fixture
def feed(services):
    return services.feed


def make_user(client):
    user = User(username="bob", email="bob@example.com", phone_number="+14155550123")
    user.set_password("hunter2")
    db.session.add(user)
    db.session.commit()
    with client.session_transaction() as session:
        session["_user_id"] = user.get_id()
        session["_fresh"] = True
    return user


def csrf_token(client, path):
    """Pulls the CSRF token out of a rendered form."""
    html = client.get(path).get_data(as_text=True)
    marker = 'name="csrf_token" value="'
    start = html.index(marker) + len(marker)
    return html[start:html.index('"', start)]


def test_login_page_renders_with_flash_block(client):
    response = client.get("/login")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "csrf_token" in body
    # Flash messages are rendered by base.html, which login.html now extends.
    response = client.post("/login", data={"username": "nope", "password": "nope"})
    assert response.status_code == 400  # missing CSRF token


def test_post_without_csrf_token_is_rejected(client):
    make_user(client)
    response = client.post("/add_alert", data={"price_threshold": "80000"})
    assert response.status_code == 400
    assert Alert.query.count() == 0


def test_add_alert_with_token_succeeds(client):
    make_user(client)
    token = csrf_token(client, "/add_alert")
    response = client.post(
        "/add_alert",
        data={"csrf_token": token, "mode": "absolute", "price_threshold": "80000"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    alert = Alert.query.one()
    assert alert.price_threshold == 80000.0
    assert alert.alert_type == "above"


def test_alert_cap_is_enforced(client):
    user = make_user(client)
    for i in range(5):
        db.session.add(Alert(price_threshold=80000 + i, alert_type="above", user_id=user.id))
    db.session.commit()

    token = csrf_token(client, "/add_alert")
    response = client.post(
        "/add_alert",
        data={"csrf_token": token, "mode": "absolute", "price_threshold": "90000"},
        follow_redirects=True,
    )
    assert "limit 5" in response.get_data(as_text=True)
    assert Alert.query.count() == 5


@pytest.mark.parametrize("bad", ["nan", "inf", "-1", "0", "abc", "1e12"])
def test_invalid_thresholds_are_rejected(client, bad):
    make_user(client)
    token = csrf_token(client, "/add_alert")
    client.post(
        "/add_alert",
        data={"csrf_token": token, "mode": "absolute", "price_threshold": bad},
        follow_redirects=True,
    )
    assert Alert.query.count() == 0


def test_percent_trigger_converts_to_absolute_threshold(client):
    make_user(client)
    token = csrf_token(client, "/add_alert")
    client.post(
        "/add_alert",
        data={"csrf_token": token, "mode": "percent", "percent_change": "5"},
        follow_redirects=True,
    )
    alert = Alert.query.one()
    assert alert.price_threshold == pytest.approx(73500.0)
    assert alert.percent_change == 5.0
    assert alert.base_price == 70000.0
    assert alert.alert_type == "above"


@pytest.mark.parametrize(
    "phone", ["12345", "not-a-number", "0123456789", "+1", "555-1234"]
)
def test_registration_rejects_non_e164_numbers(client, phone):
    token = csrf_token(client, "/register")
    client.post(
        "/register",
        data={
            "csrf_token": token,
            "username": "carol",
            "email": "carol@example.com",
            "phone_number": phone,
            "password": "hunter2-long",
        },
        follow_redirects=True,
    )
    assert User.query.count() == 0


def test_registration_accepts_e164_and_hashes_the_password(client):
    token = csrf_token(client, "/register")
    client.post(
        "/register",
        data={
            "csrf_token": token,
            "username": "carol",
            "email": "carol@example.com",
            "phone_number": "+40 712 345 678",
            "password": "hunter2-long",
        },
        follow_redirects=True,
    )
    user = User.query.one()
    assert user.phone_number == "+40712345678"
    assert user.password_hash.startswith("pbkdf2:") or user.password_hash.startswith("scrypt:")
    assert "hunter2-long" not in user.password_hash


def test_login_is_rate_limited(make_app):
    app = make_app(RATELIMIT_ENABLED=True)
    with app.app_context(), app.test_client() as client:
        statuses = [
            client.post("/login", data={"username": "bob", "password": "wrong"}).status_code
            for _ in range(15)
        ]
        db.drop_all()
    assert 429 in statuses, statuses


def test_socketio_connect_is_rejected_for_anonymous_clients(app, client):
    sio = socketio.test_client(app)
    assert sio.is_connected() is False


def test_socketio_connect_works_for_logged_in_clients(app, client):
    """Guards against the auth/CSRF hardening silently killing live updates."""
    make_user(client)
    sio = socketio.test_client(app, flask_test_client=client)
    assert sio.is_connected() is True
    sio.disconnect()


def test_stale_price_blocks_alert_creation(client, feed):
    """The alert direction is chosen against the live price; a stale one lies."""
    make_user(client)
    token = csrf_token(client, "/add_alert")
    feed.status.last_tick_monotonic -= 10_000
    response = client.post(
        "/add_alert",
        data={"csrf_token": token, "mode": "absolute", "price_threshold": "80000"},
        follow_redirects=True,
    )
    assert "no recent price" in response.get_data(as_text=True)
    assert Alert.query.count() == 0


def test_dashboard_shows_stale_feed_banner(client, feed):
    make_user(client)
    assert 'id="feed-stale"' not in client.get("/").get_data(as_text=True)
    feed.status.last_tick_monotonic -= 10_000
    assert 'id="feed-stale"' in client.get("/").get_data(as_text=True)


def register_form(client, username):
    token = csrf_token(client, "/login")
    return client.post(
        "/register",
        data={
            "csrf_token": token,
            "username": username,
            "email": f"{username}@example.com",
            "phone_number": "+14155550199",
            "password": "hunter2-long",
        },
        follow_redirects=True,
    )


def test_registration_closes_after_the_first_account(app, client):
    app.config["ALLOW_REGISTRATION"] = False
    assert "Register here" in client.get("/login").get_data(as_text=True)
    register_form(client, "owner")
    assert User.query.count() == 1

    body = register_form(client, "stranger").get_data(as_text=True)
    assert "Registration is closed" in body
    assert User.query.count() == 1
    assert "Register here" not in client.get("/login").get_data(as_text=True)


def test_allow_registration_reopens_signup(app, client):
    app.config["ALLOW_REGISTRATION"] = True
    register_form(client, "owner")
    register_form(client, "friend")
    assert User.query.count() == 2


def test_dashboard_lists_recent_notifications(client):
    user = make_user(client)
    db.session.add(
        NotificationLog(user_id=user.id, channel="call", status="sent", message="BTC rose")
    )
    db.session.commit()
    body = client.get("/").get_data(as_text=True)
    assert "BTC rose" in body and "bg-success" in body


def test_threshold_equal_to_live_price_is_rejected(client):
    make_user(client)
    token = csrf_token(client, "/add_alert")
    response = client.post(
        "/add_alert",
        data={"csrf_token": token, "mode": "absolute", "price_threshold": "70000"},
        follow_redirects=True,
    )
    assert "equals the current price" in response.get_data(as_text=True)
    assert Alert.query.count() == 0


def test_below_alert_is_created_under_the_live_price(client):
    make_user(client)
    token = csrf_token(client, "/add_alert")
    client.post(
        "/add_alert",
        data={"csrf_token": token, "mode": "absolute", "price_threshold": "65000"},
        follow_redirects=True,
    )
    assert Alert.query.one().alert_type == "below"


def test_short_passwords_are_rejected(client):
    token = csrf_token(client, "/register")
    body = client.post(
        "/register",
        data={
            "csrf_token": token,
            "username": "erin",
            "email": "erin@example.com",
            "phone_number": "+14155550123",
            "password": "short",
        },
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "at least 8" in body
    assert User.query.count() == 0


def test_logout_requires_post(client):
    make_user(client)
    assert client.get("/logout").status_code == 405
    token = csrf_token(client, "/")
    response = client.post("/logout", data={"csrf_token": token})
    assert response.status_code == 302
    assert client.get("/").status_code == 302  # back to login


def test_first_account_registered_during_hashing_closes_signup(app, client, monkeypatch):
    """Two simultaneous first registrations: both pass the 'no users yet' check,
    then spend ~100 ms hashing. Here the other one commits during that window;
    this request must then be refused, not become a second account."""
    app.config["ALLOW_REGISTRATION"] = False
    original = User.set_password

    def hash_while_another_signup_lands(self, password):
        if not User.query.count():
            rival = User(username="rival", email="rival@example.com",
                         phone_number="+14155550100", password_hash="x")
            db.session.add(rival)
            db.session.commit()
        original(self, password)

    monkeypatch.setattr(User, "set_password", hash_while_another_signup_lands)
    body = register_form(client, "second").get_data(as_text=True)
    assert "Registration is closed" in body
    assert [u.username for u in User.query.all()] == ["rival"]


def test_lower_id_wins_if_another_process_registered_first(app, client, monkeypatch):
    """Cross-process backstop: if the in-process check was passed but a lower id
    exists after commit, the newcomer is removed again."""
    app.config["ALLOW_REGISTRATION"] = False
    owner = User(username="owner", email="owner@example.com",
                 phone_number="+14155550100", password_hash="x")
    db.session.add(owner)
    db.session.commit()
    monkeypatch.setattr(auth, "registration_open", lambda: True)  # the lost race
    body = register_form(client, "second").get_data(as_text=True)
    assert "Registration is closed" in body
    assert [u.username for u in User.query.all()] == ["owner"]
