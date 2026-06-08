import os
import calendar
import logging
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK")
TZ              = ZoneInfo("Europe/Tallinn")

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

def run():
    now = datetime.now(TZ)
    logging.info("Running at %s Tallinn, day %s.", now.strftime("%H:%M"), now.day)
    if not is_last_day_of_month(now):
        logging.info("Day %s — not last day of month, skipping.", now.day)
        return
    if not (now.hour == 10 and now.minute == 45):
        logging.info("Time %s — not 10:45 Tallinn, skipping.", now.strftime("%H:%M"))
        return
    send_all("💸 Reserve funds for Group B")
    logging.info("Reserve alert sent.")

if __name__ == "__main__":
    run()
