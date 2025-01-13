import sqlite3
from typing import List, Dict, Any

def create_db(db_name: str = 'alerts.db') -> None:
    """Create the alerts table in the specified SQLite database."""
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()
    create_table_query = '''
    CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY,
        phone TEXT NOT NULL,
        threshold REAL NOT NULL,
        type TEXT CHECK(type IN ('low', 'high')) NOT NULL,
        message TEXT,
        active INTEGER CHECK(active IN (0, 1)) NOT NULL
    );
    '''
    cursor.execute(create_table_query)
    conn.commit()
    conn.close()

def insert_alert(alert: Dict[str, Any], db_name: str = 'alerts.db') -> None:
    """Insert a new alert into the alerts table."""
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()
    insert_query = '''
    INSERT INTO alerts (phone, threshold, type, message, active)
    VALUES (?, ?, ?, ?, ?);
    '''
    cursor.execute(insert_query, (
        alert['phone'],
        alert['threshold'],
        alert['type'],
        alert['message'],
        1 if alert['active'] else 0
    ))
    conn.commit()
    conn.close()

def update_alert(alert_id: int, updated_fields: Dict[str, Any], db_name: str = 'alerts.db') -> None:
    """Update an existing alert in the alerts table."""
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()
    set_clause = ', '.join([f"{key} = ?" for key in updated_fields.keys()])
    update_query = f'''
    UPDATE alerts
    SET {set_clause}
    WHERE id = ?;
    '''
    cursor.execute(update_query, (*updated_fields.values(), alert_id))
    conn.commit()
    conn.close()

def get_alerts(active_only: bool = True, db_name: str = 'alerts.db') -> List[Dict[str, Any]]:
    """Retrieve alerts from the alerts table."""
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()
    if active_only:
        select_query = 'SELECT * FROM alerts WHERE active = 1;'
    else:
        select_query = 'SELECT * FROM alerts;'
    cursor.execute(select_query)
    rows = cursor.fetchall()
    conn.close()
    alerts = []
    for row in rows:
        alerts.append({
            'id': row[0],
            'phone': row[1],
            'threshold': row[2],
            'type': row[3],
            'message': row[4],
            'active': bool(row[5])
        })
    return alerts

def delete_alert(alert_id: int, db_name: str = 'alerts.db') -> None:
    """Delete an alert from the alerts table."""
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()
    delete_query = 'DELETE FROM alerts WHERE id = ?;'
    cursor.execute(delete_query, (alert_id,))
    conn.commit()
    conn.close()

if __name__=="__main__":
    create_db()