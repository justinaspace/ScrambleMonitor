import os
import json
import asyncio
import logging
import calendar
import requests as req_lib
from datetime import datetime
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# ----------------------------------------------------------------
# Config
# ----------------------------------------------------------------
AUTH_JSON       = os.environ.get("SCRAMBLE_AUTH", "{}")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK")
MANUAL_RUN      = os.environ.get("MANUAL_RUN", "false").lower() == "true"
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
        req_lib.post(DISCORD_WEBHOOK, json={"content": message}, timeout=10)
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
        logging.info("Night skip: %s Vilnius.", now.strftime("%H:%M"))
        return False
    if is_last_day_of_month(now):
        if h == 18 and m == 0:
            return True
        return False
    if 1 <= d <= 16:
        return True
    return False

# ----------------------------------------------------------------
# Auth
# ----------------------------------------------------------------
def parse_auth():
    auth = json.loads(AUTH_JSON)
    if isinstance(auth, list):
        logging.info("Detected Cookie-Editor format (plain array).")
        return auth, {}, {}
    return auth.get("cookies", []), auth.get("localStorage", {}), auth.get("sessionStorage", {})

def cookies_as_dict(cookies_list):
    return {c["name"]: c["value"] for c in cookies_list if c.get("name") and c.get("value")}

# ----------------------------------------------------------------
# Token refresh — tries all common API version header combinations
# ----------------------------------------------------------------
def try_api_refresh(cookies_list):
    """
    Server requires an API version header.
    Error was: {"message": "Missing API version header"}
    We try all common header name + value combinations until one works.
    Also returns the winning header so Playwright can use it too.
    """
    cookie_dict = cookies_as_dict(cookies_list)
    refresh_token_val = cookie_dict.get("refresh_token")
    if not refresh_token_val:
        logging.warning("No refresh_token cookie found.")
        return None, None

    base_headers = {
        "User-Agent":   USER_AGENT,
        "Referer":      BASE_URL,
        "Origin":       BASE_URL,
        "Content-Type": "application/json",
    }

    # All plausible version header name + value combinations
    version_candidates = [
        ("X-Api-Version", "1"),
        ("X-Api-Version", "2"),
        ("X-Api-Version", "3"),
        ("X-API-Version", "1"),
        ("X-API-Version", "2"),
        ("Api-Version",   "1"),
        ("Api-Version",   "2"),
        ("X-Version",     "1"),
        ("X-Version",     "2"),
        ("Accept",        "application/json; version=1"),
        ("Accept",        "application/json; version=2"),
        ("X-App-Version", "1"),
        ("X-App-Version", "2"),
    ]

    # Try URL versioning too
    refresh_urls = [
        f"{BASE_URL}/api/token/refresh/",
        f"{BASE_URL}/api/v1/token/refresh/",
        f"{BASE_URL}/api/v2/token/refresh/",
    ]

    for url in refresh_urls:
        for header_name, header_value in version_candidates:
            headers = {**base_headers, header_name: header_value}
            try:
                # Try with body first (most common for JWT refresh)
                r = req_lib.post(
                    url,
                    json={"refresh": refresh_token_val},
                    cookies=cookie_dict,
                    headers=headers,
                    timeout=10,
                )
                logging.info(
                    "Refresh %s [%s: %s] → %d | %s",
                    url.split("/api")[-1], header_name, header_value,
                    r.status_code, r.text[:80]
                )
                if r.status_code == 200:
                    data = r.json()
                    token = data.get("access") or data.get("access_token")
                    if token:
                        winning_header = (header_name, header_value)
                        logging.info(
                            "SUCCESS: access_token via [%s: %s]",
                            header_name, header_value
                        )
                        return token, winning_header
            except Exception as e:
                logging.warning("Refresh attempt error: %s", e)

    logging.warning("All refresh attempts failed.")
    return None, None

# ----------------------------------------------------------------
# Browser helpers
# ----------------------------------------------------------------
SAME_SITE_MAP = {
    "strict":         "Strict",
    "lax":            "Lax",
    "no_restriction": "None",
    "none":           "None",
}

async def setup_page(context, cookies_list, localstorage, sessionstorage,
                     access_token=None, api_version_header=None):
    # Inject all cookies with full attributes
    cookies = []
    for c in cookies_list:
        if not c.get("name") or not c.get("value"):
            continue
        domain = c.get("domain", "investor.scrambleup.com")
        if c.get("hostOnly", False):
            domain = domain.lstrip(".")
        cookie = {
            "name":     c["name"],
            "value":    c["value"],
            "domain":   domain,
            "path":     c.get("path", "/"),
            "secure":   c.get("secure", False),
            "httpOnly": c.get("httpOnly", False),
        }
        same_site = (c.get("sameSite") or "").lower()
        if same_site in SAME_SITE_MAP:
            cookie["sameSite"] = SAME_SITE_MAP[same_site]
        if c.get("expirationDate"):
            cookie["expires"] = int(c["expirationDate"])
        cookies.append(cookie)

    if cookies:
        await context.add_cookies(cookies)
        logging.info("Injected %d cookies.", len(cookies))

    page = await context.new_page()

    # Intercept all API calls and add the version header
    # This ensures the React app's own API calls also work
    if api_version_header:
        h_name, h_value = api_version_header
        logging.info("Intercepting API calls to add [%s: %s]", h_name, h_value)
        async def add_version_header(route):
            headers = {**route.request.headers, h_name: h_value}
            await route.continue_(headers=headers)
        await page.route("**/api/**", add_version_header)

    # Log relevant network responses
    async def on_response(response):
        url = response.url
        if any(k in url for k in ("/api/", "token", "invest", "group")):
            logging.info("NET %d %s", response.status, url)
    page.on("response", on_response)

    # Inject access_token via init_script BEFORE React loads
    if access_token:
        logging.info("Injecting access_token via init_script.")
        token_json = json.dumps(access_token)
        await page.add_init_script(f"""
            (function() {{
                try {{
                    var raw = localStorage.getItem('state');
                    var state = raw ? JSON.parse(raw) : {{}};
                    if (!state.userStore) state.userStore = {{}};
                    state.userStore.token = {{ access_token: {token_json} }};
                    localStorage.setItem('state', JSON.stringify(state));
                }} catch(e) {{ console.error('Token injection failed:', e); }}
            }})();
        """)

    # Inject other localStorage keys from export (skip 'state' — we set it above)
    if localstorage:
        ls_json = json.dumps(
            {k: v for k, v in localstorage.items() if k != "state"}
        )
        await page.add_init_script(f"""
            (function() {{
                var data = {ls_json};
                for (var k in data) {{ localStorage.setItem(k, data[k]); }}
            }})();
        """)

    # Navigate directly to the investing page
    await page.goto(GROUP_B_URL, wait_until="domcontentloaded", timeout=60000)
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeoutError:
        logging.warning("networkidle timeout — continuing.")

    return page


