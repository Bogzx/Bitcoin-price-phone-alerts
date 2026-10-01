"""Account settings: every change needs the current password, a new phone number
loses its verification, and deleting the account removes its alerts."""

import pytest

from btc_alerts.models import (
    DELETED_USER_ID,
    Alert,
    NotificationLog,
    PhoneVerification,
    User,
    db,
    utcnow,
)

PASSWORD = "correct-horse"


@pytest.fixture
def user(app):
    user = User(username="sam", email="sam@example.com", phone_number="+14155550188")
    user.set_password(PASSWORD)
    user.mark_phone_verified()
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def client(app, user):
    with app.test_client() as client:
        with client.session_transaction() as session:
            session["_user_id"] = user.get_id()
            session["_fresh"] = True
        yield client


def body(response):
    return response.get_data(as_text=True)


def test_settings_page_renders_with_masked_number(client):
    page = body(client.get("/settings"))
    assert "+1******0188" in page and "+14155550188" not in page


def test_phone_change_needs_the_current_password(client, user):
    page = body(client.post(
        "/settings/phone",
        data={"phone_number": "+447700900123", "current_password": "wrong"},
        follow_redirects=True,
    ))
    assert "current password is not right" in page
    assert db.session.get(User, user.id).phone_number == "+14155550188"


def test_phone_change_rejects_invalid_numbers(client, user):
    page = body(client.post(
        "/settings/phone",
        data={"phone_number": "12345", "current_password": PASSWORD},
        follow_redirects=True,
    ))
    assert "international format" in page
    assert db.session.get(User, user.id).phone_number == "+14155550188"


def test_phone_change_resets_verification(client, user):
    db.session.add(PhoneVerification(user_id=user.id, phone_number=user.phone_number,
                                      status="pending", expires_at=utcnow()))
    db.session.commit()

    client.post("/settings/phone",
                data={"phone_number": "+44 7700 900123", "current_password": PASSWORD})

    refreshed = db.session.get(User, user.id)
    assert refreshed.phone_number == "+447700900123"
    assert refreshed.phone_verified is False
    assert PhoneVerification.query.one().status == "superseded"


def test_phone_change_with_verification_required_goes_to_verify(app, client):
    app.config["REQUIRE_PHONE_VERIFICATION"] = True
    response = client.post("/settings/phone",
                           data={"phone_number": "+447700900123", "current_password": PASSWORD})
    assert response.headers["Location"].endswith("/verify-phone")
    assert client.get("/").status_code == 302  # alerts are locked until verified


def test_password_change(client, user, app):
    def change(current, new, confirm):
        return body(client.post(
            "/settings/password",
            data={"current_password": current, "new_password": new, "confirm_password": confirm},
            follow_redirects=True,
        ))

    assert "not right" in change("wrong", "new-password-1", "new-password-1")
    assert "at least 8" in change(PASSWORD, "short", "short")
    assert "do not match" in change(PASSWORD, "new-password-1", "new-password-2")
    assert db.session.get(User, user.id).check_password(PASSWORD)

    assert "Password changed" in change(PASSWORD, "new-password-1", "new-password-1")
    assert db.session.get(User, user.id).check_password("new-password-1")


def test_delete_needs_the_password(client, user):
    client.post("/settings/delete", data={"current_password": "wrong"})
    assert db.session.get(User, user.id) is not None


def test_delete_removes_the_account_and_alerts_and_detaches_the_ledgers(client, user):
    user_id = user.id
    db.session.add(Alert(price_threshold=80000.0, alert_type="above", user_id=user_id))
    db.session.add(PhoneVerification(user_id=user_id, phone_number=user.phone_number,
                                      expires_at=utcnow()))
    db.session.add(NotificationLog(user_id=user_id, channel="call", status="sent", message="x"))
    db.session.commit()

    response = client.post("/settings/delete", data={"current_password": PASSWORD},
                           follow_redirects=True)

    assert "were deleted" in body(response)
    db.session.expire_all()
    assert db.session.get(User, user_id) is None
    assert Alert.query.count() == 0
    # Both ledgers keep counting toward the deployment-wide caps, but belong to no
    # account any more, and the sent code no longer records the phone number.
    code = PhoneVerification.query.one()
    assert (code.user_id, code.phone_number, code.code_hash) == (DELETED_USER_ID, "", None)
    assert NotificationLog.query.one().user_id == DELETED_USER_ID
    assert client.get("/").status_code == 302  # logged out


def test_settings_post_requires_csrf(app, client):
    app.config["WTF_CSRF_ENABLED"] = True
    response = client.post("/settings/delete", data={"current_password": PASSWORD})
    assert response.status_code == 400


def test_settings_posts_are_rate_limited(make_app):
    app = make_app(RATELIMIT_ENABLED=True, LOGIN_RATE_LIMIT="3 per minute")
    with app.app_context(), app.test_client() as client:
        owner = User(username="rl", email="rl@example.com", phone_number="+14155550188")
        owner.set_password(PASSWORD)
        db.session.add(owner)
        db.session.commit()
        with client.session_transaction() as session:
            session["_user_id"] = owner.get_id()
        statuses = [
            client.post("/settings/password", data={"current_password": "guess"}).status_code
            for _ in range(5)
        ]
        db.drop_all()
    assert 429 in statuses, statuses
