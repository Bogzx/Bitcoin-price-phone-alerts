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
from .models import PhoneVerification, db, utcnow
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

    def send(self, user):
        """Sends a new code to the user's current number. Raises VerificationError."""
        now = utcnow()
        last_hour = PhoneVerification.query.filter(
            PhoneVerification.created_at >= now - timedelta(hours=1)
        )
        limit = self.config["VERIFY_MAX_SENDS_PER_HOUR"]
        if last_hour.filter(PhoneVerification.user_id == user.id).count() >= limit:
            raise VerificationError(
                f"You have requested {limit} codes in the last hour. Please wait and try again."
            )
        # Each code is an SMS on the owner's Twilio balance, so many accounts each
        # staying under their own limit must not add up to an unbounded bill.
        total_limit = self.config["VERIFY_MAX_SENDS_PER_HOUR_TOTAL"]
        if total_limit > 0 and last_hour.count() >= total_limit:
            self.app.logger.warning(
                f"VERIFY_MAX_SENDS_PER_HOUR_TOTAL ({total_limit}) reached; refusing to send "
                f"a code to {mask_phone(user.phone_number)}."
            )
            raise VerificationError(
                "This deployment has sent too many verification codes in the last hour. "
                "Please try again later."
            )

        code_hash = None
        if self.config["NOTIFY_DRY_RUN"]:
            code = f"{secrets.randbelow(10**6):06d}"
            code_hash = self._code_hash(code)
            self.app.logger.info(
                f"[dry-run] Verification code for {mask_phone(user.phone_number)}: {code}"
            )
        else:
            service = self._verify_service()
            try:
                service.verifications.create(to=user.phone_number, channel="sms")
            except Exception as exc:
                self.app.logger.error(
                    f"Twilio Verify refused to send to {mask_phone(user.phone_number)}: {exc}"
                )
                raise VerificationError(
                    "The code could not be sent. Please try again later."
                ) from exc
            self.app.logger.info(f"Verification code sent to {mask_phone(user.phone_number)}")

        PhoneVerification.query.filter_by(user_id=user.id, status="pending").update(
            {"status": "superseded"}
        )
        db.session.add(
            PhoneVerification(
                user_id=user.id,
                phone_number=user.phone_number,
                code_hash=code_hash,
                created_at=now,
                expires_at=now + timedelta(seconds=self.config["VERIFY_CODE_TTL_SECONDS"]),
            )
        )
        db.session.commit()

    def check(self, user, code):
        """Returns True and marks the phone verified when `code` is right.

        Returns False for a wrong code; raises VerificationError when no usable
        code exists (none sent, expired, too many attempts).
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
        now = utcnow()
        if now >= pending.expires_at:
            pending.status = "expired"
            db.session.commit()
            raise VerificationError("That code has expired. Send a new one.")
        # Claim an attempt with one conditional UPDATE before checking the code, so
        # parallel guesses cannot both pass a stale "attempts < max" check.
        claimed = (
            PhoneVerification.query.filter(
                PhoneVerification.id == pending.id,
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
            approved = self._check_with_twilio(user, code)
        if not approved:
            return False

        pending.status = "approved"
        user.phone_verified_at = now
        db.session.commit()
        self.app.logger.info(f"Phone {mask_phone(user.phone_number)} verified for user {user.id}")
        return True

    def _check_with_twilio(self, user, code):
        service = self._verify_service()
        try:
            result = service.verification_checks.create(to=user.phone_number, code=code)
        except Exception as exc:
            # Twilio answers 404 once a verification expired, was approved or
            # used up its attempts.
            self.app.logger.warning(f"Twilio Verify check failed: {exc}")
            return False
        return getattr(result, "status", None) == "approved"

    def reset(self, user):
        """Forgets the verification, e.g. after the number changed."""
        user.phone_verified_at = None
        PhoneVerification.query.filter_by(user_id=user.id, status="pending").update(
            {"status": "superseded"}
        )


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
