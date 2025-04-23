# 📈 Bitcoin Price Alert Service

![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)
![Python 3.8+](https://img.shields.io/badge/Python-3.8+-blue.svg)
![Flask](https://img.shields.io/badge/Flask-2.2.2-red.svg)

A real-time Bitcoin price monitoring service that calls your phone when BTC crosses your specified price thresholds.

![Bitcoin Price Alert Dashboard](https://raw.githubusercontent.com/Bogzx/PhoneAlertsV2/67176fedfa9606fe52ddaa1d9fa27b076e6659ff/Screenshot%202025-04-23%20173519.png?token=GHSAT0AAAAAADCP2S2Y2XIIBOYX7HTQ4LJK2AJBP7Q)

## ✨ Features

- **Real-time Bitcoin Price Tracking** via Binance WebSocket API
- **Customizable Price Alerts** for prices going above or below your thresholds
- **Phone Call Notifications** via Twilio when price alerts trigger
- **User Authentication** with secure login system
- **WebSocket Updates** for live UI updates without page refreshes

## 🚀 Installation

### Prerequisites

- Python 3.8 or higher
- Twilio account for phone notifications

### Step-by-Step Setup

1. **Clone the repository**

   ```bash
   git clone https://github.com/yourusername/btc-price-alert.git
   cd btc-price-alert
   ```

2. **Create and activate a virtual environment**

   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

4. **Set up environment variables**

   Create a `.env` file in the project root:

   ```
   SECRET_KEY=your_secure_random_key
   DATABASE_URL=sqlite:///alerts.db
   TWILIO_ACCOUNT_SID=your_twilio_sid
   TWILIO_AUTH_TOKEN=your_twilio_auth_token
   TWILIO_PHONE_NUMBER=your_twilio_phone_number
   ```

## 🖥️ Usage

1. **Start the application**

   ```bash
   python app.py
   ```

2. **Access the web interface**

   Open your browser and navigate to: `http://localhost:5000`

3. **Register an account**

4. **Create price alerts**

   - Click "Add Alert" button
   - Enter your desired price threshold
   - The system automatically determines if it's an "above" or "below" alert based on current price

5. **Receive notifications**

   When Bitcoin crosses your set thresholds, you'll receive a phone call via Twilio.

## ⚙️ Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `SECRET_KEY` | Flask secret key for sessions | None (required) |
| `DATABASE_URL` | Database connection string | `sqlite:///alerts.db` |
| `TWILIO_ACCOUNT_SID` | Twilio Account SID | None (required) |
| `TWILIO_AUTH_TOKEN` | Twilio Auth Token | None (required) |
| `TWILIO_PHONE_NUMBER` | Twilio Phone Number | None (required) |

### Twilio Setup

1. Create a [Twilio account](https://www.twilio.com/try-twilio)
2. Get your Account SID and Auth Token from the dashboard
3. Purchase or use an existing Twilio phone number
4. Add these credentials to your `.env` file

## 🔍 How It Works

- Bitcoin prices are obtained from Binance's WebSocket API in real-time
- When a price threshold is crossed, the alert is triggered
- A Twilio call is placed to your registered phone number
- The UI is updated in real-time to show triggered alerts

## 📁 Project Structure

```
btc-price-alert/
├── app.py               # Main application entry point
├── config.py            # Configuration settings
├── models.py            # Database models
├── requirements.txt     # Python dependencies
├── .env.example         # Example environment variables
├── static/              # Static assets (CSS, JS)
└── templates/           # HTML templates
```

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## ❓ FAQ

### Is this service free to use?

The software is free and open-source. However, you will need your own Twilio account for phone call notifications, which may incur charges.

### How accurate are the price alerts?

The alerts are based on real-time data from Binance and typically trigger within seconds of the price crossing your threshold.

---

