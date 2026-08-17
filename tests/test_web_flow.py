"""Smoke tests for the HTTP surface: templates render, CSRF is enforced,
the per-user alert cap holds, and thresholds are validated.
"""

import pytest

import app as app_module
from app import app as flask_app
from models import db, Alert, User


@pytest.fixture
def client():
    flask_app.config.update(
        TESTING=True,
        WTF_CSRF_ENABLED=True,
        SESSION_COOKIE_SECURE=False,
        MAX_ACTIVE_ALERTS_PER_USER=5,
    )
    # The auth rate limits are real (and tested separately); they would otherwise
    # make these tests order-dependent.
    app_module.limiter.enabled = False
    app_module.current_btc_price = 70000.0
    with flask_app.app_context():
        db.drop_all()
        db.create_all()
        with flask_app.test_client() as client:
            yield client
        db.session.remove()
        db.drop_all()
    app_module.current_btc_price = None
    app_module.limiter.enabled = True


def make_user(client):
    user = User(username="bob", email="bob@example.com", phone_number="+14155550123")
    user.set_password("hunter2")
    db.session.add(user)
    db.session.commit()
    with client.session_transaction() as session:
        session["_user_id"] = str(user.id)
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
            "password": "hunter2",
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
            "password": "hunter2",
        },
        follow_redirects=True,
    )
    user = User.query.one()
    assert user.phone_number == "+40712345678"
    assert user.password_hash.startswith("pbkdf2:") or user.password_hash.startswith("scrypt:")
    assert "hunter2" not in user.password_hash


def test_login_is_rate_limited(client):
    app_module.limiter.enabled = True
    app_module.limiter.reset()
    token = csrf_token(client, "/login")
    statuses = [
        client.post(
            "/login", data={"csrf_token": token, "username": "bob", "password": "wrong"}
        ).status_code
        for _ in range(15)
    ]
    app_module.limiter.enabled = False
    assert 429 in statuses, statuses


def test_socketio_connect_is_rejected_for_anonymous_clients(client):
    sio = app_module.socketio.test_client(flask_app)
    assert sio.is_connected() is False


def test_socketio_connect_works_for_logged_in_clients(client):
    """Guards against the auth/CSRF hardening silently killing live updates."""
    make_user(client)
    sio = app_module.socketio.test_client(flask_app, flask_test_client=client)
    assert sio.is_connected() is True
    sio.disconnect()
