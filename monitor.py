import os
import json
import asyncio
import logging
import calendar
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# ----------------------------------------------------------------
# Config
# ----------------------------------------------------------------
AUTH_JSON       = os.environ.get("SCRAMBLE_AUTH", "{}")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK")
GROUP_B_URL     = "https://investor.scrambleup.com/investing"
BASE_URL        = "https://investor.scrambleup.com"
TZ              = ZoneInfo("Europe/Vilnius")
USER_AGENT      = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

# ----------------------------------------------------------------
# Notification
# ----------------------------------------------------------------
def send_all(message: str) -> None:
    logging.info(message)
    if not DISCORD_WEBHOOK:
        logging.warning("No DISCORD_WEBHOOK configured.")
        return
    try:
        requests.post(DISCORD_WEBHOOK, json={"content": message}, timeout=10)
    except Exception as e:
        logging.error("Failed to send Discord: %s", e)

# ----------------------------------------------------------------
# Schedule logic
# ----------------------------------------------------------------
def is_last_day_of_month(now: datetime) -> bool:
    last_day = calendar.monthrange(now.year, now.month)[1]
    return now.day == last_day

def should_run_now(now: datetime) -> bool:
    h, m, d = now.hour, now.minute, now.day

    if h >= 22 or h < 7:
        logging.info("Night skip: %s Vilnius. Sleeping.", now.strftime("%H:%M"))
        return False

    if is_last_day_of_month(now):
        if (h == 7 and m == 0) or (h == 15 and m == 0):
            logging.info("Last day of month — running at %s.", now.strftime("%H:%M"))
            return True
        logging.info("Last day of month — slot not allowed at %s.", now.strftime("%H:%M"))
        return False

    if 1 <= d <= 16:
        logging.info("Day %s — running every 10 min.", d)
        return True

    logging.info("Day %s — not in active schedule, skipping.", d)
    return False

# ----------------------------------------------------------------
# Auth
# ----------------------------------------------------------------
def parse_auth():
    auth = json.loads(AUTH_JSON)
    return auth.get("cookies", []), auth.get("localStorage", {})

# ----------------------------------------------------------------
# Browser helpers
# ----------------------------------------------------------------
async def setup_page(context, cookies_list, localstorage):
    cookies = [
        {
            "name":   c["name"],
            "value":  c["value"],
            "domain": c.get("domain", "investor.scrambleup.com").lstrip("."),
            "path":   c.get("path", "/"),
        }
        for c in cookies_list if c.get("name") and c.get("value")
    ]
    if cookies:
        await context.add_cookies(cookies)
        logging.info("Injected %d cookies.", len(cookies))

    page = await context.new_page()
    await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)

    if localstorage:
        await page.evaluate(
            "(data) => { for (const [k,v] of Object.entries(data)) localStorage.setItem(k,v); }",
            localstorage,
        )
        logging.info("Injected %d localStorage keys.", len(localstorage))

    return page

async def get_groups(page):
    group_selector      = '[class*="_group_"]'
    percentage_selector = '[class*="_percentage_"]'

    await page.wait_for_selector(group_selector, timeout=20000)
    groups = await page.query_selector_all(group_selector)
    logging.info("Found %d group element(s).", len(groups))

    group_a_pct     = None
    group_a_context = None
    group_b_pct     = None
    group_b_context = None

    for group in groups:
        text = (await group.inner_text()).strip()
        pct_el = await group.query_selector(percentage_selector)
        if not pct_el:
            continue
        pct_text = (await pct_el.inner_text()).strip()

        if "group b" in text.lower():
            group_b_pct     = pct_text
            group_b_context = text
            logging.info("Group B found: %s | %s", pct_text, text[:60])
        elif "group a" in text.lower():
            group_a_pct     = pct_text
            group_a_context = text
            logging.info("Group A found: %s | %s", pct_text, text[:60])

    # Fallback: single percentage element on page
    if group_b_pct is None:
        all_pcts = await page.query_selector_all(percentage_selector)
        if len(all_pcts) == 1:
            logging.info("Fallback: using single percentage element.")
            group_b_pct = (await all_pcts[0].inner_text()).strip()

    return group_b_pct, group_b_context, group_a_pct, group_a_context

