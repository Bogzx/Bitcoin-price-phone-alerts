from app import socketio
socketio.emit('price_update', {'price': 12345.67})
