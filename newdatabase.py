import sqlite3

# Define the path to the new database
USER_DB_PATH = "users.db"

def create_users_db():
    """Create the users.db database and the users table."""
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
    print("users.db database and users table created successfully!")

if __name__ == "__main__":
    create_users_db()
