"""Delivery of calls and SMS through Twilio, off the price-tick path.

A Twilio call is a blocking HTTP request and must not stall the feed, and a
failure must not be able to destroy an alert that has already been marked as
fired. The engine therefore commits the alert first and queues a job here; a
background worker delivers it with retries and records how it ended.
"""

import queue
import time
from datetime import timedelta
from xml.sax.saxutils import escape as xml_escape

from twilio.http.http_client import TwilioHttpClient
from twilio.rest import Client

from .extensions import socketio
from .models import Alert, NotificationLog, User, db, utcnow

TWILIO_HTTP_TIMEOUT_SECONDS = 30


def mask_phone(phone_number):
    """Keeps the country code prefix and last 4 digits for logs: +1******0123."""
    phone = phone_number or ""
    if len(phone) <= 6:
        return "***"
    return phone[:2] + "*" * (len(phone) - 6) + phone[-4:]


def notification_cost(channel):
    """Twilio sends a notification makes: "both" is a call and an SMS."""
    return 2 if channel == "both" else 1


def notifications_in_last_day(now=None):
    """Calls plus SMS started in the last 24 hours (the budget's unit).

    Must be called inside an application context.
    """
    now = now or utcnow()
    since = now - timedelta(days=1)
    recent = NotificationLog.query.filter(NotificationLog.created_at >= since)
    return recent.count() + recent.filter(NotificationLog.channel == "both").count()


def build_twilio_client(config, logger):
    """Builds the Twilio client, tolerating a missing/placeholder configuration."""
    if config["NOTIFY_DRY_RUN"]:
        logger.warning("NOTIFY_DRY_RUN is on: alerts are logged, no call or SMS is placed.")
        return None
    sid = config.get("TWILIO_ACCOUNT_SID")
    token = config.get("TWILIO_AUTH_TOKEN")
    if not sid or not token:
        logger.warning("Twilio credentials are not configured; notifications will fail.")
        return None
    try:
        # Twilio's default HTTP client has no timeout: one hung request would
        # block the single notification worker, and every later alert, forever.
        return Client(sid, token, http_client=TwilioHttpClient(timeout=TWILIO_HTTP_TIMEOUT_SECONDS))
    except Exception as exc:  # pragma: no cover - depends on local configuration
        logger.error(f"Could not create the Twilio client: {exc}")
        return None


