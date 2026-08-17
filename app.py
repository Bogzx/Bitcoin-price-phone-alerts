import json
import math
import queue
import re
import socket
import threading
import time
from xml.sax.saxutils import escape as xml_escape

from flask import Flask, render_template, request, redirect, url_for, flash
from flask_socketio import SocketIO, emit, join_room
from flask_login import LoginManager, current_user, login_user, logout_user, login_required
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from twilio.rest import Client
import websocket

# Initialize Flask app and load configuration
app = Flask(__name__)
app.config.from_object("config.Config")

# CSRF protection for every POST form (login, register, add_alert, delete_alert)
csrf = CSRFProtect(app)

# Initialize SocketIO for live updates. Origins come from CORS_ALLOWED_ORIGINS.
socketio = SocketIO(
    app,
    async_mode="threading",
    cors_allowed_origins=app.config["CORS_ALLOWED_ORIGINS"],
)

# Basic brute-force brake on the auth routes. In-memory storage is fine for a
# single-process deployment, which is the only supported topology (see README).
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

# Initialize Flask-Login
login_manager = LoginManager(app)
login_manager.login_view = "login"

# Import database and models
from models import db, utcnow, Alert, User

# Bind SQLAlchemy to the app and create tables if needed
db.init_app(app)
with app.app_context():
    db.create_all()


def _build_twilio_client():
    """Builds the Twilio client, tolerating a missing/placeholder configuration."""
    sid = app.config.get("TWILIO_ACCOUNT_SID")
    token = app.config.get("TWILIO_AUTH_TOKEN")
    if not sid or not token:
        app.logger.warning(
            "Twilio credentials are not configured; notifications will fail."
        )
        return None
    try:
        return Client(sid, token)
    except Exception as exc:  # pragma: no cover - depends on local configuration
        app.logger.error(f"Could not create the Twilio client: {exc}")
        return None


# Setup Twilio client
twilio_client = _build_twilio_client()
twilio_phone_number = app.config["TWILIO_PHONE_NUMBER"]

# Global variable for the current BTC price
current_btc_price = None

# Notifications are dispatched off the price-tick handler: a Twilio call is a
# blocking HTTP request and must not stall the feed, and a failure must not be
# able to destroy an alert that has already been marked as fired.
notification_queue = queue.Queue()

NOTIFY_MAX_ATTEMPTS = 3
NOTIFY_RETRY_DELAY_SECONDS = 5

PHONE_RE = re.compile(r"^\+[1-9]\d{7,14}$")
VALID_CHANNELS = ("call", "sms", "both")


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ---------------------------------------------------------------------------
# Notification delivery
# ---------------------------------------------------------------------------

def call_user(phone_number, message):
    """Places a Twilio voice call that reads `message` out loud.

    Raises on failure - the caller decides what to do about it.
    """
    if twilio_client is None:
        raise RuntimeError("Twilio client is not configured")
    twiml = (
        "<Response><Say voice=\"alice\">{msg}</Say><Pause length=\"1\"/>"
        "<Say voice=\"alice\">{msg}</Say></Response>"
    ).format(msg=xml_escape(message))
    call = twilio_client.calls.create(
        to=phone_number,
        from_=twilio_phone_number,
        twiml=twiml,
    )
    app.logger.info(f"Call initiated for {phone_number}, SID: {call.sid}")
    return call.sid


def sms_user(phone_number, message):
    """Sends a Twilio SMS. Raises on failure."""
    if twilio_client is None:
        raise RuntimeError("Twilio client is not configured")
    sms = twilio_client.messages.create(
        to=phone_number,
        from_=twilio_phone_number,
        body=message,
    )
    app.logger.info(f"SMS sent to {phone_number}, SID: {sms.sid}")
    return sms.sid


def deliver_notification(job):
    """Sends one notification job. Raises on the first failing channel."""
    channel = job.get("channel", "call")
    if channel in ("call", "both"):
        call_user(job["phone_number"], job["message"])
    if channel in ("sms", "both"):
        sms_user(job["phone_number"], job["message"])


def record_notification_error(alert_id, error):
    """Stores the delivery error on the alert instead of swallowing it."""
    with app.app_context():
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


def process_notification(job, max_attempts=NOTIFY_MAX_ATTEMPTS, retry_delay=NOTIFY_RETRY_DELAY_SECONDS):
    """Delivers a job, retrying on failure. Returns True when delivered."""
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            deliver_notification(job)
            return True
        except Exception as exc:
            last_error = exc
            app.logger.error(
                f"Notification attempt {attempt}/{max_attempts} failed for "
                f"alert {job.get('alert_id')}: {exc}"
            )
            if attempt < max_attempts and retry_delay:
                time.sleep(retry_delay)
    record_notification_error(job.get("alert_id"), last_error)
    return False


