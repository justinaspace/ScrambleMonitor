import os
import json
import asyncio
import logging

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
import requests
from zoneinfo import ZoneInfo
from datetime import datetime

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

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

def send_all(message: str) -> None:
    logging.info(message)
    if not DISCORD_WEBHOOK:
        logging.warning("No DISCORD_WEBHOOK configured.")
        return
    try:
        requests.post(DISCORD_WEBHOOK, json={"content": message}, timeout=10)
    except Exception as e:
        logging.error("Failed to send Discord: %s", e)

def parse_auth():
    auth = json.loads(AUTH_JSON)
    return auth.get("cookies", []), auth.get("localStorage", {})

def extract_left_line(full_text: str) -> str:
    """Return the line containing 'left' from the group text, or empty string."""
    for line in full_text.splitlines():
        if "left" in line.lower():
            return line.strip()
    return ""

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

    return group_b_pct, group_b_context, group_a_pct, group_a_context

async def run_test():
    now = datetime.now(TZ)
    logging.info("TEST RUN at %s Vilnius.", now.strftime("%H:%M"))

    try:
        cookies_list, localstorage = parse_auth()
        logging.info("Loaded %d cookies and %d localStorage keys.", len(cookies_list), len(localstorage))
    except Exception as e:
        send_all(f"❌ TEST FAILED — could not parse SCRAMBLE_AUTH.\nError: {e}")
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

            page_text = (await page.content()).lower()
            if "login" in page.url.lower() or (
                "sign in" in page_text and "logout" not in page_text
            ):
                send_all(
                    "❌ TEST FAILED — session expired.\n\n"
                    "1. Log in to investor.scrambleup.com\n"
                    "2. Click 'ScrambleUp Auth Export' bookmark\n"
                    "3. Paste into GitHub Secret: SCRAMBLE_AUTH"
                )
                return

            group_b_pct, group_b_context, group_a_pct, group_a_context = await get_groups(page)

            if group_b_pct is None:
                send_all("❌ TEST FAILED — could not find Group B percentage.")
                return

            pct_value    = int(float(group_b_pct.replace("%", "").strip()))
            context_line = group_b_context.splitlines()[0].strip() if group_b_context else "Group B"
            b_left_line  = extract_left_line(group_b_context) if group_b_context else ""

            pct_value_a_str = "N/A"
            a_left_line     = ""
            if group_a_pct is not None:
                try:
                    pct_value_a_str = f"{int(float(group_a_pct.replace('%', '').strip()))}%"
                except ValueError:
                    pct_value_a_str = group_a_pct
            if group_a_context:
                a_left_line = extract_left_line(group_a_context)

            b_left_str = f" — {b_left_line}" if b_left_line else ""
            a_left_str = f" — {a_left_line}" if a_left_line else ""

            send_all(
                f"🙂 Group B investment is OPEN!\n"
                f"📈 Currently **{pct_value}%** filled ⚡{b_left_str}\n"
                f"💶 {context_line}\n"
                f"📊 Group A {pct_value_a_str} filled{a_left_str}\n"
                f"👉 Invest now:\n{GROUP_B_URL}"
            )

        except Exception as e:
            send_all(f"❌ TEST FAILED — unexpected error.\n{e}")
        finally:
            await browser.close()

if __name__ == "__main__":
    asyncio.run(run_test())
