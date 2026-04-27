import os
import json
import asyncio
import requests
from playwright.async_api import async_playwright

# ----------------------------------------------------------------
# Config
# ----------------------------------------------------------------
AUTH_JSON        = os.environ.get("SCRAMBLE_AUTH")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
DISCORD_WEBHOOK  = os.environ.get("DISCORD_WEBHOOK")

GROUP_B_URL = "https://investor.scrambleup.com/investing"

def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10)
        print("Telegram message sent.")
    except Exception as e:
        print(f"Failed to send Telegram: {e}")

def send_discord(message):
    try:
        requests.post(DISCORD_WEBHOOK, json={"content": message}, timeout=10)
        print("Discord message sent.")
    except Exception as e:
        print(f"Failed to send Discord: {e}")

async def check_slots():
    from datetime import datetime
    import zoneinfo
    
    vilnius_time = datetime.now(zoneinfo.ZoneInfo("Europe/Vilnius"))
    current_hour = vilnius_time.hour
    current_day  = vilnius_time.day

    # 1. Time/Date Gates
    if current_hour >= 22 or current_hour < 7:
        print(f"Outside active hours ({vilnius_time.strftime('%H:%M')} Vilnius). Skipping.")
        return

    if current_day >= 20:
        print(f"Day {current_day} — outside active days (1-19). Skipping.")
        return

    # 2. Auth Parsing
    try:
        auth = json.loads(AUTH_JSON)
        cookies_list = auth.get("cookies", [])
        localstorage = auth.get("localStorage", {})
    except Exception as e:
        msg = f"⚠️ Could not parse SCRAMBLE_AUTH secret.\nError: {e}"
        send_telegram(msg)
        send_discord(msg)
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
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

        page = await context.new_page()

        try:
            # Set LocalStorage
            await page.goto("https://investor.scrambleup.com", wait_until="domcontentloaded")
            if localstorage:
                await page.evaluate("(data) => { for (const [k, v] of Object.entries(data)) { localStorage.setItem(k, v); } }", localstorage)

            # Main Scrape
            await page.goto(GROUP_B_URL, wait_until="networkidle", timeout=60000)
            await page.wait_for_timeout(5000) # Give React/Vue time to render

            current_url = page.url
            page_text = (await page.content()).lower()

            # Check Session
            if "login" in current_url or ("sign in" in page_text and "logout" not in page_text):
                alert = "🔐 ScrambleUp Monitor: SESSION EXPIRED"
                send_telegram(alert)
                send_discord(alert)
                await browser.close()
                return

            # Find Group B
            elements = await page.query_selector_all("[class*='_percentage_']")
            group_b_percentage = None

            for el in elements:
                parent_text = await page.evaluate("node => node.closest('[class*=\"_group_\"]')?.innerText || ''", el)
                pct_text = (await el.inner_text()).strip()

                if "group b" in parent_text.lower():
                    group_b_percentage = pct_text
                    break

            # Fallback if only one exists
            if group_b_percentage is None and len(elements) == 1:
                group_b_percentage = (await elements.inner_text()).strip()

            if group_b_percentage:
                pct_value = float(group_b_percentage.replace("%", "").strip())
                print(f"Group B is {pct_value}% filled.")

                if pct_value >= 0:
                    alert_msg = f"🚨 SCRAMBLE ALERT 🚨\nGroup B is {pct_value}% filled!\n{GROUP_B_URL}"
                    send_telegram(alert_msg)
                    send_discord(alert_msg)
            else:
                print("Could not find percentage.")

        except Exception as e:
            print(f"Error during scrape: {e}")
        finally:
            await browser.close()

if __name__ == "__main__":
    asyncio.run(check_slots())