def drain_notification_queue(**kwargs):
    """Processes every queued notification in the calling thread.

    Used by the tests and by anything that wants synchronous delivery.
    """
    processed = 0
    while True:
        try:
            job = notification_queue.get_nowait()
        except queue.Empty:
            return processed
        try:
            process_notification(job, **kwargs)
        finally:
            notification_queue.task_done()
        processed += 1


def notification_worker():  # pragma: no cover - background thread
    while True:
        job = notification_queue.get()
        try:
            process_notification(job)
        except Exception as exc:
            app.logger.error(f"Unhandled error in notification worker: {exc}")
        finally:
            notification_queue.task_done()


# ---------------------------------------------------------------------------
# Price feed
# ---------------------------------------------------------------------------

def process_price_tick(price):
    """Evaluates every active alert against `price`.

    Alert state is committed *before* the notification is dispatched, so a Twilio
    failure can never silently consume an alert.
    Must be called inside an application context.
    """
    now = utcnow()
    user_cooldown = app.config["NOTIFY_COOLDOWN_SECONDS"]
    repeat_cooldown = app.config["REPEAT_ALERT_COOLDOWN_SECONDS"]

    pending = []
    changed = False
    for alert in Alert.query.filter_by(triggered=False).all():
        # A repeating alert re-arms once the price leaves its trigger zone.
        if alert.repeat and not alert.armed and not alert.condition_met(price):
            alert.armed = True
            changed = True
            continue
        if not alert.is_due(price, repeat_cooldown, now):
            continue
        user = alert.user
        if user.notification_cooldown_active(user_cooldown, now):
            app.logger.info(
                f"Alert {alert.id} is due but user {user.id} is within the "
                f"{user_cooldown}s notification cooldown; skipping."
            )
            continue

        app.logger.info(
            f"Triggering alert {alert.id} for user {user.username}: "
            f"BTC {alert.alert_type} {alert.price_threshold}"
        )
        alert.last_triggered_at = now
        alert.notify_error = None
        if alert.repeat:
            alert.armed = False
        else:
            alert.triggered = True
        # Setting this here also caps a user to one notification per tick.
        user.last_notified_at = now
        changed = True
        pending.append(
            {
                "alert_id": alert.id,
                "user_id": user.id,
                "phone_number": user.phone_number,
                "channel": alert.notify_channel,
                "message": f"{alert.describe()}. The current price is {price:,.2f}.",
                "repeat": alert.repeat,
                "price_threshold": alert.price_threshold,
                "alert_type": alert.alert_type,
            }
        )

    # Avoid a write transaction on every trade tick when nothing changed.
    if changed:
        db.session.commit()

    for job in pending:
        socketio.emit(
            "alert_triggered",
            {
                "alert_id": job["alert_id"],
                "price_threshold": job["price_threshold"],
                "alert_type": job["alert_type"],
                "price": price,
                "repeat": job["repeat"],
            },
            to=f"user_{job['user_id']}",
        )
        notification_queue.put(job)
    return pending


def on_message(ws, message):
    """Handles messages from Binance's WebSocket."""
    global current_btc_price
    try:
        data = json.loads(message)
        price = float(data.get("p", 0))
        if not math.isfinite(price) or price <= 0:
            return
        current_btc_price = price
        app.logger.debug(f"Current BTC Price: {price}")

        # Emit the updated BTC price to connected clients
        socketio.emit("price_update", {"price": price})

        with app.app_context():
            process_price_tick(price)
    except Exception as exc:
        app.logger.error(f"Error in on_message: {exc}")


def on_error(ws, error):
    app.logger.error(f"WebSocket error: {error}")


def on_close(ws, close_status_code, close_msg):
    # No reconnect here: run_binance_ws() owns the reconnect loop. Calling
    # start_binance_ws() from this callback would recurse and eventually blow
    # the stack, because Binance closes long-lived streams roughly daily.
    app.logger.info(f"WebSocket connection closed ({close_status_code} {close_msg}).")


def on_open(ws):
    app.logger.info("WebSocket connection established.")


def start_binance_ws():
    """Runs one Binance WebSocket session (returns when the stream closes)."""
    websocket.enableTrace(False)
    ws_url = "wss://stream.binance.com:9443/ws/btcusdt@trade"
    ws = websocket.WebSocketApp(
        ws_url,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
        on_open=on_open,
    )
    ws.run_forever()