class Notifier:
    """Queue, worker and Twilio delivery for one app."""

    MAX_ATTEMPTS = 3
    RETRY_DELAY_SECONDS = 5
    # A notification still "queued" this long after a restart is not worth
    # delivering: the price has moved on and the call would only confuse.
    REQUEUE_MAX_AGE_SECONDS = 600

    def __init__(self, app):
        self.app = app
        self.queue = queue.Queue()
        self.twilio_client = build_twilio_client(app.config, app.logger)
        self.from_number = app.config["TWILIO_PHONE_NUMBER"]

    @property
    def logger(self):
        return self.app.logger

    @property
    def dry_run(self):
        return self.app.config["NOTIFY_DRY_RUN"]

    # -- Twilio ---------------------------------------------------------------------

    def _require_twilio(self):
        if self.twilio_client is None:
            raise RuntimeError("Twilio client is not configured")
        if not self.from_number:
            raise RuntimeError("TWILIO_PHONE_NUMBER is not configured")

    def call_user(self, phone_number, message):
        """Places a Twilio voice call that reads `message` out loud.

        Raises on failure - the caller decides what to do about it.
        """
        if self.dry_run:
            self.logger.info(f"[dry-run] Would call {mask_phone(phone_number)}: {message}")
            return "DRY-RUN"
        self._require_twilio()
        twiml = (
            "<Response><Say voice=\"alice\">{msg}</Say><Pause length=\"1\"/>"
            "<Say voice=\"alice\">{msg}</Say></Response>"
        ).format(msg=xml_escape(message))
        call = self.twilio_client.calls.create(
            to=phone_number,
            from_=self.from_number,
            twiml=twiml,
        )
        self.logger.info(f"Call initiated for {mask_phone(phone_number)}, SID: {call.sid}")
        return call.sid

    def sms_user(self, phone_number, message):
        """Sends a Twilio SMS. Raises on failure."""
        if self.dry_run:
            self.logger.info(f"[dry-run] Would text {mask_phone(phone_number)}: {message}")
            return "DRY-RUN"
        self._require_twilio()
        sms = self.twilio_client.messages.create(
            to=phone_number,
            from_=self.from_number,
            body=message,
        )
        self.logger.info(f"SMS sent to {mask_phone(phone_number)}, SID: {sms.sid}")
        return sms.sid

    # -- Delivery -------------------------------------------------------------------

    def deliver(self, job):
        """Sends one notification job. Raises on the first failing channel.

        Channels that already went out are remembered on the job, so a retry after
        "call ok, SMS failed" does not ring the phone a second time.
        """
        channel = job.get("channel", "call")
        done = job.setdefault("delivered", [])
        if channel in ("call", "both") and "call" not in done:
            self.call_user(job["phone_number"], job["message"])
            done.append("call")
            self._record_delivered(job.get("log_id"), done)
        if channel in ("sms", "both") and "sms" not in done:
            self.sms_user(job["phone_number"], job["message"])
            done.append("sms")
            self._record_delivered(job.get("log_id"), done)

    def _record_delivered(self, log_id, channels):
        """Persists which channels went out, so a restart does not repeat them."""
        if log_id is None:
            return
        with self.app.app_context():
            entry = db.session.get(NotificationLog, log_id)
            if entry is not None:
                entry.delivered = ",".join(channels)
                db.session.commit()

    def _finish_log(self, log_id, status, detail=None):
        """Records how a queued notification ended."""
        if log_id is None:
            return
        with self.app.app_context():
            entry = db.session.get(NotificationLog, log_id)
            if entry is None:
                return
            entry.status = status
            entry.detail = None if detail is None else str(detail)[:255]
            db.session.commit()

    def _record_error(self, alert_id, error):
        """Stores the delivery error on the alert instead of swallowing it."""
        with self.app.app_context():
            alert = db.session.get(Alert, alert_id)
            if alert is None:
                return
            alert.notify_error = str(error)[:255]
            db.session.commit()
            socketio.emit(
                "alert_failed",
                {"alert_id": alert_id, "error": alert.notify_error},
                to=f"user_{alert.user_id}",
            )

    def process(self, job, max_attempts=None, retry_delay=None):
        """Delivers a job, retrying on failure. Returns True when delivered."""
        max_attempts = self.MAX_ATTEMPTS if max_attempts is None else max_attempts
        retry_delay = self.RETRY_DELAY_SECONDS if retry_delay is None else retry_delay
        last_error = None
        for attempt in range(1, max_attempts + 1):
            try:
                self.deliver(job)
                self._finish_log(job.get("log_id"), "dry_run" if self.dry_run else "sent")
                return True
            except Exception as exc:
                last_error = exc
                self.logger.error(
                    f"Notification attempt {attempt}/{max_attempts} failed for "
                    f"alert {job.get('alert_id')}: {exc}"
                )
                if attempt < max_attempts and retry_delay:
                    time.sleep(retry_delay)
        self._record_error(job.get("alert_id"), last_error)
        self._finish_log(job.get("log_id"), "failed", last_error)
        return False

    def drain(self, **kwargs):
        """Processes every queued notification in the calling thread.

        Used by the tests and by anything that wants synchronous delivery.
        """
        processed = 0
        while True:
            try:
                job = self.queue.get_nowait()
            except queue.Empty:
                return processed
            try:
                self.process(job, **kwargs)
            finally:
                self.queue.task_done()
            processed += 1

    def requeue_pending(self, now=None):
        """Re-queues notifications that a restart dropped from the in-memory queue.

        The alert is committed as fired before the job is queued, so without this
        a crash or deploy in that window swallowed the call. Recent jobs are sent;
        old ones are marked failed so the user sees what happened.
        Returns (requeued, expired).
        """
        now = now or utcnow()
        cutoff = now - timedelta(seconds=self.REQUEUE_MAX_AGE_SECONDS)
        requeued = expired = 0
        with self.app.app_context():
            for entry in NotificationLog.query.filter_by(status="queued").all():
                user = db.session.get(User, entry.user_id)
                if user is None or entry.created_at < cutoff:
                    entry.status = "failed"
                    entry.detail = "Not delivered: the app restarted before sending it."
                    alert = db.session.get(Alert, entry.alert_id) if entry.alert_id else None
                    if alert is not None:
                        alert.notify_error = entry.detail
                    expired += 1
                    continue
                self.queue.put(
                    {
                        "log_id": entry.id,
                        "alert_id": entry.alert_id,
                        "user_id": user.id,
                        "phone_number": user.phone_number,
                        "channel": entry.channel,
                        "message": entry.message,
                        "delivered": entry.delivered.split(",") if entry.delivered else [],
                    }
                )
                requeued += 1
            db.session.commit()
        if requeued or expired:
            self.logger.warning(
                f"Recovered notifications after restart: {requeued} re-queued, "
                f"{expired} too old and marked failed."
            )
        return requeued, expired

    def run_worker(self):  # pragma: no cover - background thread
        while True:
            job = self.queue.get()
            try:
                self.process(job)
            except Exception as exc:
                self.logger.error(f"Unhandled error in notification worker: {exc}")
            finally:
                self.queue.task_done()
