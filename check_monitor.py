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

AUTH_JSON       = os.environ.get("SCRAMBLE_AUTH", "{}")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK")
MANUAL_RUN      = os.environ.get("MANUAL_RUN", "false").lower() == "true"
GROUP_B_URL     = "https://investor.scrambleup.com/investing"
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
        return
    try:
        req_lib.post(DISCORD_WEBHOOK, json={"content": message}, timeout=10)
    except Exception as e:
        logging.error("Discord error: %s", e)

def is_last_day_of_month(now):
    return now.day == calendar.monthrange(now.year, now.month)[1]

def should_run_now(now):
    h, m, d = now.hour, now.minute, now.day
    if h >= 22 or h < 7:
        return False
    if is_last_day_of_month(now):
        return h == 18 and m == 0
    return 1 <= d <= 16

def parse_auth():
    auth = json.loads(AUTH_JSON)
    if isinstance(auth, list):
        logging.info("Cookie-Editor format — no localStorage.")
        return auth, {}, {}
    logging.info("Bookmarklet format — has localStorage.")
    return auth.get("cookies", []), auth.get("localStorage", {}), auth.get("sessionStorage", {})

def cookies_as_dict(cookies_list):
    return {c["name"]: c["value"] for c in cookies_list if c.get("name") and c.get("value")}

def check_token(token: str) -> str | None:
    """Validate token expiry and log age. Returns token if valid, None if expired."""
    try:
        payload_b64 = token.split(".")[1]
        padding = 4 - len(payload_b64) % 4
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * (padding % 4)))
        exp = payload.get("exp", 0)
        now = int(datetime.now(TZ).timestamp())
        age_h = (now - payload.get("iat", now)) / 3600
        remaining_h = (exp - now) / 3600
        logging.info("access_token age=%.1fh, expires in %.1fh", age_h, remaining_h)
        if exp > now:
            return token
        logging.warning("access_token is expired.")
        return None
    except Exception as e:
        logging.warning("Could not validate token: %s", e)
        return None

def extract_access_token(auth: dict) -> str | None:
    """Extract access_token — tries top-level key (new bookmarklet), then localStorage.state"""
    # New bookmarklet exports token at top level as "access_token"
    direct = auth.get("access_token")
    if direct:
        logging.info("Found access_token at top level.")
        return check_token(direct)
    # Old location: localStorage.state.userStore.token.access_token
    localstorage = auth if not auth.get("cookies") else {}
    raw = localstorage.get("state")
    if not raw and isinstance(auth, dict):
        ls = auth.get("localStorage", {})
        raw = ls.get("state") if ls else None
    if raw:
        try:
            state = json.loads(raw) if isinstance(raw, str) else raw
            token = state.get("userStore", {}).get("token", {}).get("access_token")
            if token:
                logging.info("Found access_token in localStorage.state")
                return check_token(token)
        except Exception as e:
            logging.warning("Could not parse state: %s", e)
    return None

SAME_SITE_MAP = {"strict": "Strict", "lax": "Lax", "no_restriction": "None", "none": "None"}

async def scrape(context, cookies_list, access_token: str):
    """Inject cookies + access_token, navigate, scrape group percentages."""
    # Inject cookies
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

    # Inject access_token into localStorage before React loads
    token_json = json.dumps(access_token)
    await page.add_init_script(f"""
        (function() {{
            try {{
                var s = JSON.parse(localStorage.getItem('state') || '{{}}');
                s.userStore = s.userStore || {{}};
                s.userStore.token = {{ access_token: {token_json} }};
                localStorage.setItem('state', JSON.stringify(s));
            }} catch(e) {{ console.error('Token inject error:', e); }}
        }})();
    """)

    await page.goto(GROUP_B_URL, wait_until="domcontentloaded", timeout=60000)
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeoutError:
        pass
    await page.wait_for_timeout(2000)

    logging.info("URL after load: %s", page.url)

    if any(t in page.url.lower() for t in ("/auth", "login", "signin")):
        await page.close()
        return None, None, None, None

    group_selector      = '[class*="_group_"]'
    percentage_selector = '[class*="_percentage_"]'
    try:
        await page.wait_for_selector(group_selector, timeout=20000)
    except PlaywrightTimeoutError:
        logging.warning("Group selector not found.")
        await page.close()
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
        cookies_list, localstorage, _ = parse_auth()
        logging.info("Loaded %d cookies, %d localStorage keys.", len(cookies_list), len(localstorage))
    except Exception as e:
        send_all(f"⚠️ Could not parse SCRAMBLE_AUTH.\nError: {e}")
        return

    # Get access_token — new bookmarklet puts it at top level
    raw_auth = json.loads(AUTH_JSON)
    access_token = extract_access_token(raw_auth if isinstance(raw_auth, dict) else {})
    if not access_token:
        logging.warning("No valid access_token in localStorage.")
        send_all(f"🔐 Session expired ⚠️\n{GROUP_B_URL} ⬅️ Copy here")
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(user_agent=USER_AGENT)
        try:
            pct_text, group_b_ctx, group_a_pct, group_a_ctx = await scrape(
                context, cookies_list, access_token
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
