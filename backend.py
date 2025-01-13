from flask import Flask, request, jsonify
from AlertsProgram.liveprice import get_live_price
from AlertsProgram.database import insert_alert, delete_alert, get_alerts, create_db
import bcrypt
import jwt
import datetime

# Initialize Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = 'your_secret_key'  # Use a secure secret key

# In-memory user storage for prototype (use database in production)
users = {}

# Authentication routes
@app.route('/register', methods=['POST'])
def register():
    data = request.json
    username = data.get('username')
    password = data.get('password')

    if username in users:
        return jsonify({'message': 'User already exists'}), 400

    hashed_password = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
    users[username] = hashed_password

    return jsonify({'message': 'User registered successfully'})

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username')
    password = data.get('password')

    if username not in users or not bcrypt.checkpw(password.encode('utf-8'), users[username]):
        return jsonify({'message': 'Invalid username or password'}), 401

    token = jwt.encode({'username': username, 'exp': datetime.datetime.utcnow() + datetime.timedelta(hours=1)},
                       app.config['SECRET_KEY'], algorithm='HS256')
    return jsonify({'token': token})

# Alerts management
@app.route('/alerts', methods=['GET'])
def get_all_alerts():
    active_alerts = get_alerts()
    return jsonify(active_alerts)

@app.route('/alerts', methods=['POST'])
def add_alert():
    data = request.json
    new_alert = {
        "phone": data.get("phone"),
        "threshold": data.get("threshold"),
        "type": data.get("type"),
        "message": data.get("message"),
        "active": True
    }
    insert_alert(new_alert)
    return jsonify({'message': 'Alert added successfully'})

@app.route('/alerts/<int:alert_id>', methods=['DELETE'])
def delete_alert_by_id(alert_id):
    delete_alert(alert_id)
    return jsonify({'message': f'Alert ID {alert_id} deleted'})

# Live price endpoint
@app.route('/price', methods=['GET'])
def live_price():
    price = get_live_price()
    if price is None:
        return jsonify({'message': 'Price not available'}), 503
    return jsonify({'price': price})

if __name__ == '__main__':
    create_db()  # Ensure database is created
    app.run(debug=True)
