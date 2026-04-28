import os
import json
import asyncio
import logging
import requests
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# ----------------------------------------------------------------
# Config
# ----------------------------------------------------------------
AUTH_JSON       = os.environ.get("SCRAMBLE_AUTH", "{}")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK")
GROUP_B_URL     = "https://investor.scrambleup.com/investing"
BASE_URL        = "https://investor.scrambleup.com"
USER_AGENT      = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# ----------------------------------------------------------------
# Discord
# ----------------------------------------------------------------
def send_all(message):
    logging.info(message)
    if not DISCORD_WEBHOOK:
        logging.warning("No DISCORD_WEBHOOK configured.")
        return
    try:
        requests.post(DISCORD_WEBHOOK, json={"content": message}, timeout=10)
        logging.info("Discord sent.")
    except Exception as e:
        logging.error("Discord failed: %s", e)

# ----------------------------------------------------------------
# Main
# ----------------------------------------------------------------
async def run_test():
    logging.info("=== TEST RUN — no schedule logic, alerts at any % ===")

    # Load auth
    try:
        auth         = json.loads(AUTH_JSON)
        cookies_list = auth.get("cookies", [])
        localstorage = auth.get("localStorage", {})
        logging.info("Loaded %d cookies, %d localStorage keys.", len(cookies_list), len(localstorage))
    except Exception as e:
        send_all(f"⚠️ Could not parse SCRAMBLE_AUTH.\nError: {e}")
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(user_agent=USER_AGENT)

        try:
            # Inject cookies
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

            page = await context.new_page()

            # Set localStorage
            await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
            if localstorage:
                await page.evaluate(
                    "(data) => { for (const [k,v] of Object.entries(data)) localStorage.setItem(k,v); }",
                    localstorage,
                )

            # Load investing page
            await page.goto(GROUP_B_URL, wait_until="domcontentloaded", timeout=60000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeoutError:
                logging.warning("networkidle timeout — continuing anyway.")

            await page.wait_for_timeout(2000)
            logging.info("Current URL: %s", page.url)

            # Session check
            page_text = (await page.content()).lower()
            if "login" in page.url.lower() or ("sign in" in page_text and "logout" not in page_text):
                send_all(
                    "🔐 SESSION EXPIRED\n\n"
                    "1. Log in to investor.scrambleup.com\n"
                    "2. Click 'ScrambleUp Auth Export' bookmark\n"
                    "3. Paste into GitHub Secret: SCRAMBLE_AUTH"
                )
                return

            # Find Group B
            await page.wait_for_selector('[class*="_group_"]', timeout=20000)
            groups = await page.query_selector_all('[class*="_group_"]')
            logging.info("Found %d group element(s).", len(groups))

            pct_text      = None
            group_context = "Group B"

            for group in groups:
                text = (await group.inner_text()).strip()
                if "group b" not in text.lower():
                    continue
                pct_el = await group.query_selector('[class*="_percentage_"]')
                if not pct_el:
                    continue
                pct_text      = (await pct_el.inner_text()).strip()
                group_context = text.splitlines()[0].strip()
                logging.info("Group B found: %s | %s", pct_text, group_context)
                break

            # Single element fallback
            if pct_text is None:
                all_pcts = await page.query_selector_all('[class*="_percentage_"]')
                if len(all_pcts) == 1:
                    pct_text = (await all_pcts[0].inner_text()).strip()
                    logging.info("Fallback: single percentage element found: %s", pct_text)

            if pct_text is None:
                send_all("⚠️ Could not find Group B percentage. Check manually.")
                return

            pct_value = float(pct_text.replace("%", "").strip())
            logging.info("Group B is %.1f%% filled.", pct_value)

            # Always alert in test mode
            send_all(
                f"🧪 TEST ALERT\n\n"
                f"📈 Group B is **{pct_value}%** filled\n"
                f"💶 {group_context}\n\n"
                f"👉 {GROUP_B_URL}"
            )

        except Exception as e:
            send_all(f"⚠️ Crash: {str(e)[:200]}")
        finally:
            await browser.close()

if __name__ == "__main__":
    asyncio.run(run_test())
