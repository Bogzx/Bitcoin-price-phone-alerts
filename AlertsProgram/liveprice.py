# liveprice.py

import websocket
import json

# Global variable to store the live price
live_price = None
# Binance Futures WebSocket URL for BTC/USDT perpetual contract
socket = "wss://fstream.binance.com/ws/btcusdt@trade"

# Callback function to handle WebSocket messages
def on_message(ws, message):
    global live_price  # Use the global variable
    data = json.loads(message)
    live_price = float(data['p'])  # Store the price as a float
    #print(f"Current BTC/USDT price: {live_price}")  # Optional: print the price

# Callback function when WebSocket connection is opened
def on_open(ws):
    print("Connection opened")

# Callback function when WebSocket connection is closed
def on_close(ws):
    print("Connection closed")

# Function to start the WebSocket connection
def start_websocket(threads_running_event):
    while threads_running_event.is_set():  # Check the event’s state
        ws = websocket.WebSocketApp(socket, on_message=on_message, on_open=on_open, on_close=on_close)
        ws.run_forever()

# Optionally, you could also define a function to get the live price
def get_live_price():
    return live_price
