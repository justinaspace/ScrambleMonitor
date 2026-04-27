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

GROUP_B_URL = "https://investor.scrambleup.com/investing"

# ----------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message})
        print("Telegram message sent.")
    except Exception as e:
        print(f"Failed to send Telegram: {e}")

# ----------------------------------------------------------------
# Main
# ----------------------------------------------------------------
async def check_slots():
    try:
        auth = json.loads(AUTH_JSON)
        cookies_list   = auth.get("cookies", [])
        localstorage   = auth.get("localStorage", {})
        print(f"Loaded {len(cookies_list)} cookies and {len(localstorage)} localStorage keys.")
    except Exception as e:
        send_telegram(f"⚠️ Could not parse SCRAMBLE_AUTH secret.\nError: {e}")
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
            return

        try:
            pct_value = float(group_b_percentage.replace("%", "").strip())
        except ValueError:
            send_telegram(f"⚠️ Unexpected format: '{group_b_percentage}'")
            return

        print(f"Group B is {pct_value}% filled.")

        if pct_value < 100:
            send_telegram(
                f"🚨 SCRAMBLEUP ALERT 🚨\n\n"
                f"Group B has OPEN SLOTS!\n"
                f"Currently {pct_value}% filled.\n\n"
                f"👉 Act fast:\n{GROUP_B_URL}"
            )
        else:
            print("Group B is 100% full. No action needed.")

if __name__ == "__main__":
    asyncio.run(check_slots())
