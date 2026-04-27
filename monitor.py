import os
import json
import requests
from bs4 import BeautifulSoup

# ----------------------------------------------------------------
# Config — all values come from GitHub Secrets (never in the code)
# ----------------------------------------------------------------
COOKIES_JSON     = os.environ["SCRAMBLE_COOKIES"]
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# ⚠️ Update this URL to the exact Group B page URL from your browser
GROUP_B_URL = "https://investor.scrambleup.com/investing"

# ----------------------------------------------------------------
# Send a Telegram message to your phone
# ----------------------------------------------------------------
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message})
    except Exception as e:
        print(f"Failed to send Telegram message: {e}")

# ----------------------------------------------------------------
# Main monitoring function
# ----------------------------------------------------------------
def check_slots():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36"
    })

    # Load cookies from the GitHub Secret
    try:
        cookies = json.loads(COOKIES_JSON)
        for cookie in cookies:
            session.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain", "scrambleup.com")
            )
        print(f"Loaded {len(cookies)} cookies successfully.")
    except Exception as e:
        send_telegram(
            f"⚠️ ScrambleUp Monitor ERROR\n\n"
            f"Could not load cookies.\nError: {e}\n\n"
            f"Please check your SCRAMBLE_COOKIES secret in GitHub."
        )
        return

    # Fetch the Group B page
    try:
        response = session.get(GROUP_B_URL, timeout=30)
        print(f"Page loaded. Status code: {response.status_code}")
    except Exception as e:
        send_telegram(f"⚠️ ScrambleUp Monitor: Could not reach the website.\nError: {e}")
        return

    page_text = response.text.lower()

    # Check if session has expired (logged out)
    logged_out_signals = [
        "login", "sign in", "forgot password",
        "enter your email", "log in to continue"
    ]
    if any(kw in page_text for kw in logged_out_signals):
        send_telegram(
            "🔐 ScrambleUp Monitor: SESSION EXPIRED\n\n"
            "Your cookies have expired. Here's what to do:\n\n"
            "1. Log in to scrambleup.com (full email + SMS process)\n"
            "2. Go to the Group B page\n"
            "3. Click Cookie-Editor extension → Export → Export as JSON\n"
            "4. Go to GitHub → your repo → Settings → Secrets\n"
            "5. Update SCRAMBLE_COOKIES with the new cookies\n\n"
            "⏸ Monitoring is paused until you do this."
        )
        print("Session expired — notified user via Telegram.")
        return

    print("Session is valid. Checking for open slots...")

    # ----------------------------------------------------------------
    # ⚠️ IMPORTANT: Update these keywords to match what ScrambleUp
    # actually shows on the page when slots are open or closed.
    # See Part 7 of the guide for how to find the right words.
    # ----------------------------------------------------------------
    open_keywords = [
        "invest now", "join now", "open for investment",
        "slots available", "available", "open"
    ]
    closed_keywords = [
        "fully funded", "closed", "no slots",
        "coming soon", "fully subscribed", "waitlist"
    ]

    found_open   = any(kw in page_text for kw in open_keywords)
    found_closed = any(kw in page_text for kw in closed_keywords)

    print(f"Open signals found: {found_open}")
    print(f"Closed signals found: {found_closed}")

    if found_open and not found_closed:
        send_telegram(
            "🚨 SCRAMBLEUP ALERT 🚨\n\n"
            "Group B has OPEN INVESTMENT SLOTS!\n\n"
            "👉 Act fast — go check now:\n"
            "https://investor.scrambleup.com/investing"
        )
        print("✅ OPEN SLOTS DETECTED — Telegram alert sent!")
    else:
        print("No open slots detected. Will check again soon.")

if __name__ == "__main__":
    check_slots()
