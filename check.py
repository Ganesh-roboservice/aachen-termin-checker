"""
Aachen Ausländerbehörde appointment checker.

Walks the StädteRegion Aachen TEVIS booking flow for
  Infostelle -> "Beratungs- und Antragsservice"
and sends a notification (Telegram and/or ntfy) when a date earlier than
BEFORE_DATE becomes available.

Environment variables:
  BEFORE_DATE          Only alert for dates before this day (YYYY-MM-DD). Default 2026-12-23.
  TELEGRAM_BOT_TOKEN   Optional. Telegram bot token from @BotFather.
  TELEGRAM_CHAT_ID     Optional. Your chat id.
  NTFY_TOPIC           Optional. ntfy.sh topic name (alternative to Telegram).
  STATE_FILE           Where already-notified dates are stored. Default state.json.

Flags:
  --test-notify        Send a test message and exit.
  --dry-run            Check and print, but don't notify or write state.
"""

import json
import os
import re
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BASE = "https://termine.staedteregion-aachen.de/auslaenderamt"
START_URL = f"{BASE}/select2?md=1"
SUGGEST_URL = f"{BASE}/suggest"
BOOKING_LINK = START_URL

# Scheduled runs only check between these hours (Berlin time). The cron in the
# workflow is UTC, so it fires a bit wider and this keeps the window exact
# across summer/winter time.
BERLIN = ZoneInfo("Europe/Berlin")
ACTIVE_HOURS = (6, 22)

SECTION = "Infostelle"
ANLIEGEN = "Beratungs- und Antragsservice"
LOCATION_HINT = "Arkaden"  # "Ausländeramt Aachen - Aachen Arkaden, Trierer Straße 1"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")


class FlowError(Exception):
    """The booking site did not look as expected (layout probably changed)."""


# ---------------------------------------------------------------- booking flow

def find_concern(html: str) -> tuple[str, str]:
    """Return (mdt, cnc id) for SECTION -> ANLIEGEN on the start page.

    Matches the section heading and the Anliegen name exactly, because other
    sections (e.g. Fachhochschule Aachen) mention the same words in tooltips.
    """
    soup = BeautifulSoup(html, "html.parser")

    form = soup.find("form", id="cnc-select-form")
    mdt_input = form.find("input", {"name": "mdt"}) if form else None
    if mdt_input is None or not mdt_input.get("value"):
        raise FlowError("No mdt field in the Anliegen form on the start page.")

    heading = soup.find(
        "h3",
        id=re.compile(r"^header_concerns_accordion-"),
        string=lambda s: s and s.strip() == SECTION,
    )
    if heading is None:
        raise FlowError(f"No '{SECTION}' section on the start page.")
    content = soup.find(id=heading["id"].replace("header_", "content_", 1))
    inp = content.find("input", attrs={"data-tevis-cncname": ANLIEGEN}) if content else None
    if inp is None or not inp.get("data-tevis-cncid"):
        raise FlowError(f"No '{ANLIEGEN}' in the '{SECTION}' section.")

    print(f"Anliegen: {SECTION} -> {ANLIEGEN} (cnc {inp['data-tevis-cncid']}, mdt {mdt_input['value']})")
    return mdt_input["value"], inp["data-tevis-cncid"]


def location_payload(html: str) -> dict:
    """Build the POST payload that selects the Aachen Arkaden location.

    The page has one form per location (plus a map form without the name), so
    pick the form whose own text names the location.
    """
    soup = BeautifulSoup(html, "html.parser")
    for form in soup.find_all("form"):
        button = form.find("input", {"name": "select_location"})
        if form.find("input", {"name": "loc"}) and button and LOCATION_HINT in form.get_text(" "):
            payload = {
                inp["name"]: inp.get("value", "")
                for inp in form.find_all("input", {"type": "hidden"})
                if inp.get("name")
            }
            payload["select_location"] = button.get("value", "")
            print(f"Location: {LOCATION_HINT} (loc {payload.get('loc')})")
            return payload
    raise FlowError(f"No '{LOCATION_HINT}' location on the location page.")


def check_selection(soup: BeautifulSoup) -> None:
    """Make sure the suggestion page is for our Anliegen and location.

    The page's "Übersicht zu Ihrem Termin" box lists what the session selected.
    """
    box = soup.find(id="infobox_content")
    text = box.get_text(" ", strip=True) if box else ""
    if ANLIEGEN not in text or LOCATION_HINT not in text:
        raise FlowError(f"Suggestion page is not for {ANLIEGEN} at {LOCATION_HINT}: {text[:200]!r}")


