"""Flask extensions, created unbound and attached to the app in create_app()."""

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager
from flask_socketio import SocketIO
from flask_wtf.csrf import CSRFProtect

# CSRF protection for every POST form.
csrf = CSRFProtect()

# Live price and alert events for the dashboard. Origins come from
# CORS_ALLOWED_ORIGINS when the app is created.
socketio = SocketIO()

# Brute-force brake on the auth routes. In-memory storage is fine for a
# single-process deployment, which is the only supported topology (see README).
limiter = Limiter(get_remote_address, default_limits=[], storage_uri="memory://")

login_manager = LoginManager()
login_manager.login_view = "auth.login"