async def get_groups(page):
    group_selector      = '[class*="_group_"]'
    percentage_selector = '[class*="_percentage_"]'

    await page.wait_for_selector(group_selector, timeout=20000)
    groups = await page.query_selector_all(group_selector)
    logging.info("Found %d group element(s).", len(groups))

    group_a_pct = group_a_context = group_b_pct = group_b_context = None

    for group in groups:
        text = (await group.inner_text()).strip()
        pct_el = await group.query_selector(percentage_selector)
        if not pct_el:
            continue
        pct_text = (await pct_el.inner_text()).strip()
        if "group b" in text.lower():
            group_b_pct, group_b_context = pct_text, text
            logging.info("Group B: %s | %s", pct_text, text[:60])
        elif "group a" in text.lower():
            group_a_pct, group_a_context = pct_text, text
            logging.info("Group A: %s | %s", pct_text, text[:60])

    if group_b_pct is None:
        all_pcts = await page.query_selector_all(percentage_selector)
        if len(all_pcts) == 1:
            group_b_pct = (await all_pcts[0].inner_text()).strip()

    return group_b_pct, group_b_context, group_a_pct, group_a_context


def is_login_url(url: str) -> bool:
    lowered = url.lower()
    return any(t in lowered for t in ("login", "signin", "sign-in", "auth/", "/auth"))


# ----------------------------------------------------------------
# Main
# ----------------------------------------------------------------
async def check_slots():
    now = datetime.now(TZ)

    if not MANUAL_RUN and is_last_day_of_month(now) and now.hour == 12:
        send_all("⚠️ Scramble Group B Bot.\nRESERVE the Group B funds/slots")

    if not MANUAL_RUN and not should_run_now(now):
        return

    if MANUAL_RUN:
        logging.info("Manual run triggered — skipping schedule check.")

    logging.info("Running at %s Vilnius, day %s.", now.strftime("%H:%M"), now.day)

    try:
        cookies_list, localstorage, sessionstorage = parse_auth()
        logging.info(
            "Loaded %d cookies, %d localStorage, %d sessionStorage keys.",
            len(cookies_list), len(localstorage), len(sessionstorage)
        )
    except Exception as e:
        send_all(f"⚠️ Could not parse SCRAMBLE_AUTH secret.\nError: {e}")
        return

    access_token, api_version_header = try_api_refresh(cookies_list)
    if not access_token:
        logging.warning("No access_token obtained — proceeding without it.")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(user_agent=USER_AGENT)

        try:
            page = await setup_page(
                context, cookies_list, localstorage, sessionstorage,
                access_token, api_version_header
            )

            await page.wait_for_timeout(2000)
            logging.info("Current URL: %s", page.url)

            if is_login_url(page.url):
                logging.warning("Still on login URL — auth failed.")
                send_all(f"🔐 Session expired ⚠️\n{GROUP_B_URL} ⬅️ Copy here")
                return

            try:
                pct_text, group_b_context, group_a_pct, group_a_context = await get_groups(page)
            except PlaywrightTimeoutError:
                logging.warning("Group selector not found — session expired.")
                send_all(f"🔐 Session expired ⚠️\n{GROUP_B_URL} ⬅️ Copy here")
                return

            if pct_text is None:
                send_all("⚠️ Could not find Group B percentage. Please check manually.")
                return

            try:
                pct_value = int(float(pct_text.replace("%", "").strip()))
            except ValueError:
                send_all(f"⚠️ Unexpected percentage format: '{pct_text}'")
                return

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
                logging.info("Group B is 0%% — round not open yet.")
            elif 0 < pct_value < 100:
                context_line = group_b_context.splitlines()[0].strip() if group_b_context else "Group B"
                send_all(
                    f"🙂 OPEN investment in Group B!\n"
                    f"📈 Currently **{pct_value}%** filled ⚡\n"
                    f"💸 {context_line}\n"
                    f"📊 Group A - {pct_value_a_str} filled\n"
                    f"💵 {context_line_a}\n"
                    f"{GROUP_B_URL} ⬅️ Invest now\n"
                )
                logging.info("ALERT SENT — Group B is %d%% full.", pct_value)
            else:
                logging.info("Group B is 100%% full.")

        except Exception as e:
            logging.exception("Unexpected error: %s", e)
            send_all(
                f"⚠️ Something unexpected\n"
                f"🔐 Try to update the Cookies\n"
                f"{GROUP_B_URL} ⬅️ Copy here"
            )
        finally:
            await browser.close()

if __name__ == "__main__":
    asyncio.run(check_slots())