def run_binance_ws():  # pragma: no cover - network loop
    """Reconnect loop for the Binance feed."""
    while True:
        try:
            start_binance_ws()
        except Exception as exc:
            app.logger.error(f"Binance WebSocket crashed: {exc}")
        app.logger.info("Binance stream ended. Reconnecting in 5 seconds...")
        time.sleep(5)


_price_feed_lock_socket = None


def acquire_price_feed_lock():
    """Cross-process mutex so only one worker ever runs the price feed.

    Two workers running the feed would each evaluate every alert and place
    duplicate calls. Binding a localhost port is a portable way to make the
    single-owner constraint enforceable rather than merely documented.
    """
    global _price_feed_lock_socket
    port = app.config["PRICE_FEED_LOCK_PORT"]
    if port <= 0:
        return True
    lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock_socket.bind(("127.0.0.1", port))
        lock_socket.listen(1)
    except OSError:
        lock_socket.close()
        return False
    _price_feed_lock_socket = lock_socket
    return True


def start_price_feed():
    """Starts the price feed and the notification worker, at most once."""
    if not app.config["RUN_PRICE_FEED"]:
        app.logger.warning(
            "RUN_PRICE_FEED is disabled: this process will not evaluate alerts."
        )
        return False
    if not acquire_price_feed_lock():
        app.logger.warning(
            "Another process already owns the price feed lock on port "
            f"{app.config['PRICE_FEED_LOCK_PORT']}; this worker will not run the feed."
        )
        return False
    worker = threading.Thread(target=notification_worker, daemon=True)
    worker.start()
    socketio.start_background_task(target=run_binance_ws)
    app.logger.info("Binance price feed started.")
    return True


# ---------------------------------------------------------------------------
# Socket.IO
# ---------------------------------------------------------------------------

@socketio.on("connect")
def handle_connect(auth=None):
    """Rejects anonymous sockets and puts each user in their own room."""
    if not current_user.is_authenticated:
        return False
    join_room(f"user_{current_user.id}")
    if current_btc_price is not None:
        emit("price_update", {"price": current_btc_price})
    return True


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def normalize_phone_number(raw):
    """Returns (phone, error). Requires E.164, e.g. +14155550123."""
    phone = re.sub(r"[\s\-().]", "", raw or "")
    if not PHONE_RE.match(phone):
        return None, "Enter your phone number in international format, e.g. +14155550123."
    allowed = app.config["ALLOWED_PHONE_COUNTRY_CODES"]
    if allowed and not any(phone[1:].startswith(code) for code in allowed):
        return None, "That country calling code is not accepted by this deployment."
    return phone, None


def validate_threshold(value):
    """Returns (threshold, error) for a user supplied price threshold."""
    try:
        threshold = float(value)
    except (TypeError, ValueError):
        return None, "Invalid price threshold. Please enter a numeric value."
    if not math.isfinite(threshold):
        return None, "Price threshold must be a finite number."
    low = app.config["MIN_PRICE_THRESHOLD"]
    high = app.config["MAX_PRICE_THRESHOLD"]
    if not (low <= threshold <= high):
        return None, f"Price threshold must be between {low} and {high:,.0f}."
    return threshold, None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
@login_required
def index():
    # Show only alerts belonging to the logged-in user
    active_alerts = Alert.query.filter_by(user_id=current_user.id, triggered=False).all()
    triggered_alerts = Alert.query.filter_by(user_id=current_user.id, triggered=True).all()
    return render_template(
        "index.html",
        active_alerts=active_alerts,
        triggered_alerts=triggered_alerts,
        current_btc_price=current_btc_price,
        max_alerts=app.config["MAX_ACTIVE_ALERTS_PER_USER"],
    )


