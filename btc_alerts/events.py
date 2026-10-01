"""Socket.IO handlers. Live updates are per user, never broadcast."""

from flask_login import current_user
from flask_socketio import emit, join_room

from .extensions import socketio
from .services import get_services


@socketio.on("connect")
def handle_connect(auth=None):
    """Rejects anonymous sockets and puts each user in their own room."""
    if not current_user.is_authenticated:
        return False
    join_room(f"user_{current_user.id}")
    price = get_services().feed.price
    if price is not None:
        emit("price_update", {"price": price})
    return True
