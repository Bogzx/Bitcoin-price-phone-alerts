import os
import json
import time
from threading import Thread

from flask import Flask, render_template, request, redirect, url_for, flash
from flask_socketio import SocketIO, emit
from flask_login import LoginManager, current_user, login_user, logout_user, login_required
from twilio.rest import Client
import websocket

# Initialize Flask app and load configuration
app = Flask(__name__)
app.config.from_object("config.Config")

# Initialize SocketIO for live updates
socketio = SocketIO(app)

# Initialize Flask-Login
login_manager = LoginManager(app)
login_manager.login_view = "login"

# Import database and models
from models import db, Alert, User

# Bind SQLAlchemy to the app and create tables if needed
db.init_app(app)
with app.app_context():
    db.create_all()

# Setup Twilio client
twilio_client = Client(app.config["TWILIO_ACCOUNT_SID"], app.config["TWILIO_AUTH_TOKEN"])
twilio_phone_number = app.config["TWILIO_PHONE_NUMBER"]

# Global variable for the current BTC price
current_btc_price = None

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

def call_user(phone_number):
    """Uses Twilio to call the given phone number."""
    try:
        call = twilio_client.calls.create(
            to=phone_number,
            from_=twilio_phone_number,
            url="http://demo.twilio.com/docs/voice.xml"
        )
        app.logger.info(f"Call initiated for {phone_number}, SID: {call.sid}")
    except Exception as e:
        app.logger.error(f"Error initiating call for {phone_number}: {e}")

def on_message(ws, message):
    """Handles messages from Binance's WebSocket."""
    global current_btc_price
    try:
        data = json.loads(message)
        current_price = float(data.get("p", 0))
        current_btc_price = current_price
        app.logger.info(f"Current BTC Price: {current_price}")

        # Emit the updated BTC price to connected clients
        socketio.emit('price_update', {'price': current_price})

        with app.app_context():
            # Check active alerts for all users
            alerts = Alert.query.filter_by(triggered=False).all()
            for alert in alerts:
                if (alert.alert_type == "above" and current_price >= alert.price_threshold) or \
                   (alert.alert_type == "below" and current_price <= alert.price_threshold):
                    app.logger.info(f"Triggering alert for user {alert.user.username}: BTC {alert.alert_type} {alert.price_threshold}")
                    # Use the user's stored phone number
                    call_user(alert.user.phone_number)
                    alert.triggered = True
                    db.session.commit()
                    socketio.emit("alert_triggered", {
                        "alert_id": alert.id,
                        "price_threshold": alert.price_threshold,
                        "alert_type": alert.alert_type,
                        "user_id": alert.user_id
                    })
    except Exception as e:
        app.logger.error(f"Error in on_message: {e}")

def on_error(ws, error):
    app.logger.error(f"WebSocket error: {error}")

def on_close(ws, close_status_code, close_msg):
    app.logger.info("WebSocket connection closed. Reconnecting in 5 seconds...")
    time.sleep(5)
    start_binance_ws()

def on_open(ws):
    app.logger.info("WebSocket connection established.")

def start_binance_ws():
    """Starts the Binance WebSocket for live BTC trade data."""
    websocket.enableTrace(False)
    ws_url = "wss://stream.binance.com:9443/ws/btcusdt@trade"
    ws = websocket.WebSocketApp(
        ws_url,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    ws.on_open = on_open
    ws.run_forever()

def start_ws_thread():
    """Starts the Binance WebSocket listener in a background thread."""
    ws_thread = Thread(target=start_binance_ws)
    ws_thread.daemon = True
    ws_thread.start()

@app.route("/")
@login_required
def index():
    # Show only alerts belonging to the logged-in user
    active_alerts = Alert.query.filter_by(user_id=current_user.id, triggered=False).all()
    triggered_alerts = Alert.query.filter_by(user_id=current_user.id, triggered=True).all()
    return render_template("index.html", 
                           active_alerts=active_alerts, 
                           triggered_alerts=triggered_alerts,
                           current_btc_price=current_btc_price)

@app.route("/add_alert", methods=["GET", "POST"])
@login_required
def add_alert():
    if request.method == "POST":
        try:
            price_threshold = float(request.form["price_threshold"])
        except ValueError:
            flash("Invalid price threshold. Please enter a numeric value.", "danger")
            return redirect(url_for("add_alert"))

        # Automatically determine alert type based on current BTC price
        if current_btc_price is not None:
            alert_type = "above" if price_threshold > current_btc_price else "below"
        else:
            alert_type = "above"

        new_alert = Alert(
            price_threshold=price_threshold,
            alert_type=alert_type,
            user_id=current_user.id
        )
        db.session.add(new_alert)
        db.session.commit()
        flash("Alert added successfully!", "success")
        return redirect(url_for("index"))
    return render_template("add_alert.html", current_btc_price=current_btc_price)

@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        username = request.form["username"]
        email = request.form["email"]
        phone_number = request.form["phone_number"]  # Capture user's phone number
        password = request.form["password"]
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
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        user = User.query.filter_by(username=username).first()
        if user is None or not user.check_password(password):
            flash("Invalid username or password.", "danger")
            return redirect(url_for("login"))
        login_user(user)
        flash("Logged in successfully.", "success")
        return redirect(url_for("index"))
    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Logged out successfully.", "success")
    return redirect(url_for("login"))

if __name__ == "__main__":
    start_ws_thread()
    socketio.run(app, debug=True)
