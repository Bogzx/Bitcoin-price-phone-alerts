import sqlite3
import logging
from call import apeleaza
from liveprice import get_live_price
import time
def check_alerts(threads_running):
    # Open a persistent database connection for this function
    conn = sqlite3.connect('alerts.db')
    cursor = conn.cursor()

    while threads_running.is_set():  # Check if the thread should keep running
        current_price = get_live_price()
        if current_price is not None:
            logging.debug(f"Current live price: {current_price}")
            
            # Retrieve active alerts from the database
            cursor.execute('SELECT * FROM alerts WHERE active = 1')
            active_alerts = cursor.fetchall()
            
            for alert in active_alerts:
                alert_id, phone, threshold, alert_type, message, active = alert

                # Check if the alert should be triggered
                if (alert_type == "high" and current_price >= threshold) or \
                   (alert_type == "low" and current_price <= threshold and current_price != 0.0):
                    
                    logging.info(f"Triggering alert ID {alert_id} for phone {phone} with current price {current_price}")
                    # Trigger the alert (e.g., call the user)
                    apeleaza(phone, message)

                    # Remove (deactivate) the alert after triggering
                    cursor.execute('UPDATE alerts SET active = 0 WHERE id = ?', (alert_id,))
                    conn.commit()  # Commit the change to deactivate the alert
                    logging.info(f"Alert ID {alert_id} triggered and deactivated.")

        time.sleep(0.25)  # Wait before checking the price again

    # Close the database connection once the thread stops running
    conn.close()