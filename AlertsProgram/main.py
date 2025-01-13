from AlertsProgram.liveprice import start_websocket
from AlertsProgram.database import insert_alert, delete_alert, get_alerts  # Import database functions
from AlertsProgram.check_alerts import check_alerts
from AlertsProgram.user_interface import handle_user_input
import threading
import logging
def main():
    # Configure logging
    logging.basicConfig(
        filename='alerts.log',
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )

    logging.info("Starting the alert system...")

    # Use an Event to control the running state
    threads_running = threading.Event()
    threads_running.set()  # Set the event initially to True


    # Start the WebSocket in a separate thread
    def run_websocket():
        logging.info("WebSocket thread starting...")
        start_websocket(threads_running)  # Pass the Event to manage thread state


    # Create and start the WebSocket thread
    ws_thread = threading.Thread(target=run_websocket, daemon=True)
    ws_thread.start()

    # Start the alert checking thread
    alert_thread = threading.Thread(target=check_alerts,args=(threads_running,), daemon=True)  # Set as a daemon thread
    alert_thread.start()


    handle_user_input(threads_running)

    # Wait for threads to finish before exiting
    logging.info("Waiting for threads to finish...")
    # No need to join daemon threads; they will exit when the main program exits.
    print("Program has terminated.")

if __name__ == '__main__':
    main()