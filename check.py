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

def find_cnc_id(html: str) -> str:
    """Return the numeric id of the 'Beratungs- und Antragsservice' Anliegen."""
    soup = BeautifulSoup(html, "html.parser")

    # Preferred: the <li> under the "Infostelle" heading whose text matches.
    for heading in soup.find_all(["h3", "h2", "button", "a"]):
        if SECTION in heading.get_text(" ", strip=True):
            container = heading.find_next_sibling() or heading.parent
            for li in container.find_all("li"):
                if ANLIEGEN in li.get_text(" ", strip=True) and li.get("id"):
                    return li["id"].split("-")[-1]

    # Fallback: any element mentioning the Anliegen that carries a cnc-<id> input.
    for el in soup.find_all(string=re.compile(re.escape(ANLIEGEN))):
        node = el.parent
        for _ in range(6):
            if node is None:
                break
            inp = node.find("input", attrs={"name": re.compile(r"^cnc-\d+$")})
            if inp:
                return inp["name"].split("-")[-1]
            if node.get("id", "").rsplit("-", 1)[-1].isdigit():
                return node["id"].rsplit("-", 1)[-1]
            node = node.parent

    raise FlowError(f"Could not find '{ANLIEGEN}' under '{SECTION}' on the start page.")


def location_payload(html: str) -> dict:
    """Build the POST payload that selects the Aachen Arkaden location."""
    soup = BeautifulSoup(html, "html.parser")
    loc_input = soup.find("input", {"name": "loc"})
    if not loc_input:
        raise FlowError("No location field on the location page.")

    # If there are several locations, prefer the one mentioning the hint.
    form = loc_input.find_parent("form") or soup
    buttons = form.find_all("input", {"name": "select_location"}) or soup.find_all(
        "input", {"name": "select_location"}
    )
    chosen = next(
        (b for b in buttons if LOCATION_HINT in (b.get("value") or "")),
        buttons[0] if buttons else None,
    )

    payload = {"gps_lat": "50.77", "gps_long": "6.08"}
    for hidden in form.find_all("input", {"type": "hidden"}):
        name, value = hidden.get("name"), hidden.get("value", "")
        if name and (value or name not in payload):
            payload[name] = value
    if chosen is not None:
        # The loc hidden field sits next to its button in multi-location layouts.
        sibling_loc = chosen.find_previous("input", {"name": "loc"})
        if sibling_loc is not None:
            payload["loc"] = sibling_loc.get("value", "")
        payload["select_location"] = chosen.get("value", "")
    else:
        payload["loc"] = loc_input.get("value", "")
        payload["select_location"] = "Ausländeramt Aachen - Aachen Arkaden auswählen"
    return payload


def parse_dates(html: str) -> list[date]:
    """Extract available appointment dates from the suggestion page.

    Raises FlowError unless the page is clearly the date list or the
    "no appointment" page, so a wrong page can't silently pass as "nothing free".
    """
    soup = BeautifulSoup(html, "html.parser")
    area = soup.find(id="sugg_accordion") or soup.find("summary", id="suggest_details_summary")
    if area is None:
        if "Kein freier Termin verfügbar" in html:
            return []
        raise FlowError("Suggestion page had neither the date list nor the 'no appointment' text.")

    found = set()
    for heading in area.find_all(["h3", "summary", "button"]) or [area]:
        for d, m, y in DATE_RE.findall(heading.get_text(" ", strip=True)):
            try:
                found.add(date(int(y), int(m), int(d)))
            except ValueError:
                pass
    if not found:
        raise FlowError("Found the date list but could not read any dates from it.")
    return sorted(found)


def fetch_available_dates() -> tuple[list[date], str]:
    """Walk the booking flow; return the free dates and the location-step URL."""
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "de-DE,de;q=0.9"})

    r1 = s.get(START_URL, timeout=30)
    r1.raise_for_status()
    cnc = find_cnc_id(r1.text)

    loc_url = f"{BASE}/location?mdt=89&select_cnc=1&cnc-{cnc}=1"
    r2 = s.get(loc_url, timeout=30)
    r2.raise_for_status()
    payload = location_payload(r2.text)

    s.post(loc_url, data=payload, timeout=30).raise_for_status()
    r4 = s.get(SUGGEST_URL, timeout=30)
    r4.raise_for_status()
    return parse_dates(r4.text), loc_url


# --------------------------------------------------------------- notifications

def notify(text: str, link: str = BOOKING_LINK) -> bool:
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
            headers={"Title": "Aachen Termin", "Priority": "urgent", "Click": link},
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

    dates, quick_link = fetch_available_dates()
    today = now.date()
    early = [d for d in dates if today <= d < before]
    print(f"Available: {[d.isoformat() for d in dates][:10]} | before {before}: {[d.isoformat() for d in early]}")
    print(f"Quick link: {quick_link}")

    notified = load_state(state_file)
    new = [d for d in early if d.isoformat() not in notified]

    # Forget dates that have since disappeared, so they alert again if they reappear.
    still_open = {d.isoformat() for d in early}
    notified &= still_open

    if new and not dry:
        lines = "\n".join(d.strftime("• %a %d.%m.%Y") for d in new)
        # The quick link skips the Infostelle/Anliegen clicks and lands on the
        # location step; the start page is the fallback if it ever stops working.
        msg = (
            f"🔥 Earlier Ausländeramt Aachen appointment available!\n"
            f"{ANLIEGEN} (Aachen Arkaden)\n{lines}\n\n"
            f"Book now: {quick_link}\nStart page: {BOOKING_LINK}"
        )
        if notify(msg, link=quick_link):
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
