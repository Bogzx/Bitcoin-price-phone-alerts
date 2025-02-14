import os
from dotenv import load_dotenv

# Load environment variables from a .env file if available
load_dotenv()

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "your_random_generated_secret_key")
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", "sqlite:///alerts.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Twilio configuration (set these in your environment or .env file)
    TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
    TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
    TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
