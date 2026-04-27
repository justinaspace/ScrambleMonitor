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
GROUP_B_URL      = "https://investor.scrambleup.com/investing"

def send_alert(message):
    """Combined alerts to save request overhead"""
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        # Fire and forget quickly with low timeout
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data=payload, timeout=5)
        requests.post(DISCORD_WEBHOOK, json={"content": message}, timeout=5)
    except:
        pass

async def check_slots():
    from datetime import datetime
    import zoneinfo
    
    # 1. Immediate Gate (Fastest)
    tz = zoneinfo.ZoneInfo("Europe/Vilnius")
    now = datetime.now(tz)
    if now.hour >= 22 or now.hour < 7 or now.day >= 20:
        return

    # 2. Setup Playwright with optimized resource blocking
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        # Block images and CSS to speed up loading
        context = await browser.new_context(user_agent="Mozilla/5.0...")
        page = await context.new_page()
        
        # Optimization: Abort images and fonts to save bandwidth/time
        await page.route("**/*.{png,jpg,jpeg,svg,woff2,css}", lambda route: route.abort())

        try:
            # 3. Quick Auth Injection
            await page.goto("https://investor.scrambleup.com", wait_until="commit")
            auth = json.loads(AUTH_JSON)
            await context.add_cookies(auth.get("cookies", []))
            
            ls_script = "data => { for (const [k, v] of Object.entries(data)) localStorage.setItem(k, v); }"
            await page.evaluate(ls_script, auth.get("localStorage", {}))

            # 4. Smart Navigation
            # 'commit' is faster than 'networkidle' or 'load'
            await page.goto(GROUP_B_URL, wait_until="commit")

            # Instead of waiting 5-8 seconds, wait ONLY for the element to appear
            selector = "[class*='_percentage_']"
            try:
                await page.wait_for_selector(selector, timeout=15000)
            except:
                print("Element didn't appear in time.")
                return

            # 5. Fast Data Extraction
            # We do the heavy lifting inside the browser context in one go
            data = await page.evaluate("""() => {
                const elements = document.querySelectorAll("[class*='_percentage_']");
                for (let el of elements) {
                    const parentText = el.closest('[class*="_group_"]')?.innerText.toLowerCase() || "";
                    if (parentText.includes("group b")) return el.innerText;
                }
                return elements.length === 1 ? elements.innerText : null;
            }""")

            if data:
                pct_value = float(data.replace("%", "").strip())
                print(f"Group B: {pct_value}%")
                if pct_value >= 0:
                    send_alert(f"🚨 SCRAMBLE: Group B is {pct_value}%! {GROUP_B_URL}")
            
        finally:
            await browser.close()

if __name__ == "__main__":
    asyncio.run(check_slots())
