"""Registration, login and logout."""

import threading

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from .extensions import limiter, login_manager
from .models import User, db
from .validation import MIN_PASSWORD_LENGTH, normalize_phone_number

bp = Blueprint("auth", __name__)

# Serialises the "is anyone registered yet?" check with the insert. Without it,
# two simultaneous first registrations both see an empty table and both get in.
_registration_lock = threading.Lock()


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def _registration_closed():
    flash(
        "Registration is closed on this deployment. Ask the owner to set "
        "ALLOW_REGISTRATION=true if you need an account.",
        "warning",
    )
    return redirect(url_for("auth.login"))


def registration_open():
    """Registration is for the owner's first account unless explicitly opened.

    Phone numbers are not verified, so each extra account is someone who can
    make this deployment call any number on the owner's Twilio balance.
    """
    if current_app.config["ALLOW_REGISTRATION"]:
        return True
    return db.session.query(User.id).first() is None


@bp.app_context_processor
def inject_registration_open():
    return {"registration_open": registration_open}


@bp.route("/register", methods=["GET", "POST"])
@limiter.limit(lambda: current_app.config["REGISTER_RATE_LIMIT"], methods=["POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("alerts.index"))
    if not registration_open():
        return _registration_closed()

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        if not username or not email or not password:
            flash("Username, email and password are required.", "danger")
            return redirect(url_for("auth.register"))
        if len(password) < MIN_PASSWORD_LENGTH:
            flash(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.", "danger")
            return redirect(url_for("auth.register"))

        # The phone number is whatever the registrant types and is never verified,
        # so at minimum it has to be a plausible E.164 number.
        phone_number, error = normalize_phone_number(request.form.get("phone_number"))
        if error:
            flash(error, "danger")
            return redirect(url_for("auth.register"))

        new_user = User(username=username, email=email, phone_number=phone_number)
        new_user.set_password(password)  # slow (scrypt), so outside the lock

        with _registration_lock:
            # Checked again: the check at the top ran before the password hashing.
            if not registration_open():
                return _registration_closed()
            if User.query.filter(
                (User.username == username) | (User.email == email)
            ).first():
                flash("Username or email already exists.", "danger")
                return redirect(url_for("auth.register"))
            db.session.add(new_user)
            db.session.commit()

        # Another process (only one is supported, but still) may have registered
        # the first account at the same moment: the lowest id keeps it.
        if not current_app.config["ALLOW_REGISTRATION"]:
            first_id = db.session.query(db.func.min(User.id)).scalar()
            if first_id != new_user.id:
                db.session.delete(new_user)
                db.session.commit()
                return _registration_closed()

        flash("Registration successful. Please log in.", "success")
        return redirect(url_for("auth.login"))

    return render_template("register.html")


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit(lambda: current_app.config["LOGIN_RATE_LIMIT"], methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("alerts.index"))

    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()

        if user is None or not user.check_password(password):
            flash("Invalid username or password.", "danger")
            return redirect(url_for("auth.login"))

        login_user(user, remember=request.form.get("remember") == "on")
        flash("Logged in successfully.", "success")
        return redirect(url_for("alerts.index"))

    return render_template("login.html")


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("Logged out successfully.", "success")
    return redirect(url_for("auth.login"))
