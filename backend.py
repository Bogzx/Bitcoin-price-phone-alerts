import sys
import os
import sqlite3
from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
from AlertsProgram.liveprice import get_live_price
from AlertsProgram.database import insert_alert, delete_alert, get_alerts, create_db
from AlertsProgram.main import *
import bcrypt
import jwt
import datetime

# Add the parent directory to the Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Initialize Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = 'your_secret_key'  # Use a secure and complex secret key for production

# Enable Cross-Origin Resource Sharing (CORS)
CORS(app)

# Path to the new users database
USER_DB_PATH = "users.db"

def create_users_table():
    """Create the users table in the users.db database if it doesn't exist."""
    conn = sqlite3.connect(USER_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

# Create the users table in the new users.db
create_users_table()

# User registration route
@app.route('/register', methods=['POST'])
def register():
    data = request.json
    username = data.get('username')
    password = data.get('password')

    if not username or not password:
        return jsonify({'message': 'Username and password are required'}), 400

    hashed_password = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())

    try:
        conn = sqlite3.connect(USER_DB_PATH)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, hashed_password))
        conn.commit()
        conn.close()
    except sqlite3.IntegrityError:
        return jsonify({'message': 'User already exists'}), 400

    return jsonify({'message': 'User registered successfully'}), 201

# User login route
@app.route('/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username')
    password = data.get('password')

    if not username or not password:
        return jsonify({'message': 'Username and password are required'}), 400

    conn = sqlite3.connect(USER_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT password FROM users WHERE username = ?", (username,))
    result = cursor.fetchone()
    conn.close()

    # Correct comparison without unnecessary .encode()
    if not result or not bcrypt.checkpw(password.encode('utf-8'), result[0]):
        return jsonify({'message': 'Invalid username or password'}), 401

    # Generate a JSON Web Token (JWT)
    token = jwt.encode(
        {'username': username, 'exp': datetime.datetime.utcnow() + datetime.timedelta(hours=1)},
        app.config['SECRET_KEY'],
        algorithm='HS256'
    )
    return jsonify({'token': token})

# Fetch all alerts
@app.route('/alerts', methods=['GET'])
def get_all_alerts():
    active_alerts = get_alerts()
    return jsonify(active_alerts)

# Add a new alert
@app.route('/alerts', methods=['POST'])
def add_alert():
    data = request.json

    if not all(k in data for k in ("phone", "threshold", "type", "message")):
        return jsonify({'message': 'Missing required alert fields'}), 400

    new_alert = {
        "phone": data.get("phone"),
        "threshold": data.get("threshold"),
        "type": data.get("type"),
        "message": data.get("message"),
        "active": True
    }
    insert_alert(new_alert)
    return jsonify({'message': 'Alert added successfully'}), 201

# Delete an alert by its ID
@app.route('/alerts/<int:alert_id>', methods=['DELETE'])
def delete_alert_by_id(alert_id):
    delete_alert(alert_id)
    return jsonify({'message': f'Alert ID {alert_id} deleted'}), 200

# Fetch the live price
@app.route('/price', methods=['GET'])
def live_price():
    main()
    price = get_live_price()
    if price is None:
        return jsonify({'message': 'Price not available'}), 503
    return jsonify({'price': price})

# Serve the frontend
@app.route('/')
def index():
    return render_template('proto_frontend.html')

if __name__ == '__main__':
    # Ensure the alerts database is created before starting the server
    create_db()
    app.run(debug=True)
