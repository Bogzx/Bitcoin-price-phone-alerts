"""Phone-number ownership checks with one-time SMS codes.

Without this, the phone number is whatever a registrant types, so any account
can make the deployment call any number on the owner's Twilio balance. When
verification is required, alerts of unverified users never fire and every
page except settings, logout and this one redirects here.

Codes go through Twilio Verify, which also brings Twilio's own SMS-pumping
protection. In NOTIFY_DRY_RUN mode a local code is generated, stored as an
HMAC and written to the log instead, so the flow can be tried without Twilio.
"""

import hashlib
import hmac
import re
import secrets
import threading
from datetime import timedelta

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from .config import phone_verification_required
from .extensions import limiter
from .models import PhoneVerification, User, db, utcnow
from .notifications import mask_phone

bp = Blueprint("verify", __name__)

# Pages an unverified user can still use: they may need to fix a mistyped number,
# change their password, delete the account or simply leave.
EXEMPT_ENDPOINTS = {"auth.logout", "alerts.healthz", "static"}
EXEMPT_BLUEPRINTS = {"verify", "settings"}

CODE_RE = re.compile(r"^\d{4,10}$")


class VerificationError(Exception):
    """A problem the user should see, e.g. "That code has expired"."""


def verification_required(app=None):
    return phone_verification_required((app or current_app).config)


class PhoneVerifier:
    """Sends and checks verification codes for one app."""

    def __init__(self, app, notifier):
        self.app = app
        self.notifier = notifier

    @property
    def config(self):
        return self.app.config

    def _code_hash(self, code):
        key = self.config["SECRET_KEY"].encode()
        return hmac.new(key, code.encode(), hashlib.sha256).hexdigest()

    def _verify_service(self):
        client = self.notifier.twilio_client
        sid = self.config.get("TWILIO_VERIFY_SERVICE_SID")
        if client is None or not sid:
            raise VerificationError(
                "Phone verification is not configured on this deployment "
                "(TWILIO_VERIFY_SERVICE_SID). Ask the owner to set it up."
            )
        return client.verify.v2.services(sid)

    # Serialises "count recent sends, then record this one". Without it, parallel
    # requests all pass the caps before any of them is recorded. The app runs as
    # one process (see README), so a process-wide lock covers every request.
    _send_lock = threading.Lock()

    def _reserve_send(self, user, now):
        """Checks both send caps and records the send *before* it happens."""
        with self._send_lock:
            last_hour = PhoneVerification.query.filter(
                PhoneVerification.created_at >= now - timedelta(hours=1)
            )
            limit = self.config["VERIFY_MAX_SENDS_PER_HOUR"]
            if last_hour.filter(PhoneVerification.user_id == user.id).count() >= limit:
                raise VerificationError(
                    f"You have requested {limit} codes in the last hour. "
                    "Please wait and try again."
                )
            # Each code is an SMS on the owner's Twilio balance, so many accounts each
            # staying under their own limit must not add up to an unbounded bill.
            total_limit = self.config["VERIFY_MAX_SENDS_PER_HOUR_TOTAL"]
            if total_limit > 0 and last_hour.count() >= total_limit:
                self.app.logger.warning(
                    f"VERIFY_MAX_SENDS_PER_HOUR_TOTAL ({total_limit}) reached; refusing to "
                    f"send a code to {mask_phone(user.phone_number)}."
                )
                raise VerificationError(
                    "This deployment has sent too many verification codes in the last hour. "
                    "Please try again later."
                )
            reservation = PhoneVerification(
                user_id=user.id,
                phone_number=user.phone_number,
                status="sending",
                created_at=now,
                expires_at=now + timedelta(seconds=self.config["VERIFY_CODE_TTL_SECONDS"]),
            )
            db.session.add(reservation)
            db.session.commit()
            return reservation.id, reservation.phone_number

    def send(self, user):
        """Sends a new code to the user's current number. Raises VerificationError."""
        dry_run = self.config["NOTIFY_DRY_RUN"]
        service = None if dry_run else self._verify_service()  # fails before reserving
        reservation_id, phone = self._reserve_send(user, utcnow())

        code_hash = None
        if dry_run:
            code = f"{secrets.randbelow(10**6):06d}"
            code_hash = self._code_hash(code)
            self.app.logger.info(f"[dry-run] Verification code for {mask_phone(phone)}: {code}")
        else:
            try:
                service.verifications.create(to=phone, channel="sms")
            except Exception as exc:
                # The reservation stays and keeps counting: a send that failed may
                # still have cost money, and errors must not be a way around the caps.
                PhoneVerification.query.filter_by(id=reservation_id).update(
                    {"status": "failed"}, synchronize_session=False
                )
                db.session.commit()
                self.app.logger.error(
                    f"Twilio Verify refused to send to {mask_phone(phone)}: {exc}"
                )
                raise VerificationError(
                    "The code could not be sent. Please try again later."
                ) from exc
            self.app.logger.info(f"Verification code sent to {mask_phone(phone)}")

        PhoneVerification.query.filter(
            PhoneVerification.user_id == user.id,
            PhoneVerification.status == "pending",
            PhoneVerification.id != reservation_id,
        ).update({"status": "superseded"}, synchronize_session=False)
        # Only if still "sending": a number change meanwhile superseded it.
        PhoneVerification.query.filter_by(id=reservation_id, status="sending").update(
            {"status": "pending", "code_hash": code_hash}, synchronize_session=False
        )
        db.session.commit()

    def check(self, user, code):
        """Returns True and marks the phone verified when `code` is right.

        Returns False for a wrong code; raises VerificationError when no usable
        code exists (none sent, expired, too many attempts) or when the number
        changed while the code was being checked.
        """
        pending = (
            PhoneVerification.query.filter_by(
                user_id=user.id, status="pending", phone_number=user.phone_number
            )
            .order_by(PhoneVerification.id.desc())
            .first()
        )
        if pending is None:
            raise VerificationError("Send yourself a code first.")
        pending_id, phone = pending.id, pending.phone_number
        if utcnow() >= pending.expires_at:
            pending.status = "expired"
            db.session.commit()
            raise VerificationError("That code has expired. Send a new one.")
        # Claim an attempt with one conditional UPDATE before checking the code, so
        # parallel guesses cannot both pass a stale "attempts < max" check.
        claimed = (
            PhoneVerification.query.filter(
                PhoneVerification.id == pending_id,
                PhoneVerification.attempts < self.config["VERIFY_MAX_ATTEMPTS"],
            ).update(
                {PhoneVerification.attempts: PhoneVerification.attempts + 1},
                synchronize_session=False,
            )
        )
        db.session.commit()
        if not claimed:
            raise VerificationError("Too many wrong codes. Send a new one.")

        code = re.sub(r"\s", "", code or "")
        if not CODE_RE.match(code):
            return False
        if pending.code_hash is not None:
            approved = hmac.compare_digest(pending.code_hash, self._code_hash(code))
        else:
            approved = self._check_with_twilio(phone, code)
        if not approved:
            return False

        # The Twilio round trip can take seconds, and the number may have changed
        # meanwhile (settings, another session). Approve only if the code is still
        # pending AND the user still has the number it was sent to; both rows
        # change in one transaction or neither does.
        code_approved = PhoneVerification.query.filter_by(
            id=pending_id, status="pending"
        ).update({"status": "approved"}, synchronize_session=False)
        user_verified = User.query.filter(
            User.id == user.id, User.phone_number == phone
        ).update(
            {"phone_verified_at": utcnow(), "phone_verified_number": phone},
            synchronize_session=False,
        )
        if code_approved != 1 or user_verified != 1:
            db.session.rollback()
            self.app.logger.warning(
                f"Code for {mask_phone(phone)} was right, but user {user.id}'s number "
                "changed while it was being checked; not verified."
            )
            raise VerificationError(
                "Your phone number changed while the code was being checked. "
                "Send a code to the new number."
            )
        db.session.commit()
        self.app.logger.info(f"Phone {mask_phone(phone)} verified for user {user.id}")
        return True

    def _check_with_twilio(self, phone, code):
        service = self._verify_service()
        try:
            result = service.verification_checks.create(to=phone, code=code)
        except Exception as exc:
            # Twilio answers 404 once a verification expired, was approved or
            # used up its attempts.
            self.app.logger.warning(f"Twilio Verify check failed: {exc}")
            return False
        return getattr(result, "status", None) == "approved"

    def change_number(self, user, phone_number):
        """Sets a new number and forgets every verification of the old one.

        One explicit UPDATE rather than ORM change tracking: an object loaded
        before a concurrent verification would see nothing to reset and leave
        that verification in place.
        """
        User.query.filter_by(id=user.id).update(
            {"phone_number": phone_number, "phone_verified_at": None,
             "phone_verified_number": None},
            synchronize_session=False,
        )
        PhoneVerification.query.filter(
            PhoneVerification.user_id == user.id,
            PhoneVerification.status.in_(("pending", "sending")),
        ).update({"status": "superseded"}, synchronize_session=False)


