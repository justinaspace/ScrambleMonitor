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
API_VERSION_HEADER = {"X-Api-Version": "1"}

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

def decode_jwt_payload(token: str) -> dict:
    try:
        payload_b64 = token.split(".")[1]
        padding = 4 - len(payload_b64) % 4
        payload_b64 += "=" * (padding % 4)
        return json.loads(base64.b64decode(payload_b64))
    except Exception:
        return {}

# ----------------------------------------------------------------
# Create a temporary access token
# ----------------------------------------------------------------
def create_temp_access_token(cookies_list):
    """
    Creates a structurally-valid JWT with full user payload.
    The real access_token contains a 'user' object — without it,
    React's userStore doesn't initialize and it redirects to /auth
    even if the token hasn't expired.
    """
    now = int(datetime.now(TZ).timestamp())
    cookie_dict = cookies_as_dict(cookies_list)

    # Extract full user data from shared_user cookie
    user_id = "25408"
    user_obj = None
    shared_user_raw = cookie_dict.get("shared_user", "")
    if shared_user_raw:
        try:
            import urllib.parse
            user_obj = json.loads(urllib.parse.unquote(shared_user_raw))
            user_id = str(user_obj.get("id", user_id))
            logging.info("Built fake token for user_id=%s (%s)",
                         user_id, user_obj.get("email", ""))
        except Exception as e:
            logging.warning("Could not parse shared_user cookie: %s", e)

    if not user_obj:
        user_obj = {
            "id": int(user_id),
            "role": "investor",
            "status": "verified",
            "is_new_user": False,
        }

    payload = {
        "token_type": "access",
        "exp": now + 28800,   # 8 hours — same as real tokens
        "iat": now,
        "jti": f"temp_{hex(now)[2:]}",
        "user_id": user_id,
        "user": user_obj,      # ← THIS is what React reads to init the user store
    }

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    header      = b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload_enc = b64(json.dumps(payload, separators=(",", ":")).encode())
    fake_sig    = b64(b"fakesig_client_does_not_verify")

    return f"{header}.{payload_enc}.{fake_sig}"

# ----------------------------------------------------------------
# Browser helpers
# ----------------------------------------------------------------
SAME_SITE_MAP = {
    "strict":         "Strict",
    "lax":            "Lax",
    "no_restriction": "None",
    "none":           "None",
}

async def setup_page(context, cookies_list, localstorage):
    # 1. Inject all cookies
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

    # 2. Intercept all /api/ requests — add X-Api-Version: 1
    async def add_version_header(route):
        headers = {**route.request.headers, **API_VERSION_HEADER}
        await route.continue_(headers=headers)
    await page.route("**/api/**", add_version_header)

    # 3. Listen for the browser's own refresh call
    #    The browser JS includes the fingerprint; we just intercept the response.
    real_token = {"value": None}
    refresh_done = asyncio.Event()

    async def on_request(request):
        if "/api/token/refresh/" in request.url:
            logging.info("Browser made refresh REQUEST → headers: %s | body: %s",
                         {k: v for k, v in request.headers.items()
                          if any(x in k.lower() for x in ("version", "finger", "auth", "content"))},
                         (request.post_data or "")[:200])

    async def on_response(response):
        if "/api/token/refresh/" in response.url:
            try:
                data = await response.json()
                logging.info("Browser refresh RESPONSE → %d | %s",
                             response.status, str(data)[:150])
                if response.status == 200:
                    token = data.get("access") or data.get("access_token")
                    if token:
                        real_token["value"] = token
                        refresh_done.set()
            except Exception as e:
                logging.warning("Could not parse refresh response: %s", e)

    page.on("request", on_request)
    page.on("response", on_response)

    # Also log relevant NET responses
    async def on_net_response(response):
        url = response.url
        if any(k in url for k in ("/api/", "invest", "group")):
            logging.info("NET %d %s", response.status, url)
    page.on("response", on_net_response)

    # 4. Inject temp token via init_script (BEFORE React loads)
    #    This passes the client-side auth check so React doesn't immediately redirect.
    #    React will then make data API calls → server returns 401 (fake token) →
    #    React's 401 interceptor calls refresh (with fingerprint) → we capture real token.
    temp_token = create_temp_access_token(cookies_list)
    token_json = json.dumps(temp_token)
    logging.info("Injecting temp access_token (3 min expiry) to allow React to initialize.")
    await page.add_init_script(f"""
        (function() {{
            try {{
                var raw = localStorage.getItem('state');
                var s = raw ? JSON.parse(raw) : {{}};
                if (!s.userStore) s.userStore = {{}};
                s.userStore.token = {{ access_token: {token_json} }};
                localStorage.setItem('state', JSON.stringify(s));
            }} catch(e) {{ console.error('Temp token injection failed:', e); }}
        }})();
    """)

    # 5. Navigate to investing page
    await page.goto(GROUP_B_URL, wait_until="domcontentloaded", timeout=60000)

    # 6. Wait for React to make a refresh call (up to 60 seconds)
    #    React will call refresh when:
    #    (a) A data API call returns 401 (fake token rejected by server) — fast
    #    (b) Pre-emptive refresh timer fires (when token is about to expire) — up to 3 min
    logging.info("Waiting for React to make its own refresh call (with fingerprint)…")
    try:
        await asyncio.wait_for(refresh_done.wait(), timeout=60)
    except asyncio.TimeoutError:
        logging.warning("React did not make a refresh call within 60 seconds.")

    # 7. If we captured the real token, inject it and reload
    if real_token["value"]:
        logging.info("Got real access_token from browser refresh! Injecting and reloading.")
        real_json = json.dumps(real_token["value"])
        await page.evaluate(f"""
            (function() {{
                try {{
                    var raw = localStorage.getItem('state');
                    var s = raw ? JSON.parse(raw) : {{}};
                    if (!s.userStore) s.userStore = {{}};
                    s.userStore.token = {{ access_token: {real_json} }};
                    localStorage.setItem('state', JSON.stringify(s));
                }} catch(e) {{}}
            }})();
        """)
        await page.goto(GROUP_B_URL, wait_until="domcontentloaded", timeout=60000)
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except PlaywrightTimeoutError:
            pass
    else:
        # No real token captured — continue with whatever state we have
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except PlaywrightTimeoutError:
            pass

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

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(user_agent=USER_AGENT)

        try:
            page = await setup_page(context, cookies_list, localstorage)

            await page.wait_for_timeout(2000)
            logging.info("Current URL: %s", page.url)

            if is_login_url(page.url):
                logging.warning("Still on login URL — auth failed.")
                send_all(f"🔐 Session expired ⚠️\n{GROUP_B_URL} ⬅️ Copy here")
                return

            try:
                pct_text, group_b_context, group_a_pct, group_a_context = await get_groups(page)
            except PlaywrightTimeoutError:
                logging.warning("Group selector not found.")
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