@app.route("/add_alert", methods=["GET", "POST"])
@login_required
def add_alert():
    max_alerts = app.config["MAX_ACTIVE_ALERTS_PER_USER"]
    if request.method == "POST":
        # Hard cap on outstanding alerts: every alert is a billable phone call.
        active_count = Alert.query.filter_by(
            user_id=current_user.id, triggered=False
        ).count()
        if active_count >= max_alerts:
            flash(
                f"You already have {active_count} active alerts (limit {max_alerts}). "
                "Delete one before adding another.",
                "danger",
            )
            return redirect(url_for("index"))

        # Without a price the alert direction cannot be determined, and the old
        # default of "above" fired an unwanted call immediately.
        if current_btc_price is None:
            flash(
                "The Bitcoin price feed has not delivered a price yet. "
                "Please try again in a few seconds.",
                "warning",
            )
            return redirect(url_for("add_alert"))

        mode = request.form.get("mode", "absolute")
        percent_change = None
        base_price = None

        if mode == "percent":
            try:
                percent_change = float(request.form.get("percent_change", ""))
            except ValueError:
                flash("Invalid percent change. Please enter a numeric value.", "danger")
                return redirect(url_for("add_alert"))
            if not math.isfinite(percent_change) or not (-99.0 <= percent_change <= 1000.0):
                flash("Percent change must be between -99 and 1000.", "danger")
                return redirect(url_for("add_alert"))
            if percent_change == 0:
                flash("Percent change must not be zero.", "danger")
                return redirect(url_for("add_alert"))
            base_price = current_btc_price
            price_threshold = round(base_price * (1 + percent_change / 100.0), 2)
        else:
            price_threshold = request.form.get("price_threshold")

        price_threshold, error = validate_threshold(price_threshold)
        if error:
            flash(error, "danger")
            return redirect(url_for("add_alert"))

        notify_channel = request.form.get("notify_channel", "call")
        if notify_channel not in VALID_CHANNELS:
            notify_channel = "call"
        repeat = request.form.get("repeat") == "on"

        # Determine alert direction from the live price.
        alert_type = "above" if price_threshold > current_btc_price else "below"

        new_alert = Alert(
            price_threshold=price_threshold,
            alert_type=alert_type,
            user_id=current_user.id,
            repeat=repeat,
            armed=True,
            notify_channel=notify_channel,
            percent_change=percent_change,
            base_price=base_price,
        )
        db.session.add(new_alert)
        db.session.commit()
        flash("Alert added successfully!", "success")
        return redirect(url_for("index"))

    active_count = Alert.query.filter_by(
        user_id=current_user.id, triggered=False
    ).count()
    return render_template(
        "add_alert.html",
        current_btc_price=current_btc_price,
        active_count=active_count,
        max_alerts=max_alerts,
    )


@app.route("/delete_alert/<int:alert_id>", methods=["POST"])
@login_required
def delete_alert(alert_id):
    # Find the alert by ID
    alert = db.get_or_404(Alert, alert_id)

    # Ensure the current user owns this alert
    if alert.user_id != current_user.id:
        flash("You are not authorized to delete this alert.", "danger")
        return redirect(url_for("index"))

    # Delete the alert from the database
    db.session.delete(alert)
    db.session.commit()

    flash("Alert deleted successfully.", "success")
    return redirect(url_for("index"))


@app.route("/register", methods=["GET", "POST"])
@limiter.limit(lambda: app.config["REGISTER_RATE_LIMIT"], methods=["POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        if not username or not email or not password:
            flash("Username, email and password are required.", "danger")
            return redirect(url_for("register"))

        # The phone number is whatever the registrant types and is never verified,
        # so at minimum it has to be a plausible E.164 number.
        phone_number, error = normalize_phone_number(request.form.get("phone_number"))
        if error:
            flash(error, "danger")
            return redirect(url_for("register"))

        # Check if username or email already exists
        if User.query.filter((User.username == username) | (User.email == email)).first():
            flash("Username or email already exists.", "danger")
            return redirect(url_for("register"))

        new_user = User(username=username, email=email, phone_number=phone_number)
        new_user.set_password(password)
        db.session.add(new_user)
        db.session.commit()

        flash("Registration successful. Please log in.", "success")
        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
@limiter.limit(lambda: app.config["LOGIN_RATE_LIMIT"], methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()

        # If user not found or password check fails, show an error
        if user is None or not user.check_password(password):
            flash("Invalid username or password.", "danger")
            return redirect(url_for("login"))

        # Check if "remember" checkbox was selected
        remember_me = request.form.get("remember") == "on"

        # Log the user in, passing the "remember" value
        login_user(user, remember=remember_me)
        flash("Logged in successfully.", "success")
        return redirect(url_for("index"))

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Logged out successfully.", "success")
    return redirect(url_for("login"))


# The price feed used to start only under `python app.py`, so under gunicorn the
# UI worked and alerts never fired. It now starts at import time, guarded by
# RUN_PRICE_FEED and by a single-owner lock (see acquire_price_feed_lock).
start_price_feed()


if __name__ == "__main__":
    print("Starting Flask app with Socket.IO...")
    # use_reloader=False avoids starting the app (and the feed) twice.
    socketio.run(app, debug=False, use_reloader=False)
