import os
import json
import asyncio
import requests
from playwright.async_api import async_playwright

# ----------------------------------------------------------------
# Config
# ----------------------------------------------------------------
AUTH_JSON        = os.environ["SCRAMBLE_AUTH"]
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
DISCORD_WEBHOOK  = os.environ["DISCORD_WEBHOOK"]

GROUP_B_URL = "https://investor.scrambleup.com/investing"

# ----------------------------------------------------------------
# Telegram and Discord
# ----------------------------------------------------------------
def send_telegram(message):
    # Telegram
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message})
        print("Telegram message sent.")
    except Exception as e:
        print(f"Failed to send Telegram: {e}")

def send_discord(message):
    # Discord
    try:
        webhook_url = os.environ["DISCORD_WEBHOOK"]
        requests.post(webhook_url, json={"content": message})
        print("Discord message sent.")
    except Exception as e:
        print(f"Failed to send Discord: {e}")

# ----------------------------------------------------------------
# Main
# ----------------------------------------------------------------
async def check_slots():
   # Skip outside active hours (22:00 - 07:00 Vilnius time)
    from datetime import datetime
    import zoneinfo
    vilnius_time = datetime.now(zoneinfo.ZoneInfo("Europe/Vilnius"))
    current_hour = vilnius_time.hour
    current_day  = vilnius_time.day

    if current_hour >= 22 or current_hour < 7:
        print(f"Outside active hours ({vilnius_time.strftime('%H:%M')} Vilnius). Skipping.")
        return

    # Skip between day 20 and end of month
    if current_day >= 20:
        print(f"Day {current_day} — outside active days (1-19). Skipping.")
        return

    try:
        auth = json.loads(AUTH_JSON)
        cookies_list = auth.get("cookies", [])
        localstorage = auth.get("localStorage", {})
        print(f"Loaded {len(cookies_list)} cookies and {len(localstorage)} localStorage keys.")
    except Exception as e:
        send_telegram(f"⚠️ Could not parse SCRAMBLE_AUTH secret.\nError: {e}")
        send_discord(f"⚠️ Could not parse SCRAMBLE_AUTH secret.\nError: {e}")
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"
        )

        # Inject cookies
        playwright_cookies = []
        for c in cookies_list:
            if c.get("name") and c.get("value"):
                playwright_cookies.append({
                    "name":   c["name"],
                    "value":  c["value"],
                    "domain": c.get("domain", "investor.scrambleup.com").lstrip("."),
                    "path":   c.get("path", "/"),
                })
        if playwright_cookies:
            await context.add_cookies(playwright_cookies)
            print(f"Injected {len(playwright_cookies)} cookies.")

        page = await context.new_page()

        # Go to domain first to set localStorage
        await page.goto("https://investor.scrambleup.com", wait_until="domcontentloaded", timeout=30000)

        # Inject localStorage
        if localstorage:
            await page.evaluate("""
                (data) => {
                    for (const [key, value] of Object.entries(data)) {
                        localStorage.setItem(key, value);
                    }
                }
            """, localstorage)
            print(f"Injected {len(localstorage)} localStorage items.")

        # Navigate to investing page
        try:
            await page.goto(GROUP_B_URL, wait_until="networkidle", timeout=60000)
        except Exception as e:
            send_telegram(f"⚠️ Could not load page.\nError: {e}")
            send_discord(f"⚠️ Could not load page.\nError: {e}")
            await browser.close()
            return

        await page.wait_for_timeout(8000)

        current_url = page.url
        page_text   = (await page.content()).lower()
        print(f"Current URL: {current_url}")

        # Check if logged out
        if "login" in current_url or ("sign in" in page_text and "logout" not in page_text):
            send_telegram(
                "🔐 ScrambleUp Monitor: SESSION EXPIRED\n\n"
                "Do this to resume:\n"
                "1. Log in to investor.scrambleup.com\n"
                "2. Click the 'ScrambleUp Auth Export' bookmark\n"
                "3. Paste into GitHub Secret: SCRAMBLE_AUTH\n\n"
                "⏸ Monitoring paused until updated."
            )
            send_discord(
                "🔐 ScrambleUp Monitor: SESSION EXPIRED\n\n"
                "Do this to resume:\n"
                "1. Log in to investor.scrambleup.com\n"
                "2. Click the 'ScrambleUp Auth Export' bookmark\n"
                "3. Paste into GitHub Secret: SCRAMBLE_AUTH\n\n"
                "⏸ Monitoring paused until updated."
            )            
            print("Session expired.")
            await browser.close()
            return

        # Find Group B percentage
        await page.wait_for_timeout(2000)
        elements = await page.query_selector_all("[class*='_percentage_']")
        print(f"Found {len(elements)} percentage element(s).")

        group_b_percentage = None

        for el in elements:
            try:
                parent_handle = await el.evaluate_handle(
                    "node => node.closest('[class*=\"_group_\"]')"
                )
                parent_text = await parent_handle.evaluate("node => node ? node.innerText : ''")
            except Exception:
                parent_text = ""

            pct_text = (await el.inner_text()).strip()
            print(f"Percentage: '{pct_text}' | Context: '{parent_text[:60]}'")

            if "group b" in parent_text.lower():
                group_b_percentage = pct_text
                print(f"✅ Group B: {group_b_percentage}")
                break

        if group_b_percentage is None and len(elements) == 1:
            group_b_percentage = (await elements[0].inner_text()).strip()

        await browser.close()

        if group_b_percentage is None:
            send_telegram("⚠️ Could not find Group B percentage. Please check manually.")
            send_discord("⚠️ Could not find Group B percentage. Please check manually.")
            return

        try:
            pct_value = float(group_b_percentage.replace("%", "").strip())
        except ValueError:
            send_telegram(f"⚠️ Unexpected format: '{group_b_percentage}'")
            send_discord(f"⚠️ Unexpected format: '{group_b_percentage}'")
            return

        print(f"Group B is {pct_value}% filled.")

        # ✅ Only alert when round is actively open (between 0% and 100%)
        if 0 < pct_value < 100:
            send_telegram(
                f"🚨 SCRAMBLE ALERT 🚨\n\n"
                f"Group B investment is OPEN!\n"
                f"Currently {pct_value}% filled — act fast!\n\n"
                f"👉 Invest now:\n{GROUP_B_URL}"
            )
            send_discord(
                f"🚨 SCRAMBLE ALERT 🚨\n\n"
                f"Group B investment is OPEN!\n"
                f"Currently {pct_value}% filled — act fast!\n\n"
                f"👉 Invest now:\n{GROUP_B_URL}"
            )
            print(f"ALERT SENT — Group B is {pct_value}% full!")
        elif pct_value == 0:
            print("Group B is 0% — round not open yet. No alert.")
        else:
            print("Group B is 100% full. No alert.")

if __name__ == "__main__":
    asyncio.run(check_slots())
