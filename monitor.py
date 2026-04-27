import os
import json
import asyncio
from playwright.async_api import async_playwright
import requests

# ----------------------------------------------------------------
# Config from GitHub Secrets
# ----------------------------------------------------------------
COOKIES_JSON     = os.environ["SCRAMBLE_COOKIES"]
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

GROUP_B_URL = "https://investor.scrambleup.com/investing"  # ← update to exact URL

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
# Main check using a real browser (handles JavaScript pages)
# ----------------------------------------------------------------
async def check_slots():
    # Parse cookies
    try:
        cookies = json.loads(COOKIES_JSON)
        print(f"Loaded {len(cookies)} cookies.")
    except Exception as e:
        send_telegram(f"⚠️ ScrambleUp Monitor ERROR\nCould not parse cookies.\nError: {e}")
        return

    async with async_playwright() as p:
        # Launch headless browser (invisible Chrome)
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"
        )

        # Inject your cookies so you're already logged in
        playwright_cookies = []
        for c in cookies:
            cookie = {
                "name":   c.get("name", ""),
                "value":  c.get("value", ""),
                "domain": c.get("domain", "scrambleup.com"),
                "path":   c.get("path", "/"),
            }
            # Only add optional fields if they exist and are valid
            if c.get("secure") is not None:
                cookie["secure"] = c["secure"]
            if c.get("httpOnly") is not None:
                cookie["httpOnly"] = c["httpOnly"]
            playwright_cookies.append(cookie)

        await context.add_cookies(playwright_cookies)

        page = await context.new_page()

        # Navigate to Group B page
        try:
            print(f"Loading page: {GROUP_B_URL}")
            await page.goto(GROUP_B_URL, wait_until="networkidle", timeout=60000)
        except Exception as e:
            send_telegram(f"⚠️ ScrambleUp Monitor: Could not load page.\nError: {e}")
            await browser.close()
            return

        # Check if session expired (redirected to login)
        current_url = page.url
        page_text = (await page.content()).lower()

        logged_out_signals = ["login", "sign in", "forgot password", "enter your email"]
        if any(kw in page_text for kw in logged_out_signals) or "login" in current_url:
            send_telegram(
                "🔐 ScrambleUp Monitor: SESSION EXPIRED\n\n"
                "Your cookies have expired. Here is what to do:\n\n"
                "1. Log in to scrambleup.com (full email + SMS process)\n"
                "2. Navigate to the Group B page\n"
                "3. Click Cookie-Editor → Export → Export as JSON\n"
                "4. Go to GitHub → repo → Settings → Secrets\n"
                "5. Update SCRAMBLE_COOKIES with new cookies\n\n"
                "Monitoring is paused until you refresh cookies."
            )
            print("Session expired — user notified.")
            await browser.close()
            return

        print("Session valid. Looking for Group B percentage...")

        # Wait longer for React to fully render
        import re
        await page.wait_for_timeout(8000)  # wait 8 seconds for JS to render

        # Debug: print all class names containing 'percentage' or 'group'
        all_classes = await page.evaluate("""
            () => {
                const els = document.querySelectorAll('[class]');
                const found = [];
                els.forEach(el => {
                    const c = el.className;
                    if (typeof c === 'string' && 
                        (c.includes('percentage') || c.includes('group') || c.includes('value'))) {
                        found.push(c + ' | text: ' + el.innerText.substring(0, 50));
                    }
                });
                return found.slice(0, 20);
            }
        """)
        print("Relevant elements found on page:")
        for cls in all_classes:
            print(" -", cls)

        # Try to find percentage element
        try:
            await page.wait_for_selector("[class*='_percentage_']", timeout=20000)
            print("Found percentage element.")
        except Exception:
            # Also try alternative selectors
            alt_found = await page.query_selector_all("[class*='percentage']")
            if alt_found:
                print(f"Found {len(alt_found)} elements with 'percentage' in class (alt selector)")
            else:
                page_snippet = (await page.content())[:500]
                print(f"Page start: {page_snippet}")
                send_telegram(
                    "⚠️ ScrambleUp Monitor: Could not find Group B percentage element.\n\n"
                    "The page layout may have changed.\n"
                    "Please check scrambleup.com manually."
                )
                print("Percentage element not found.")
                await browser.close()
                return

        # Find ALL percentage elements (there may be multiple groups)
        elements = await page.query_selector_all("[class*='_percentage_']")
        print(f"Found {len(elements)} percentage element(s).")

        group_b_percentage = None

        for el in elements:
            # For each percentage, check if "Group B" is nearby
            # Look at the parent container for "Group B" text
            parent = await el.evaluate_handle(
                "node => node.closest('[class*=\"_group_\"], [class*=\"_content_\"], [class*=\"_wrapper_\"]')"
            )
            parent_text = ""
            try:
                parent_text = await parent.evaluate("node => node ? node.innerText : ''")
            except Exception:
                pass

            pct_text = (await el.inner_text()).strip()
            print(f"Element text: '{pct_text}' | Parent text snippet: '{parent_text[:80]}'")

            if "group b" in parent_text.lower():
                group_b_percentage = pct_text
                print(f"Group B percentage found: {group_b_percentage}")
                break

        # Fallback: if only one percentage on the page, use it
        if group_b_percentage is None and len(elements) == 1:
            group_b_percentage = (await elements[0].inner_text()).strip()
            print(f"Using only percentage on page: {group_b_percentage}")

        await browser.close()

        # ----------------------------------------------------------------
        # Decision logic
        # ----------------------------------------------------------------
        if group_b_percentage is None:
            send_telegram(
                "⚠️ ScrambleUp Monitor: Could not identify Group B specifically.\n"
                "Please check the page manually."
            )
            return

        # Parse the number (remove % sign)
        try:
            pct_value = float(group_b_percentage.replace("%", "").strip())
        except ValueError:
            send_telegram(
                f"⚠️ ScrambleUp Monitor: Unexpected percentage format: '{group_b_percentage}'\n"
                "Please check manually."
            )
            return

        print(f"Group B is {pct_value}% filled.")

        if pct_value < 100:
            send_telegram(
                f"🚨 SCRAMBLEUP ALERT 🚨\n\n"
                f"Group B has OPEN INVESTMENT SLOTS!\n"
                f"Currently {pct_value}% filled — slots still available.\n\n"
                f"👉 Act fast:\n{GROUP_B_URL}"
            )
            print(f"ALERT SENT — Group B is only {pct_value}% full!")
        else:
            print("Group B is 100% full. No action needed.")

# ----------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------
if __name__ == "__main__":
    asyncio.run(check_slots())
