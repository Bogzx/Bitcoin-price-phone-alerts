"""Validation of user input that ends up dialled or compared against prices."""

import math
import re

from flask import current_app

PHONE_RE = re.compile(r"^\+[1-9]\d{7,14}$")
VALID_CHANNELS = ("call", "sms", "both")
# An account controls phone calls billed to the owner; "a" is not a password.
MIN_PASSWORD_LENGTH = 8


def normalize_phone_number(raw):
    """Returns (phone, error). Requires E.164, e.g. +14155550123."""
    phone = re.sub(r"[\s\-().]", "", raw or "")
    if not PHONE_RE.match(phone):
        return None, "Enter your phone number in international format, e.g. +14155550123."
    allowed = current_app.config["ALLOWED_PHONE_COUNTRY_CODES"]
    if allowed and not any(phone[1:].startswith(code) for code in allowed):
        return None, "That country calling code is not accepted by this deployment."
    return phone, None


def validate_threshold(value):
    """Returns (threshold, error) for a user supplied price threshold."""
    try:
        threshold = float(value)
    except (TypeError, ValueError):
        return None, "Invalid price threshold. Please enter a numeric value."
    if not math.isfinite(threshold):
        return None, "Price threshold must be a finite number."
    low = current_app.config["MIN_PRICE_THRESHOLD"]
    high = current_app.config["MAX_PRICE_THRESHOLD"]
    if not (low <= threshold <= high):
        return None, f"Price threshold must be between {low} and {high:,.0f}."
    return threshold, None
