"""Measures the cost of one trade tick against N armed alerts, none of them due.

    python scripts/bench_ticks.py

Compares the quiet-band path (the default) with a full database evaluation on
every tick (ALERT_FULL_SCAN_SECONDS=0). Uses a temporary SQLite file and dry-run
notifications; touches no network.
"""

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btc_alerts import create_app, get_services  # noqa: E402
from btc_alerts.models import Alert, User, db  # noqa: E402

TICKS = 500


def measure(n_alerts, full_scan_seconds):
    with tempfile.TemporaryDirectory() as tmp:
        app = create_app({
            "SECRET_KEY": "bench",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp}/bench.db",
            "NOTIFY_DRY_RUN": True,
            "ALERT_FULL_SCAN_SECONDS": full_scan_seconds,
            "LOG_LEVEL": "ERROR",
        })
        feed = get_services(app).feed
        with app.app_context():
            for u in range(max(n_alerts // 5, 1)):
                user = User(username=f"u{u}", email=f"u{u}@example.com",
                            phone_number="+14155550123", password_hash="x")
                db.session.add(user)
                db.session.flush()
                for k in range(min(5, n_alerts)):
                    db.session.add(Alert(price_threshold=200_000 + k, alert_type="above",
                                         user_id=user.id))
            db.session.commit()
            started = time.perf_counter()
            for i in range(TICKS):
                feed.on_message(None, json.dumps({"p": str(60_000 + i * 0.01)}))
            elapsed = time.perf_counter() - started
            db.session.remove()
        return elapsed / TICKS * 1000


if __name__ == "__main__":
    print(f"{'alerts':>7} {'full scan every tick':>22} {'quiet band':>12}")
    for n in (5, 100, 1000):
        print(f"{n:>7} {measure(n, 0):>19.3f} ms {measure(n, 5.0):>9.3f} ms")
