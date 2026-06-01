import os
import json
import base64
import logging
import calendar
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

AUTH_JSON       = os.environ.get("SCRAMBLE_AUTH", "{}")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK")
GROUP_B_URL     = "https://investor.scrambleup.com/investing"
API_URL         = "https://investor.scrambleup.com/api/investors/invested_in_groups_stats/"
TZ              = ZoneInfo("Europe/Vilnius")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

def send_all(message: str) -> None:
    logging.info(message)
    if not DISCORD_WEBHOOK:
        logging.warning("No DISCORD_WEBHOOK configured.")
        return
    try:
        requests.post(DISCORD_WEBHOOK, json={"content": message}, timeout=10)
    except Exception as e:
        logging.error("Failed to send Discord: %s", e)

def is_last_day_of_month(now: datetime) -> bool:
    return now.day == calendar.monthrange(now.year, now.month)[1]

def should_run_now(now: datetime) -> bool:
    h, m, d = now.hour, now.minute, now.day
    if h >= 22 or h < 7:
        logging.info("Night skip: %s Vilnius.", now.strftime("%H:%M"))
        return False
    if is_last_day_of_month(now):
        if h == 18 and m == 0:
            return True
        logging.info("Last day of month — slot not allowed at %s.", now.strftime("%H:%M"))
        return False
    if 5 <= d <= 10:
        return True
    logging.info("Day %s — not in active schedule, skipping.", d)
    return False

def get_access_token(auth: dict) -> str | None:
    token = auth.get("access_token")
    if not token:
        return None
    try:
        payload_b64 = token.split(".")[1]
        padding = 4 - len(payload_b64) % 4
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * (padding % 4)))
        exp = payload.get("exp", 0)
        now = int(datetime.now(TZ).timestamp())
        remaining_h = (exp - now) / 3600
        age_h = (now - payload.get("iat", now)) / 3600
        logging.info("Token age=%.1fh, expires in %.1fh", age_h, remaining_h)
        if exp <= now:
            logging.warning("Token is expired.")
            return None
        return token
    except Exception as e:
        logging.warning("Could not validate token: %s", e)
        return None

def fetch_groups(access_token: str) -> list | None:
    headers = {
        "Authorization": f"Token {access_token}",
        "X-Api-Version": "3",
        "Accept": "application/json",
        "Referer": "https://investor.scrambleup.com/investing",
    }
    try:
        r = requests.get(API_URL, headers=headers, timeout=15)
        logging.info("API response: %d", r.status_code)
        if r.status_code == 200:
            return r.json()
        logging.warning("API returned %d: %s", r.status_code, r.text[:200])
        return None
    except Exception as e:
        logging.error("API call failed: %s", e)
        return None

def parse_groups(data: list) -> tuple:
    logging.info("AP