def parse_dates(html: str) -> list[date]:
    """Extract the days with at least one bookable slot from the suggestion page.

    Raises FlowError unless the page is clearly the date list or the
    "no appointment" page, so a wrong page can't silently pass as "nothing free".
    """
    soup = BeautifulSoup(html, "html.parser")
    check_selection(soup)
    area = soup.find(id="sugg_accordion")
    if area is None:
        if "Kein freier Termin verfügbar" in html:
            return []
        raise FlowError("Suggestion page had neither the date list nor the 'no appointment' text.")

    # Each day is an <h3 title="Mittwoch, 23.12.2026"> followed by a panel of
    # slots; greyed-out slots are disabled buttons, bookable ones are forms.
    days, found = 0, set()
    for heading in area.find_all("h3"):
        match = DATE_RE.search(heading.get_text(" ", strip=True))
        if not match:
            continue
        days += 1
        panel = heading.find_next_sibling()
        if panel is not None and panel.find("form", class_="suggestion_form"):
            d, m, y = match.groups()
            found.add(date(int(y), int(m), int(d)))
    if days == 0:
        raise FlowError("Found the date list but could not read any dates from it.")
    return sorted(found)


def fetch_available_dates() -> list[date]:
    """Walk the booking flow and return the days with bookable slots."""
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "de-DE,de;q=0.9"})

    r1 = s.get(START_URL, timeout=30)
    r1.raise_for_status()
    mdt, cnc = find_concern(r1.text)

    # Same GET the start page's "Weiter" button sends.
    loc_url = f"{BASE}/location?mdt={mdt}&select_cnc=1&cnc-{cnc}=1"
    r2 = s.get(loc_url, timeout=30)
    r2.raise_for_status()
    payload = location_payload(r2.text)

    s.post(loc_url, data=payload, timeout=30).raise_for_status()
    r4 = s.get(SUGGEST_URL, timeout=30)
    r4.raise_for_status()
    return parse_dates(r4.text)


# --------------------------------------------------------------- notifications

def notify(text: str) -> bool:
    """Send text via every configured channel; True if at least one succeeded."""
    sent = False
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if token and chat:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat, "text": text, "disable_web_page_preview": "true"},
            timeout=20,
        )
        print("Telegram:", r.status_code)
        sent = sent or r.ok
    topic = os.getenv("NTFY_TOPIC")
    if topic:
        r = requests.post(
            f"https://ntfy.sh/{topic}",
            data=text.encode("utf-8"),
            headers={"Title": "Aachen Termin", "Priority": "urgent", "Click": BOOKING_LINK},
            timeout=20,
        )
        print("ntfy:", r.status_code)
        sent = sent or r.ok
    if not sent:
        print("No notification channel configured or all failed.")
    return sent


# ------------------------------------------------------------------------ main

def load_state(path: str) -> set[str]:
    try:
        with open(path, encoding="utf-8") as f:
            return set(json.load(f).get("notified", []))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_state(path: str, notified: set[str]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"notified": sorted(notified)}, f, indent=2)
        f.write("\n")


def main() -> int:
    args = set(sys.argv[1:])
    if "--test-notify" in args:
        ok = notify(f"✅ Test from Aachen Termin checker. Booking page: {BOOKING_LINK}")
        return 0 if ok else 1

    now = datetime.now(BERLIN)
    if os.getenv("GITHUB_EVENT_NAME") == "schedule" and not (
        ACTIVE_HOURS[0] <= now.hour < ACTIVE_HOURS[1]
    ):
        print(f"Outside active hours ({now:%H:%M} Berlin), skipping.")
        return 0

    dry = "--dry-run" in args
    before = datetime.strptime(os.getenv("BEFORE_DATE", "2026-12-23"), "%Y-%m-%d").date()
    state_file = os.getenv("STATE_FILE", "state.json")

    dates = fetch_available_dates()
    today = now.date()
    early = [d for d in dates if today <= d < before]
    print(f"Available: {[d.isoformat() for d in dates][:10]} | before {before}: {[d.isoformat() for d in early]}")

    notified = load_state(state_file)
    new = [d for d in early if d.isoformat() not in notified]

    # Forget dates that have since disappeared, so they alert again if they reappear.
    still_open = {d.isoformat() for d in early}
    notified &= still_open

    if new and not dry:
        lines = "\n".join(d.strftime("• %a %d.%m.%Y") for d in new)
        # Deep links into the flow fail without the site's session cookie
        # ("Kein gültiger Mandant"), so the alert links to the start page.
        msg = (
            f"🔥 Earlier Ausländeramt Aachen appointment available!\n"
            f"{SECTION} → {ANLIEGEN} (Aachen Arkaden)\n{lines}\n\nBook now: {BOOKING_LINK}"
        )
        if notify(msg):
            notified |= {d.isoformat() for d in new}

    if not dry:
        save_state(state_file, notified)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except FlowError as e:
        print("FLOW ERROR:", e)
        sys.exit(2)
