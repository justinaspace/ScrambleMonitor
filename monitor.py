import os
import json
import time
import random
import base64
import logging
import requests
from decimal import Decimal, ROUND_DOWN
from datetime import datetime
from zoneinfo import ZoneInfo

AUTH_JSON       = os.environ.get("SCRAMBLE_AUTH", "{}")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK")
GROUP_B_URL     = "https://investor.scrambleup.com/investing"
API_URL         = "https://investor.scrambleup.com/api/investors/invested_in_groups_stats/"
BALANCE_URL     = "https://investor.scrambleup.com/api/investors/dashboard/balance/"
STATE_FILE      = "state.json"
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

def should_run_now(now: datetime) -> bool:
    h, d = now.hour, now.day
    if h >= 22 or h < 7:
        logging.info("Night skip: %s Vilnius.", now.strftime("%H:%M"))
        return False
    if 5 <= d <= 10:
        return True
    logging.info("Day %s — not in active schedule (days 5-10 only), skipping.", d)
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
    for attempt in (1, 2):
        try:
            r = requests.get(API_URL, headers=headers, timeout=15)
            logging.info("API response: %d (attempt %d)", r.status_code, attempt)
            if r.status_code == 200:
                return r.json()
            logging.warning("API returned %d: %s", r.status_code, r.text[:200])
        except Exception as e:
            logging.error("API call failed (attempt %d): %s", attempt, e)
        if attempt == 1:
            logging.info("Retrying API call in 5s...")
            time.sleep(5)
    return None

def fetch_balance(access_token: str) -> float | None:
    headers = {
        "Authorization": f"Token {access_token}",
        "X-Api-Version": "3",
        "Accept": "application/json",
        "Referer": "https://investor.scrambleup.com/investing",
    }
    for attempt in (1, 2):
        try:
            r = requests.get(BALANCE_URL, headers=headers, timeout=15)
            logging.info("Balance API response: %d (attempt %d)", r.status_code, attempt)
            if r.status_code == 200:
                data = r.json()
                logging.info("Full balance response: %s", json.dumps(data))

                available = float(data.get("available") or 0)

                bonus = 0.0
                for key, value in data.items():
                    if "bonus" in key.lower():
                        try:
                            bonus += float(value or 0)
                            logging.info("Bonus field '%s' = %s", key, value)
                        except (TypeError, ValueError):
                            pass

                total = Decimal(str(available)) + Decimal(str(bonus))
                total = float(total.quantize(Decimal("0.01"), rounding=ROUND_DOWN))

                logging.info("Available=%.2f Bonus=%.2f Total=%.2f", available, bonus, total)
                return total
            logging.warning("Balance API returned %d: %s", r.status_code, r.text[:200])
        except Exception as e:
            logging.error("Balance API call failed (attempt %d): %s", attempt, e)
        if attempt == 1:
            logging.info("Retrying balance API call in 5s...")
            time.sleep(5)
    return None

def parse_groups(data: list) -> tuple:
    logging.info("API data: %s", json.dumps(data)[:500])
    group_a_pct = group_b_pct = None
    group_b_remaining = group_b_full = group_a_full = None
    for item in data:
        if not isinstance(item, dict):
            continue
        group = str(item.get("group", "")).lower()
        full = float(item.get("full_amount") or 0)
        remaining = float(item.get("remaining_amount") or 0)
        pct_val = round((full - remaining) / full * 100) if full > 0 else 0
        pct_str = f"{pct_val}%"
        if group == "moderate":
            group_b_pct = pct_str
            group_b_remaining = remaining
            group_b_full = full
            logging.info("Group B: full=%.2f remaining=%.2f pct=%s", full, remaining, pct_str)
        elif group == "conservative":
            group_a_pct = pct_str
            group_a_full = full
            logging.info("Group A: full=%.2f remaining=%.2f pct=%s", full, remaining, pct_str)
    return group_b_pct, group_a_pct, group_b_remaining, group_b_full, group_a_full