def get_verifier():
    return current_app.extensions["btc_alerts"].verifier


@bp.before_app_request
def require_verified_phone():
    if not current_user.is_authenticated or current_user.phone_verified:
        return None
    if not verification_required():
        return None
    endpoint = request.endpoint or ""
    if endpoint in EXEMPT_ENDPOINTS or endpoint.split(".")[0] in EXEMPT_BLUEPRINTS:
        return None
    return redirect(url_for("verify.verify_phone"))


@bp.route("/verify-phone", methods=["GET"])
@login_required
def verify_phone():
    if current_user.phone_verified:
        return redirect(url_for("alerts.index"))
    return render_template(
        "verify_phone.html",
        masked_phone=mask_phone(current_user.phone_number),
        dry_run=current_app.config["NOTIFY_DRY_RUN"],
        required=verification_required(),
    )


@bp.route("/verify-phone/send", methods=["POST"])
@login_required
@limiter.limit(lambda: current_app.config["VERIFY_SEND_RATE_LIMIT"])
def send_code():
    if current_user.phone_verified:
        return redirect(url_for("alerts.index"))
    try:
        get_verifier().send(current_user)
    except VerificationError as exc:
        flash(str(exc), "danger")
    else:
        flash(f"Code sent to {mask_phone(current_user.phone_number)}.", "success")
    return redirect(url_for("verify.verify_phone"))


@bp.route("/verify-phone", methods=["POST"])
@login_required
@limiter.limit(lambda: current_app.config["VERIFY_CHECK_RATE_LIMIT"])
def check_code():
    if current_user.phone_verified:
        return redirect(url_for("alerts.index"))
    try:
        approved = get_verifier().check(current_user, request.form.get("code", ""))
    except VerificationError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("verify.verify_phone"))
    if not approved:
        flash("That code is not right.", "danger")
        return redirect(url_for("verify.verify_phone"))
    flash("Phone number verified. Your alerts can now call it.", "success")
    return redirect(url_for("alerts.index"))
