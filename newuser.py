import sqlite3
import bcrypt

# Define the path to the users database
USER_DB_PATH = "users.db"

def quick_create_user(username, password):
    """Create a new user in the users.db database."""
    hashed_password = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())

    conn = sqlite3.connect(USER_DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, hashed_password))
        conn.commit()
        print(f"User '{username}' created successfully!")
    except sqlite3.IntegrityError:
        print(f"User '{username}' already exists.")
    finally:
        conn.close()

# Example usage
if __name__ == "__main__":
    quick_create_user("admin", "adminpassword")
