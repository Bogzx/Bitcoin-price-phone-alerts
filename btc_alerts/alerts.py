"""Dashboard, alert management and the health endpoint."""

import math

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from .extensions import limiter
from .models import Alert, NotificationLog, db
from .services import get_services
from .validation import VALID_CHANNELS, validate_threshold

bp = Blueprint("alerts", __name__)


@bp.route("/")
@login_required
def index():
    feed = get_services().feed
    # Show only alerts belonging to the logged-in user
    active_alerts = Alert.query.filter_by(user_id=current_user.id, triggered=False).all()
    triggered_alerts = Alert.query.filter_by(user_id=current_user.id, triggered=True).all()
    recent_notifications = (
        NotificationLog.query.filter_by(user_id=current_user.id)
        .order_by(NotificationLog.created_at.desc(), NotificationLog.id.desc())
        .limit(10)
        .all()
    )
    return render_template(
        "index.html",
        active_alerts=active_alerts,
        triggered_alerts=triggered_alerts,
        current_btc_price=feed.price,
        recent_notifications=recent_notifications,
        price_fresh=feed.is_fresh(),
        price_age=feed.status.age_seconds(),
        max_alerts=current_app.config["MAX_ACTIVE_ALERTS_PER_USER"],
    )


@bp.route("/healthz")
@limiter.exempt
def healthz():
    """Liveness of the alerting path, for an external uptime monitor.

    Returns 503 when this process has no recent price, i.e. alerts cannot fire.
    Point an uptime checker at it: a dead feed is otherwise invisible.
    """
    services = get_services()
    feed = services.feed
    age = feed.status.age_seconds()
    fresh = feed.is_fresh()
    body = {
        "status": "ok" if fresh else "stale",
        "feed_running": feed.status.running,
        "connected": feed.status.connected,
        "last_price_age_seconds": None if age is None else round(age, 1),
        "reconnects": feed.status.reconnects,
        "notifications_queued": services.notifier.queue.qsize(),
    }
    return jsonify(body), 200 if fresh else 503


@bp.route("/add_alert", methods=["GET", "POST"])
@login_required
def add_alert():
    feed = get_services().feed
    max_alerts = current_app.config["MAX_ACTIVE_ALERTS_PER_USER"]
    if request.method == "POST":
        # Hard cap on outstanding alerts: every alert is a billable phone call.
        active_count = Alert.query.filter_by(user_id=current_user.id, triggered=False).count()
        if active_count >= max_alerts:
            flash(
                f"You already have {active_count} active alerts (limit {max_alerts}). "
                "Delete one before adding another.",
                "danger",
            )
            return redirect(url_for("alerts.index"))

        # Without a price the alert direction cannot be determined, and the old
        # default of "above" fired an unwanted call immediately. A stale price is
        # just as bad: the direction is picked against where BTC used to be.
        current_price = feed.price
        if current_price is None or not feed.is_fresh():
            flash(
                "The Bitcoin price feed has no recent price. "
                "Please try again in a few seconds.",
                "warning",
            )
            return redirect(url_for("alerts.add_alert"))

        mode = request.form.get("mode", "absolute")
        percent_change = None
        base_price = None

        if mode == "percent":
            try:
                percent_change = float(request.form.get("percent_change", ""))
            except ValueError:
                flash("Invalid percent change. Please enter a numeric value.", "danger")
                return redirect(url_for("alerts.add_alert"))
            if not math.isfinite(percent_change) or not (-99.0 <= percent_change <= 1000.0):
                flash("Percent change must be between -99 and 1000.", "danger")
                return redirect(url_for("alerts.add_alert"))
            if percent_change == 0:
                flash("Percent change must not be zero.", "danger")
                return redirect(url_for("alerts.add_alert"))
            base_price = current_price
            price_threshold = round(base_price * (1 + percent_change / 100.0), 2)
        else:
            price_threshold = request.form.get("price_threshold")

        price_threshold, error = validate_threshold(price_threshold)
        if error:
            flash(error, "danger")
            return redirect(url_for("alerts.add_alert"))

        notify_channel = request.form.get("notify_channel", "call")
        if notify_channel not in VALID_CHANNELS:
            notify_channel = "call"
        repeat = request.form.get("repeat") == "on"

        # Determine alert direction from the live price. A threshold equal to
        # the live price would count as "below", be met already and call at once.
        if round(price_threshold, 2) == round(current_price, 2):
            flash(
                "That threshold equals the current price; pick a price above or below it.",
                "danger",
            )
            return redirect(url_for("alerts.add_alert"))
        alert_type = "above" if price_threshold > current_price else "below"

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
        return redirect(url_for("alerts.index"))

    active_count = Alert.query.filter_by(user_id=current_user.id, triggered=False).count()
    return render_template(
        "add_alert.html",
        current_btc_price=feed.price if feed.is_fresh() else None,
        active_count=active_count,
        max_alerts=max_alerts,
    )


@bp.route("/delete_alert/<int:alert_id>", methods=["POST"])
@login_required
def delete_alert(alert_id):
    alert = db.get_or_404(Alert, alert_id)
    if alert.user_id != current_user.id:
        flash("You are not authorized to delete this alert.", "danger")
        return redirect(url_for("alerts.index"))
    db.session.delete(alert)
    db.session.commit()
    flash("Alert deleted successfully.", "success")
    return redirect(url_for("alerts.index"))
