from liveprice import get_live_price
import logging
from database import insert_alert,delete_alert,get_alerts

def handle_user_input(threads_running):
    while threads_running.is_set():  # Check if the thread should keep running
        print("\n1: Add Alert\n2: Remove Alert\n3: Show Active Alerts\n0: Exit")
        try:
            choice = input("Enter your choice: ")
            logging.info(f"User input choice: {choice}")

            if choice == "1":
                current_price = get_live_price()  # Get the current live price
                if current_price is None:
                    logging.warning("Current price not available. User prompted to try again later.")
                    print("Current price not available. Please try again later.")
                    continue

                # Prompt for user input for alert details
                test = input("Bogdan's phone number? (1/0): ")
                phone = "+40735810974" if test == "1" else input("Enter phone number: ")
                price = float(input("Enter price threshold: "))
                alert_type = "high" if current_price < price else "low"
                message = input("Enter custom message: ")

                # Construct the alert dictionary
                new_alert = {
                    "phone": phone,
                    "threshold": price,
                    "type": alert_type,
                    "message": message,
                    "active": True
                }

                # Insert the alert into the database and retrieve the ID
                new_id = insert_alert(new_alert)
                
                logging.info(f"Alert added: ID {new_id}, Type: {alert_type}, Threshold: {price}, Phone: {phone}")
                print(f"Alert added: {alert_type} alert set for {price}.")

            elif choice == "2":
                alert_id = int(input("Enter alert ID to remove: "))
                # Remove alert from the database
                delete_alert(alert_id)
                logging.info(f"Alert ID {alert_id} removed.")
                print(f"Alert ID {alert_id} removed.")

            elif choice == "3":
                logging.info("User requested to view active alerts.")
                print("Active Alerts:")
                # Retrieve active alerts from the database
                active_alerts = get_alerts(active_only=True)
                for alert in active_alerts:
                    print(f"ID: {alert['id']}, Phone: {alert['phone']}, Threshold: {alert['threshold']}, Type: {alert['type']}, Message: {alert['message']}")

            elif choice == "0":
                logging.info("User opted to exit. Stopping threads...")
                print("Exiting...")
                threads_running.clear()  # Signal threads to stop
                break  # Exit the user input loop

            else:
                logging.warning(f"Invalid choice entered by user: {choice}")
                print("Invalid choice. Please try again.")

        except Exception as e:
            logging.warning(f"An error has occurred: {e}")
            print("An error has occurred. Please try again.")