# ----------------------------------------------------------------
# Main
# ----------------------------------------------------------------
async def check_slots():
    now = datetime.now(TZ)

    # ── Reserve alert: last day of month, anywhere in the 12:xx hour ──
    if is_last_day_of_month(now) and now.hour == 12:
        logging.info("Last day of month, 12:%02d Vilnius — sending reserve alert.",
                     now.minute)
        send_all(
            "⚠️ Scramble Group B Bot.\n"
            "RESERVE the Group B funds/slots"
        )

    if not should_run_now(now):
        return

    logging.info("Running at %s Vilnius, day %s.", now.strftime("%H:%M"), now.day)

    try:
        cookies_list, localstorage = parse_auth()
        logging.info(
            "Loaded %d cookies and %d localStorage keys.",
            len(cookies_list), len(localstorage)
        )
    except Exception as e:
        send_all(f"⚠️ Could not parse SCRAMBLE_AUTH secret.\nError: {e}")
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(user_agent=USER_AGENT)

        try:
            page = await setup_page(context, cookies_list, localstorage)

            await page.goto(GROUP_B_URL, wait_until="domcontentloaded", timeout=60000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeoutError:
                logging.warning("networkidle timeout — continuing anyway.")

            await page.wait_for_timeout(2000)
            logging.info("Current URL: %s", page.url)

            # Session check
            page_text = (await page.content()).lower()
            if "login" in page.url.lower() or (
                "sign in" in page_text and "logout" not in page_text
            ):
                send_all(
                    f"🔐 Session expired ⚠️\n"
                    f"{GROUP_B_URL} ⬅️ Copy here"
                )
                return

            # Extract percentages
            try:
                pct_text, group_b_context, group_a_pct, group_a_context = await get_groups(page)
            except PlaywrightTimeoutError:
                send_all(
                    f"🔐 Session expired ⚠️\n"
                    f"{GROUP_B_URL} ⬅️ Copy here"
                )
                return

            if pct_text is None:
                send_all("⚠️ Could not find Group B percentage. Please check manually.")
                return

            try:
                pct_value = int(float(pct_text.replace("%", "").strip()))
            except ValueError:
                send_all(f"⚠️ Unexpected percentage format: '{pct_text}'")
                return

            # Parse Group A percentage and context for display
            pct_value_a_str = "N/A"
            context_line_a  = ""
            if group_a_pct is not None:
                try:
                    pct_value_a_str = f"{int(float(group_a_pct.replace('%', '').strip()))}%"
                except ValueError:
                    pct_value_a_str = group_a_pct
            if group_a_context:
                context_line_a = group_a_context.splitlines()[0].strip()

            logging.info("Group B is %d%% filled.", pct_value)

            if pct_value == 0:
                logging.info("Group B is 0%% — round not open yet. No alert.")
            elif 0 < pct_value < 100:
                context_line = group_b_context.splitlines()[0].strip() if group_b_context else "Group B"
                send_all(
                    f"🙂 OPEN investment in Group B!\n"
                    f"📈 Currently **{pct_value}%** filled ⚡\n"
                    f"💸 {context_line}\n\n"
                    f"📊 Group A - {pct_value_a_str} filled\n"
                    f"💶 {context_line_a}\n"
                    f"{GROUP_B_URL} ⬅️ Invest now\n"
                )
                logging.info("ALERT SENT — Group B is %d%% full.", pct_value)
            else:
                logging.info("Group B is 100%% full. No alert.")

        except Exception as e:
            send_all(f"⚠️ Something unexpected\n🔐 Try to update the Cookies\n{GROUP_B_URL} ⬅️ Copy here")
        finally:
            await browser.close()

if __name__ == "__main__":
    asyncio.run(check_slots())
