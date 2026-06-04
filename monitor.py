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
    logging.info("API data: %s", json.dumps(data)[:500])
    group_a_pct = group_a_ctx = group_b_pct = group_b_ctx = None
    group_b_remaining = group_b_full = group_a_full = None
    for item in data:
        if not isinstance(item, dict):
            continue
        group = str(item.get("group", "")).lower()
        title = item.get("group_title", group)
        full = float(item.get("full_amount") or 0)
        remaining = float(item.get("remaining_amount") or 0)
        pct_val = round((full - remaining) / full * 100) if full > 0 else 0
        pct_str = f"{pct_val}%"
        if group == "moderate":
            group_b_pct, group_b_ctx = pct_str, str(title)
            group_b_remaining = remaining
            group_b_full = full
            logging.info("Group B: full=%.2f remaining=%.2f pct=%s", full, remaining, pct_str)
        elif group == "conservative":
            group_a_pct, group_a_ctx = pct_str, str(title)
            group_a_full = full
            logging.info("Group A: full=%.2f remaining=%.2f pct=%s", full, remaining, pct_str)
    return group_b_pct, group_b_ctx, group_a_pct, group_a_ctx, group_b_remaining, group_b_full, group_a_full

def check_slots():
    now = datetime.now(TZ)
    if is_last_day_of_month(now) and now.hour == 12:
        logging.info("Last day of month, 12:%02d Vilnius — sending reserve alert.", now.minute)
        send_all("⚠️ Scramble Group B Bot.\nRESERVE the Group B funds/slots")
    if not should_run_now(now):
        return
    logging.info("Running at %s Vilnius, day %s.", now.strftime("%H:%M"), now.day)
    try:
        auth = json.loads(AUTH_JSON)
    except Exception as e:
        send_all(f"⚠️ Could not parse SCRAMBLE_AUTH: {e}")
        return
    access_token = get_access_token(auth if isinstance(auth, dict) else {})
    if not access_token:
        send_all(f"🔐 Session expired ⚠️\n{GROUP_B_URL} ⬅️ Update token")
        return
    data = fetch_groups(access_token)
    if data is None:
        send_all(f"🔐 Session expired ⚠️\n{GROUP_B_URL} ⬅️ Update token")
        return
    group_b_pct, group_b_ctx, group_a_pct, group_a_ctx, group_b_remaining, group_b_full, group_a_full = parse_groups(data)
    if group_b_pct is None:
        send_all("⚠️ Could not parse group data. Check logs.")
        return
    try:
        pct_value = int(group_b_pct.replace("%", "").strip())
    except ValueError:
        send_all(f"⚠️ Unexpected format: '{group_b_pct}'")
        return
    pct_a_str = group_a_pct or "N/A"
    remaining_str = f"€{group_b_remaining:,.0f}" if group_b_remaining is not None else "N/A"
    b_target = f"€{group_b_full:,.0f}" if group_b_full else "N/A"
    a_target = f"€{group_a_full:,.0f}" if group_a_full else "N/A"
    logging.info("Group B: %s, Group A: %s", group_b_pct, group_a_pct)
    if pct_value == 0:
        logging.info("Group B 0%% — not open yet. No alert.")
    elif 0 < pct_value < 100:
        send_all(
            f"🙂 OPEN investment in Group B!\n"
            f"📈 Currently **{pct_value}%** filled ⚡\n"
            f"💸 {remaining_str} left from {b_target}\n"
            f"📊 Group A - {pct_a_str} filled\n"
            f"💵 Group A target: **{a_target}**\n"
            f"{GROUP_B_URL} ⬅️ Invest now\n"
        )
        logging.info("ALERT SENT — Group B is %d%% full.", pct_value)
    else:
        logging.info("Group B 100%% full. No alert.")

if __name__ == "__main__":
    check_slots()