def load_state() -> dict:
    defaults = {"alerted_open": False, "auth_alert_sent": False}
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
        if isinstance(data, dict):
            defaults.update({k: v for k, v in data.items() if k in defaults})
    except Exception as e:
        logging.info("No usable state file yet (%s) — starting fresh.", e)
    return defaults

def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
        logging.info("Saved state: %s", state)
    except Exception as e:
        logging.error("Could not write state file: %s", e)

def check_slots():
    now = datetime.now(TZ)
    if not should_run_now(now):
        return
    logging.info("Running at %s Vilnius, day %s.", now.strftime("%H:%M"), now.day)

    # Random jitter 0-60s before the API call to break the metronomic polling pattern
    jitter = random.uniform(0, 60)
    logging.info("Sleeping %.1fs jitter before API call.", jitter)
    time.sleep(jitter)

    state = load_state()

    try:
        auth = json.loads(AUTH_JSON)
    except Exception as e:
        if not state.get("auth_alert_sent"):
            send_all(f"⚠️ Could not parse SCRAMBLE_AUTH: {e}")
            state["auth_alert_sent"] = True
            save_state(state)
        else:
            logging.info("Auth parse error already alerted earlier — staying quiet.")
        return

    access_token = get_access_token(auth if isinstance(auth, dict) else {})
    if not access_token:
        if not state.get("auth_alert_sent"):
            send_all(f"🔐 Session expired ⚠️\n{GROUP_B_URL} ⬅️ Update token")
            state["auth_alert_sent"] = True
            save_state(state)
        else:
            logging.info("Session-expired already alerted earlier — staying quiet.")
        return

    data = fetch_groups(access_token)
    if data is None:
        if not state.get("auth_alert_sent"):
            send_all(f"🔐 Session expired ⚠️\n{GROUP_B_URL} ⬅️ Update token")
            state["auth_alert_sent"] = True
            save_state(state)
        else:
            logging.info("Session-expired already alerted earlier — staying quiet.")
        return

    if state.get("auth_alert_sent"):
        logging.info("Auth recovered — clearing auth alert flag.")
        state["auth_alert_sent"] = False
        save_state(state)

    group_b_pct, group_a_pct, group_b_remaining, group_b_full, group_a_full = parse_groups(data)
    if group_b_pct is None or group_b_full is None:
        send_all("⚠️ Could not parse group data. Check logs.")
        return

    pct_a_str = group_a_pct or "N/A"
    filled = (group_b_full - group_b_remaining) if group_b_full > 0 else 0
    pct_value = round(filled / group_b_full * 100) if group_b_full > 0 else 0
    logging.info("Group B: filled=%.2f of %.2f (%d%%)", filled, group_b_full, pct_value)

    if filled <= 0:
        logging.info("Group B not open yet — no alert.")
        if state.get("alerted_open"):
            state["alerted_open"] = False
            save_state(state)
    elif group_b_remaining <= 0:
        logging.info("Group B full — no alert.")
        if state.get("alerted_open"):
            state["alerted_open"] = False
            save_state(state)
    else:
        if state.get("alerted_open"):
            logging.info("Already alerted for this open window — staying quiet.")
            return
        available = fetch_balance(access_token)
        if available is not None and available < 1.00:
            logging.info("Available cash €%.2f < €1.00 — suppressing alert.", available)
            return
        cash_str = f"€{available:,.2f}" if available is not None else "N/A"
        remaining_str = f"€{group_b_remaining:,.0f}"
        b_target = f"€{group_b_full:,.0f}"
        a_target = f"€{group_a_full:,.0f}" if group_a_full else "N/A"
        send_all(
            f"🙂 OPEN investment in Group B!\n"
            f"📈 Currently {pct_value}% filled ⚡\n"
            f"💸 {remaining_str} left from {b_target}\n"
            f"📊 Group A - {pct_a_str} filled\n"
            f"💵 Group A target: {a_target}\n"
            f"💰 Available cash: {cash_str}\n"
            f"{GROUP_B_URL} ⬅️ Invest now\n"
        )
        logging.info("ALERT SENT — Group B is %d%% full.", pct_value)
        state["alerted_open"] = True
        save_state(state)

if __name__ == "__main__":
    check_slots()
