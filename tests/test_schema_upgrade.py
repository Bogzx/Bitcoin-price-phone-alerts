"""A database created by the April 2025 version must keep working."""

import sqlite3

from sqlalchemy import create_engine

from btc_alerts.models import add_missing_columns

OLD_SCHEMA = """
CREATE TABLE user (
    id INTEGER PRIMARY KEY,
    username VARCHAR(64) NOT NULL UNIQUE,
    email VARCHAR(120) NOT NULL UNIQUE,
    phone_number VARCHAR(20) NOT NULL,
    password_hash VARCHAR(128) NOT NULL
);
CREATE TABLE alert (
    id INTEGER PRIMARY KEY,
    price_threshold FLOAT NOT NULL,
    alert_type VARCHAR(10) NOT NULL,
    triggered BOOLEAN,
    user_id INTEGER NOT NULL REFERENCES user(id)
);
INSERT INTO user VALUES (1, 'old', 'old@example.com', '+14155550123', 'x');
INSERT INTO alert VALUES (1, 70000.0, 'above', 0, 1);
"""


def test_old_database_gains_new_columns_with_defaults(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.close()

    engine = create_engine(f"sqlite:///{path}")
    added = add_missing_columns(engine)

    assert "alert.repeat" in added and "user.last_notified_at" in added
    assert "user.phone_verified_at" in added  # existing users start unverified
    assert add_missing_columns(engine) == []  # idempotent

    conn = sqlite3.connect(path)
    row = conn.execute(
        "SELECT repeat, armed, notify_channel, notify_error, price_threshold FROM alert"
    ).fetchone()
    conn.close()
    assert row == (0, 1, "call", None, 70000.0)


def test_non_sqlite_engines_are_left_alone():
    class FakeEngine:
        class dialect:
            name = "postgresql"

    assert add_missing_columns(FakeEngine()) == []
