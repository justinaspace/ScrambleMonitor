import os
import json
import base64
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# ----------------------------------------------------------------
# Notification
# ----------------------------------------------------------------
def send_all(message: str) -> None:
    logging.info(message)
    if not DISCORD_WEBHOOK:
        return
    try:
        req_lib.post(DISCORD_WEBHOOK, json={"content": message}, timeout=10)
    except Exception as e:
        logging.error("Discord error: %s", e)

# ----------------------------------------------------------------
# Schedule
# ----------------------------------------------------------------
def is_last_day_of_month(now):
    return now.day == calendar.monthrange(now.year, now.month)[1]

def should_run_now(now):
    h, m, d = now.hour, now.minute, now.day
    if h >= 22 or h < 7:
        return False
    if is_last_day_of_month(now):
        return h == 18 and m == 0
    return 1 <= d <= 16

# ----------------------------------------------------------------
# Auth
# ----------------------------------------------------------------
def parse_auth():
    auth = json.loads(AUTH_JSON)
    if isinstance(auth, list):
        logging.info("Cookie-Editor format detected.")
        return auth, {}, {}
    return auth.get("cookies", []), auth.get("localStorage", {}), auth.get("sessionStorage", {})

def cookies_as_dict(cookies_list):
    return {c["name"]: c["value"] for c in cookies_list if c.get("name") and c.get("value")}

def make_expired_token(cookies_list):
    """
    Build a fake JWT that is already expired (-60s) but has full user data.
    React will: (1) see user data → stay on /investing, (2) detect expiry → call /api/token/refresh/
    We intercept that call and inject the real refresh_token into the body.
    """
    now = int(datetime.now(TZ).timestamp())
    cookie_dict = cookies_as_dict(cookies_list)
    user_obj = {"id": 25408, "role": "investor", "status": "verified", "is_new_user": False}
    shared_raw = cookie_dict.get("shared_user", "")
    if shared_raw:
        try:
            import urllib.parse
            parsed = json.loads(urllib.parse.unquote(shared_raw))
            user_obj = parsed
            logging.info("Built token for user %s", parsed.get("email", ""))
        except Exception:
            pass

    payload = {
        "token_type": "access",
        "exp": now - 60,   # EXPIRED — triggers React refresh
        "iat": now - 28860,
        "jti": f"fake_{hex(now)[2:]}",
        "user_id": str(user_obj.get("id", "25408")),
        "user": user_obj,
    }

    def b64url(data):
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    h = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    p = b64url(json.dumps(payload, separators=(",", ":")).encode())
    s = b64url(b"fakesig")
    return f"{h}.{p}.{s}"

# ----------------------------------------------------------------
# Browser
# ----------------------------------------------------------------
SAME_SITE_MAP = {"strict": "Strict", "lax": "Lax", "no_restriction": "None", "none": "None"}

