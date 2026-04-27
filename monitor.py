import os
import json
import asyncio
import requests
from playwright.async_api import async_playwright

# ----------------------------------------------------------------
# Config from GitHub Secrets
# ----------------------------------------------------------------
COOKIES_JSON      = os.environ["SCRAMBLE_COOKIES"]
LOCALSTORAGE_JSON = os.environ.get("SCRAMBLE_LOCALSTORAGE", "{}")
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID  = os.environ["TELEGRAM_CHAT_ID"]

GROUP_B_URL = "https://investor.scrambleup.com/investing"

# ----------------------------------------------------------------
# Send Telegram message
# ----------------------------------------------------------------
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message})
        print("Telegram message sent.")
    except Exception as e:
        print(f"Failed to send Telegram: {e}")

# ----------------------------------------------------------------
# Main check
# ----------------------------------------------------------------
async def check_slots():
    # Parse cookies
    try:
        cookies = json.loads(COOKIES_JSON)
        print(f"Loaded {len(cookies)} cookies.")
    except Exception as e:
        send_telegram(f"⚠️ Cookie parse error: {e}")
        return

    # Parse localStorage
    try:
        localstorage = json.loads(LOCALSTORAGE_JSON)
        print(f"Loaded {len(localstorage)} localStorage keys: {list(localstorage.keys())}")
    except Exception as e:
        print(f"localStorage parse warning: {e}")
        localstorage = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"
        )

        # Inject cookies
        playwright_cookies = []
        for c in cookies:
            cookie = {
                "name":   c.get("name", ""),
                "value":  c.get("value", ""),
                "domain": c.get("domain", "investor.scrambleup.com").lstrip("."),
                "path":   c.get("path", "/"),
                "secure": c.get("secure", False),
                "httpOnly": c.get("httpOnly", False),
            }
            if cookie["name"] and cookie["value"]:
                playwright_cookies.append(cookie)

        if playwright_cookies:
            await context.add_cookies(playwright_cookies)
            print(f"Injected {len(playwright_cookies)} cookies into browser.")

        page = await context.new_page()

        # First navigate to the domain so we can set localStorage
        await page.goto("https://investor.scrambleup.com", wait_until="domcontentloaded", timeout=30000)

        # Inject localStorage if we have it
        if localstorage:
            await page.evaluate("""
                (data) => {
                    for (const [key, value] of Object.entries(data)) {
                        localStorage.setItem(key, value);
                    }
                }
            """, localstorage)
            print(f"Injected {len(localstorage)} localStorage items.")

        # Now navigate to the actual investing page
        try:
            print(f"Loading page: {GROUP_B_URL}")
            await page.goto(GROUP_B_URL, wait_until="networkidle", timeout=60000)
        except Exception as e:
            send_telegram(f"⚠️ Could not load page.\nError: {e}")
            await browser.close()
            return

        # Wait for React to render
        await page.wait_for_timeout(8000)

        # Check current URL and page state
        current_url = page.url
        print(f"Current URL after load: {current_url}")

        page_text = (await page.content()).lower()

        # Check if redirected to login
        if "login" in current_url or ("sign in" in page_text and "logout" not in page_text):
            send_telegram(
                "🔐 ScrambleUp Monitor: SESSION EXPIRED\n\n"
                "Please re-do the full setup:\n"
                "1. Log in to investor.scrambleup.com\n"
                "2. Export cookies via Cookie-Editor → update SCRAMBLE_COOKIES\n"
                "3. Run in console: JSON.stringify(localStorage)\n"
                "   → update SCRAMBLE_LOCALSTORAGE\n\n"
                "Monitoring paused until refreshed."
            )
            print("Session expired — user notified.")
            await browser.close()
            return

        # Debug: show all relevant elements
        all_classes = await page.evaluate("""
            () => {
                const els = document.querySelectorAll('[class]');
                const found = [];
                els.forEach(el => {
                    const c = el.className || '';
                    const t = (el.innerText || '').substring(0, 60).replace(/\\n/g, ' ');
                    if (typeof c === 'string' && (
                        c.includes('percentage') || c.includes('group') ||
                        c.includes('value') || c.includes('progress') ||
                        c.includes('invest') || c.includes('slot')
                    )) {
                        found.push(`CLASS: ${c} | TEXT: ${t}`);
                    }
                });
                return found.slice(0, 30);
            }
        """)

        print(f"Relevant elements on page ({len(all_classes)} found):")
        for cls in all_classes:
            print(" -", cls)

        if not all_classes:
            print("No relevant elements found — page likely not authenticated.")
            # Print page title and first 300 chars for debugging
            title = await page.title()
            print(f"Page title: {title}")
            snippet = (await page.content())[:300]
            print(f"Page snippet: {snippet}")
            send_telegram(
                "⚠️ ScrambleUp Monitor: Page loaded but no investment elements found.\n\n"
                "Authentication may have failed.\n"
                "Please refresh your cookies and localStorage in GitHub Secrets."
            )
            await browser.close()
            return

        # Find Group B percentage
        elements = await page.query_selector_all("[class*='_percentage_']")
        print(f"\nFound {len(elements)} percentage element(s).")

        group_b_percentage = None

        for el in elements:
            try:
                parent_handle = await el.evaluate_handle(
                    "node => node.closest('[class*=\"_group_\"], [class*=\"_content_\"], [class*=\"_wrapper_\"]')"
                )
                parent_text = await parent_handle.evaluate("node => node ? node.innerText : ''")
            except Exception:
                parent_text = ""

            pct_text = (await el.inner_text()).strip()
            print(f"Percentage: '{pct_text}' | Context: '{parent_text[:80]}'")

            if "group b" in parent_text.lower():
                group_b_percentage = pct_text
                print(f"✅ Group B percentage identified: {group_b_percentage}")
                break

        # Fallback: single element
        if group_b_percentage is None and len(elements) == 1:
            group_b_percentage = (await elements[0].inner_text()).strip()
            print(f"Using sole percentage element: {group_b_percentage}")

        await browser.close()

        if group_b_percentage is None:
            send_telegram(
                "⚠️ ScrambleUp Monitor: Found elements but could not isolate Group B.\n"
                "Please check manually."
            )
            return

        # Parse percentage
        try:
            pct_value = float(group_b_percentage.replace("%", "").strip())
        except ValueError:
            send_telegram(f"⚠️ Unexpected percentage format: '{group_b_percentage}'")
            return

        print(f"\nGroup B is {pct_value}% filled.")

        if pct_value < 100:
            send_telegram(
                f"🚨 SCRAMBLEUP ALERT 🚨\n\n"
                f"Group B has OPEN INVESTMENT SLOTS!\n"
                f"Currently {pct_value}% filled.\n\n"
                f"👉 Act fast:\n{GROUP_B_URL}"
            )
            print(f"ALERT SENT — Group B is {pct_value}% full!")
        else:
            print("Group B is 100% full. No alert needed.")

if __name__ == "__main__":
    asyncio.run(check_slots())
