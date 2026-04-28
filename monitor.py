import os
import json
import asyncio
import requests
from datetime import datetime
import zoneinfo
from playwright.async_api import async_playwright

# ----------------------------------------------------------------
# Config
# ----------------------------------------------------------------
AUTH_JSON       = os.environ["SCRAMBLE_AUTH"]
DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK"]
GROUP_B_URL     = "https://investor.scrambleup.com/investing"

# ----------------------------------------------------------------
# Notification
# ----------------------------------------------------------------
def send_all(message):
    try:
        requests.post(DISCORD_WEBHOOK, json={"content": message})
        print("Discord message sent.")
    except Exception as e:
        print(f"Failed to send Discord: {e}")

# ----------------------------------------------------------------
# Main
# ----------------------------------------------------------------
async def check_slots():
    vilnius_time   = datetime.now(zoneinfo.ZoneInfo("Europe/Vilnius"))
    current_hour   = vilnius_time.hour
    current_minute = vilnius_time.minute
    current_day    = vilnius_time.day

    # ── Night skip (all days) ──────────────────────────────────
    if current_hour >= 23 or current_hour < 7:
        print(f"Outside active hours ({vilnius_time.strftime('%H:%M')} Vilnius). Skipping.")
        return

    # ── Day 1-16: run every 10 min — no extra restriction ─────
    if 1 <= current_day <= 16:
        print(f"Day {current_day} — running every 10 min.")

    # ── Day 17-20: run every 60 min only ──────────────────────
    elif 17 <= current_day <= 20:
        if current_minute != 0:
            print(f"Day {current_day} — 60 min schedule, skipping at :{current_minute:02d}.")
            return
        print(f"Day {current_day} — running every 60 min.")

    # ── Day 21-31: run only at 07:00 and 15:00 ────────────────
    elif current_day >= 21:
        if not (current_hour == 7 and current_minute == 0) and \
           not (current_hour == 15 and current_minute == 0):
            print(f"Day {current_day} — 2x daily schedule, skipping at {vilnius_time.strftime('%H:%M')}.")
            return
        print(f"Day {current_day} — running 2x daily at {vilnius_time.strftime('%H:%M')}.")

    print(f"Running at {vilnius_time.strftime('%H:%M')} Vilnius, day {current_day}...")

    # Load auth
    try:
        auth         = json.loads(AUTH_JSON)
        cookies_list = auth.get("cookies", [])
        localstorage = auth.get("localStorage", {})
        print(f"Loaded {len(cookies_list)} cookies and {len(localstorage)} localStorage keys.")
    except Exception as e:
        send_all(f"⚠️ Could not parse SCRAMBLE_AUTH secret.\nError: {e}")
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"
        )

        # Inject cookies
        playwright_cookies = [
            {"name": c["name"], "value": c["value"],
             "domain": c.get("domain", "investor.scrambleup.com").lstrip("."),
             "path": c.get("path", "/")}
            for c in cookies_list if c.get("name") and c.get("value")
        ]
        if playwright_cookies:
            await context.add_cookies(playwright_cookies)

        page = await context.new_page()

        # Set localStorage
        await page.goto("https://investor.scrambleup.com", wait_until="domcontentloaded", timeout=30000)
        if localstorage:
            await page.evaluate(
                "(data) => { for (const [k,v] of Object.entries(data)) localStorage.setItem(k,v); }",
                localstorage
            )

        # Navigate to investing page
        try:
            await page.goto(GROUP_B_URL, wait_until="networkidle", timeout=60000)
        except Exception as e:
            send_all(f"⚠️ Could not load page.\nError: {e}")
            await browser.close()
            return

        await page.wait_for_timeout(3000)
        print(f"Current URL: {page.url}")

        # Check if logged out
        page_text = (await page.content()).lower()
        if "login" in page.url or ("sign in" in page_text and "logout" not in page_text):
            send_all(
                "🔐 ScrambleUp Monitor: SESSION EXPIRED\n\n"
                "Do this to resume:\n"
                "1. Log in to investor.scrambleup.com\n"
                "2. Click 'ScrambleUp Auth Export' bookmark\n"
                "3. Paste into GitHub Secret: SCRAMBLE_AUTH"
            )
            await browser.close()
            return

        # Smart wait for percentage element
        try:
            await page.wait_for_selector("[class*='_percentage_']", timeout=15000)
        except Exception:
            send_all("⚠️ Could not find Group B percentage. Please check manually.")
            await browser.close()
            return

        elements = await page.query_selector_all("[class*='_percentage_']")
        print(f"Found {len(elements)} percentage element(s).")

        group_b_percentage = None
        group_b_context    = ""

        for el in elements:
            try:
                parent_text = await (await el.evaluate_handle(
                    "node => node.closest('[class*=\"_group_\"]')"
                )).evaluate("node => node ? node.innerText : ''")
            except Exception:
                parent_text = ""

            pct_text = (await el.inner_text()).strip()
            print(f"Percentage: '{pct_text}' | Context: '{parent_text[:60]}'")

            if "group b" in parent_text.lower():
                group_b_percentage = pct_text
                group_b_context    = parent_text.split("\n")[0].strip()
                print(f"✅ Group B: {group_b_percentage} | {group_b_context}")
                break

        if group_b_percentage is None and len(elements) == 1:
            group_b_percentage = (await elements[0].inner_text()).strip()

        await browser.close()

        if group_b_percentage is None:
            send_all("⚠️ Could not find Group B percentage. Please check manually.")
            return

        try:
            pct_value = float(group_b_percentage.replace("%", "").strip())
        except ValueError:
            send_all(f"⚠️ Unexpected format: '{group_b_percentage}'")
            return

        print(f"Group B is {pct_value}% filled.")

        if 0 <= pct_value:
            send_all(
                f"🚨 SCRAMBLE ALERT 🚨\n\n"
                f"🙂 Group B investment is OPEN!\n"
                f"📈 Currently **{pct_value}%** filled ⚡\n"
                f"💶 {group_b_context}\n\n"
                f"👉 Invest now:\n{GROUP_B_URL}"
            )
            print(f"ALERT SENT — Group B is {pct_value}% full!")
        elif pct_value == 0:
            print("Group B is 0% — round not open yet. No alert.")
        else:
            print("Group B is 100% full. No alert.")

if __name__ == "__main__":
    asyncio.run(check_slots())