async def get_percentage(context, cookies_list):
    """
    Main auth flow:
    1. Inject cookies + expired fake token
    2. Route-intercept /api/token/refresh/ to add refresh_token body
    3. React calls refresh → we get real token → reload → scrape
    """
    refresh_token_val = cookies_as_dict(cookies_list).get("refresh_token", "")
    if not refresh_token_val:
        logging.warning("No refresh_token in cookies!")

    # --- Inject cookies ---
    cookies = []
    for c in cookies_list:
        if not c.get("name") or not c.get("value"):
            continue
        domain = c.get("domain", "investor.scrambleup.com")
        if c.get("hostOnly", False):
            domain = domain.lstrip(".")
        cookie = {
            "name": c["name"], "value": c["value"],
            "domain": domain, "path": c.get("path", "/"),
            "secure": c.get("secure", False), "httpOnly": c.get("httpOnly", False),
        }
        ss = (c.get("sameSite") or "").lower()
        if ss in SAME_SITE_MAP:
            cookie["sameSite"] = SAME_SITE_MAP[ss]
        if c.get("expirationDate"):
            cookie["expires"] = int(c["expirationDate"])
        cookies.append(cookie)
    if cookies:
        await context.add_cookies(cookies)
        logging.info("Injected %d cookies.", len(cookies))

    page = await context.new_page()

    # --- Capture real token from refresh response ---
    real_token = {"value": None}
    refresh_done = asyncio.Event()

    # Route interceptor — fires BEFORE request hits server
    async def intercept_refresh(route):
        logging.info("### REFRESH INTERCEPTED — injecting body ###")
        headers = dict(route.request.headers)
        headers["x-api-version"] = "3"
        headers["content-type"] = "application/json"
        body = json.dumps({"refresh": refresh_token_val})
        logging.info("Body being sent: %s", body[:80])
        await route.continue_(headers=headers, post_data=body)

    await page.route("**/token/refresh/**", intercept_refresh)
    logging.info("Route interceptor registered for **/token/refresh/**")

    # Response listener — captures the token after server responds
    async def on_response(response):
        if "/api/token/refresh/" in response.url:
            try:
                data = await response.json()
                logging.info("Refresh response %d: %s", response.status, str(data)[:150])
                if response.status == 200:
                    token = data.get("access") or data.get("access_token")
                    if token:
                        real_token["value"] = token
                        refresh_done.set()
                        logging.info("Real access_token captured!")
            except Exception as e:
                logging.warning("Could not parse refresh response: %s", e)
    page.on("response", on_response)

    # --- Inject expired token BEFORE React loads ---
    expired_token = make_expired_token(cookies_list)
    token_json = json.dumps(expired_token)
    logging.info("Injecting EXPIRED token to trigger React refresh.")
    await page.add_init_script(f"""
        (function() {{
            try {{
                var s = JSON.parse(localStorage.getItem('state') || '{{}}');
                s.userStore = s.userStore || {{}};
                s.userStore.token = {{ access_token: {token_json} }};
                localStorage.setItem('state', JSON.stringify(s));
                localStorage.setItem('auth_refresh_session', '1');
                var p = JSON.parse(atob({token_json}.split('.')[1].replace(/-/g,'+').replace(/_/g,'/')));
                console.log('Token exp:', p.exp, 'now:', Math.floor(Date.now()/1000), 'expired:', p.exp < Date.now()/1000);
            }} catch(e) {{ console.error('Init error:', e); }}
        }})();
    """)

    # --- Navigate ---
    await page.goto(GROUP_B_URL, wait_until="domcontentloaded", timeout=60000)

    # --- Wait for refresh call (up to 30 seconds) ---
    logging.info("Waiting for React refresh call...")
    try:
        await asyncio.wait_for(refresh_done.wait(), timeout=30)
    except asyncio.TimeoutError:
        logging.warning("No refresh call in 30s.")

    if not real_token["value"]:
        logging.warning("No real token captured. Checking URL...")
        await page.wait_for_timeout(2000)
        logging.info("Current URL: %s", page.url)
        return None, None, None, None

    # --- Inject real token and reload ---
    logging.info("Injecting real token and reloading...")
    real_json = json.dumps(real_token["value"])
    await page.evaluate(f"""
        (function() {{
            var s = JSON.parse(localStorage.getItem('state') || '{{}}');
            s.userStore = s.userStore || {{}};
            s.userStore.token = {{ access_token: {real_json} }};
            localStorage.setItem('state', JSON.stringify(s));
        }})();
    """)
    await page.goto(GROUP_B_URL, wait_until="domcontentloaded", timeout=60000)
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeoutError:
        pass
    await page.wait_for_timeout(2000)
    logging.info("After reload URL: %s", page.url)

    # --- Check for login redirect ---
    if any(t in page.url.lower() for t in ("/auth", "login", "signin")):
        logging.warning("Redirected to auth after token injection.")
        return None, None, None, None

    # --- Scrape groups ---
    group_selector      = '[class*="_group_"]'
    percentage_selector = '[class*="_percentage_"]'
    try:
        await page.wait_for_selector(group_selector, timeout=20000)
    except PlaywrightTimeoutError:
        logging.warning("Group selector not found after real token injection.")
        return None, None, None, None

    groups = await page.query_selector_all(group_selector)
    logging.info("Found %d group element(s).", len(groups))

    group_a_pct = group_a_ctx = group_b_pct = group_b_ctx = None
    for g in groups:
        text = (await g.inner_text()).strip()
        pct_el = await g.query_selector(percentage_selector)
        if not pct_el:
            continue
        pct = (await pct_el.inner_text()).strip()
        if "group b" in text.lower():
            group_b_pct, group_b_ctx = pct, text
        elif "group a" in text.lower():
            group_a_pct, group_a_ctx = pct, text

    if group_b_pct is None:
        all_pcts = await page.query_selector_all(percentage_selector)
        if len(all_pcts) == 1:
            group_b_pct = (await all_pcts[0].inner_text()).strip()

    await page.close()
    return group_b_pct, group_b_ctx, group_a_pct, group_a_ctx

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
        logging.info("Manual run triggered.")

    logging.info("Running at %s Vilnius, day %s.", now.strftime("%H:%M"), now.day)

    try:
        cookies_list, localstorage, sessionstorage = parse_auth()
        logging.info("Loaded %d cookies.", len(cookies_list))
    except Exception as e:
        send_all(f"⚠️ Could not parse SCRAMBLE_AUTH.\nError: {e}")
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(user_agent=USER_AGENT)
        try:
            pct_text, group_b_ctx, group_a_pct, group_a_ctx = await get_percentage(
                context, cookies_list
            )

            if pct_text is None:
                send_all(f"🔐 Session expired ⚠️\n{GROUP_B_URL} ⬅️ Copy here")
                return

            try:
                pct_value = int(float(pct_text.replace("%", "").strip()))
            except ValueError:
                send_all(f"⚠️ Unexpected format: '{pct_text}'")
                return

            pct_a_str = "N/A"
            ctx_a_line = ""
            if group_a_pct:
                try:
                    pct_a_str = f"{int(float(group_a_pct.replace('%','').strip()))}%"
                except ValueError:
                    pct_a_str = group_a_pct
            if group_a_ctx:
                ctx_a_line = group_a_ctx.splitlines()[0].strip()

            logging.info("Group B: %d%%", pct_value)

            if pct_value == 0:
                logging.info("Group B 0%% — not open yet.")
            elif 0 < pct_value < 100:
                ctx_b_line = group_b_ctx.splitlines()[0].strip() if group_b_ctx else "Group B"
                send_all(
                    f"🙂 OPEN investment in Group B!\n"
                    f"📈 Currently **{pct_value}%** filled ⚡\n"
                    f"💸 {ctx_b_line}\n"
                    f"📊 Group A - {pct_a_str} filled\n"
                    f"💵 {ctx_a_line}\n"
                    f"{GROUP_B_URL} ⬅️ Invest now\n"
                )
                logging.info("ALERT SENT — %d%% full.", pct_value)
            else:
                logging.info("Group B 100%% full.")

        except Exception as e:
            logging.exception("Unexpected error: %s", e)
            send_all(f"⚠️ Something unexpected\n🔐 Update Cookies\n{GROUP_B_URL} ⬅️ Copy here")
        finally:
            await browser.close()

if __name__ == "__main__":
    asyncio.run(check_slots())
