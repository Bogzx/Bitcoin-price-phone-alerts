"""Account settings: phone number, password, account deletion.

Every change needs the current password: a stolen session cookie alone must
not be able to point the alerts at another number or lock the owner out.
"""

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, logout_user

from .extensions import limiter
from .models import Alert, PhoneVerification, User, db
from .notifications import mask_phone
from .validation import MIN_PASSWORD_LENGTH, normalize_phone_number
from .verification import get_verifier, verification_required

bp = Blueprint("settings", __name__, url_prefix="/settings")


def _rate_limit():
    return current_app.config["LOGIN_RATE_LIMIT"]


def _password_ok():
    if current_user.check_password(request.form.get("current_password", "")):
        return True
    flash("Your current password is not right.", "danger")
    return False


@bp.route("", methods=["GET"])
@login_required
def index():
    return render_template(
        "settings.html",
        masked_phone=mask_phone(current_user.phone_number),
        verification_required=verification_required(),
        min_password_length=MIN_PASSWORD_LENGTH,
    )


@bp.route("/phone", methods=["POST"])
@login_required
@limiter.limit(_rate_limit)
def change_phone():
    if not _password_ok():
        return redirect(url_for("settings.index"))
    phone_number, error = normalize_phone_number(request.form.get("phone_number"))
    if error:
        flash(error, "danger")
        return redirect(url_for("settings.index"))
    if phone_number == current_user.phone_number:
        flash("That is already your phone number.", "info")
        return redirect(url_for("settings.index"))

    current_user.phone_number = phone_number
    # A code only ever proved ownership of the old number.
    get_verifier().reset(current_user)
    db.session.commit()
    current_app.logger.info(
        f"User {current_user.id} changed their phone number to {mask_phone(phone_number)}"
    )
    if verification_required():
        flash("Phone number updated. Confirm it with a code before alerts can call it.", "warning")
        return redirect(url_for("verify.verify_phone"))
    flash("Phone number updated.", "success")
    return redirect(url_for("settings.index"))


@bp.route("/password", methods=["POST"])
@login_required
@limiter.limit(_rate_limit)
def change_password():
    if not _password_ok():
        return redirect(url_for("settings.index"))
    new_password = request.form.get("new_password", "")
    if len(new_password) < MIN_PASSWORD_LENGTH:
        flash(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.", "danger")
        return redirect(url_for("settings.index"))
    if new_password != request.form.get("confirm_password", ""):
        flash("The two new passwords do not match.", "danger")
        return redirect(url_for("settings.index"))
    current_user.set_password(new_password)
    db.session.commit()
    flash("Password changed.", "success")
    return redirect(url_for("settings.index"))


@bp.route("/delete", methods=["POST"])
@login_required
@limiter.limit(_rate_limit)
def delete_account():
    if not _password_ok():
        return redirect(url_for("settings.index"))
    user = db.session.get(User, current_user.id)
    # The notification log is kept: it is the ledger for MAX_NOTIFICATIONS_PER_DAY
    # and holds no phone number, only the user id and the message text.
    Alert.query.filter_by(user_id=user.id).delete()
    PhoneVerification.query.filter_by(user_id=user.id).delete()
    logout_user()
    db.session.delete(user)
    db.session.commit()
    current_app.logger.info(f"User {user.id} deleted their account")
    flash("Your account and its alerts were deleted.", "success")
    return redirect(url_for("auth.login"))
