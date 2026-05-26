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
MANUAL_RUN      = os.environ.get("MANUAL_RUN", "false").lower() == "true"
GROUP_B_URL     = "https://investor.scrambleup.com/investing"
API_URL         = "https://investor.scrambleup.com/api/investors/invested_in_groups_stats/"
TZ              = ZoneInfo("Europe/Vilnius")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

def send_all(message: str) -> None:
    logging.info(message)
    if not DISCORD_WEBHOOK:
        return
    try:
        requests.post(DISCORD_WEBHOOK, json={"content": message}, timeout=10)
    except Exception as e:
        logging.error("Discord error: %s", e)

def is_last_day_of_month(now):
    return now.day == calendar.monthrange(now.year, now.month)[1]

def should_run_now(now):
    h, m, d = now.hour, now.minute, now.day
    if h >= 22 or h < 7:
        return False
    if is_last_day_of_month(now):
        return h == 18 and m == 0
    return 1 <= d <= 16

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

def fetch_rounds(access_token: str) -> dict | None:
    headers = {
        "Authorization": f"Token {access_token}",
        "X-Api-Version": "3",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
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

def parse_groups(data) -> tuple:
    """Parse API response to extract Group A and Group B percentages."""
    logging.info("API data: %s", json.dumps(data)[:500])
    group_a_pct = group_a_ctx = group_b_pct = group_b_ctx = None

    # data could be a list of rounds or a dict with rounds inside
    rounds = data if isinstance(data, list) else data.get("results", data.get("rounds", [data]))

    for item in rounds:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "") or item.get("title", "") or item.get("group", "")).lower()
        # Try various percentage field names
        pct = (item.get("filled_percentage") or item.get("percentage") or
               item.get("fill_percentage") or item.get("progress") or
               item.get("funded_percentage") or item.get("percent"))
        if pct is not None:
            pct_str = f"{float(pct):.0f}%"
            ctx = item.get("name") or item.get("title") or name
            if "b" in name or "group b" in name or name == "b":
                group_b_pct, group_b_ctx = pct_str, str(ctx)
            elif "a" in name or "group a" in name or name == "a":
                group_a_pct, group_a_ctx = pct_str, str(ctx)

    # If we couldn't identify by name, log all fields to help debugging
    if group_b_pct is None:
        logging.info("Could not identify groups by name. Raw rounds: %s", json.dumps(rounds)[:600])

    return group_b_pct, group_b_ctx, group_a_pct, group_a_ctx

def check_slots():
    now = datetime.now(TZ)

    if not MANUAL_RUN and is_last_day_of_month(now) and now.hour == 12:
        send_all("⚠️ Scramble Group B Bot.\nRESERVE the Group B funds/slots")

    if not MANUAL_RUN and not should_run_now(now):
        return

    if MANUAL_RUN:
        logging.info("Manual run triggered.")
    logging.info("Running at %s Vilnius, day %s.", now.strftime("%H:%M"), now.day)

    try:
        auth = json.loads(AUTH_JSON) if isinstance(AUTH_JSON, str) else AUTH_JSON
    except Exception as e:
        send_all(f"⚠️ Could not parse SCRAMBLE_AUTH: {e}")
        return

    access_token = get_access_token(auth if isinstance(auth, dict) else {})
    if not access_token:
        send_all(f"🔐 Session expired ⚠️\n{GROUP_B_URL} ⬅️ Copy here")
        return

    data = fetch_rounds(access_token)
    if data is None:
        send_all(f"🔐 Session expired ⚠️\n{GROUP_B_URL} ⬅️ Copy here")
        return

    group_b_pct, group_b_ctx, group_a_pct, group_a_ctx = parse_groups(data)

    if group_b_pct is None:
        send_all(f"⚠️ Could not parse group data. Check logs.")
        return

    try:
        pct_value = int(float(group_b_pct.replace("%", "").strip()))
    except ValueError:
        send_all(f"⚠️ Unexpected format: '{group_b_pct}'")
        return

    pct_a_str = "N/A"
    ctx_a_line = ""
    if group_a_pct:
        try:
            pct_a_str = f"{int(float(group_a_pct.replace('%','').strip()))}%"
        except ValueError:
            pct_a_str = group_a_pct
    if group_a_ctx:
        ctx_a_line = str(group_a_ctx).splitlines()[0].strip()

    logging.info("Group B: %s, Group A: %s", group_b_pct, group_a_pct)

    if pct_value == 0:
        logging.info("Group B 0%% — not open yet.")
    elif 0 < pct_value < 100:
        ctx_b_line = str(group_b_ctx).splitlines()[0].strip() if group_b_ctx else "Group B"
        send_all(
            f"🙂 OPEN investment in Group B!\n"
            f"📈 Currently **{pct_value}%** filled ⚡\n"
            f"💸 {ctx_b_line}\n"
            f"📊 Group A - {pct_a_str} filled\n"
            f"💵 {ctx_a_line}\n"
            f"{GROUP_B_URL} ⬅️ Invest now\n"
        )
        logging.info("ALERT SENT — %d%% full.", pct_value)
    else:
        logging.info("Group B 100%% full.")

if __name__ == "__main__":
    check_slots()
