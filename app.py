"""Entry point: `gunicorn --workers 1 --threads 50 app:app`, or `python app.py`.

The application lives in the `btc_alerts` package. This module loads `.env`,
builds the app and starts the Binance feed and notification worker in the
serving process. Importing `btc_alerts` alone starts nothing.
"""

from dotenv import load_dotenv

load_dotenv()

from btc_alerts import create_app, start_background_services  # noqa: E402
from btc_alerts.extensions import socketio  # noqa: E402

app = create_app()
start_background_services(app)


if __name__ == "__main__":
    print("Starting Flask app with Socket.IO...")
    # use_reloader=False avoids starting the app (and the feed) twice.
    socketio.run(app, debug=False, use_reloader=False)
