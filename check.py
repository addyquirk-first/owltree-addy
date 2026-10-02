"""
Owl Tree consignment appointment watcher.

Loads each location's Acuity booking calendar in a headless browser, reads the
availability data the page itself requests, and sends a push notification via
ntfy.sh when a new opening appears before ALERT_BEFORE.
"""
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import date

from playwright.sync_api import sync_playwright

# ---- Edit these ------------------------------------------------------------
LOCATIONS = {
    "Carroll Gardens": "https://owltreekids.as.me/schedule/e06d0fee/appointment/70496014/calendar/11018044",
    "Park Slope": "https://owltreekids.as.me/schedule/e06d0fee/appointment/70496014/calendar/11018121",
}
MONTHS_AHEAD = 3  # how many months forward to check
# ----------------------------------------------------------------------------

ALERT_BEFORE = os.environ.get("ALERT_BEFORE") or "2026-12-06"
NTFY_TOPIC = os.environ["NTFY_TOPIC"]
MANUAL_RUN = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
STATE_FILE = "state.json"
DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def notify(title, message, click_url=None):
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}", data=message.encode("utf-8"), method="POST"
    )
    req.add_header("Title", title)
    req.add_header("Priority", "high")
    if click_url:
        req.add_header("Click", click_url)
    urllib.request.urlopen(req, timeout=20)


def extract_dates(obj, out):
    """Collect available dates from Acuity's availability JSON, whatever its shape."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            m = DATE_RE.match(str(key))
            if m:
                if value:  # date key with a truthy value (True / non-empty list of times)
                    out.add(m.group(1))
            else:
                extract_dates(value, out)
    elif isinstance(obj, list):
        for item in obj:
            extract_dates(item, out)
    elif isinstance(obj, str):
        m = DATE_RE.match(obj)
        if m:
            out.add(m.group(1))


def upcoming_months(n):
    today = date.today()
    y, m = today.year, today.month
    months = []
    for _ in range(n):
        months.append((y, m))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return months


def with_param(url, key, value):
    parts = urllib.parse.urlsplit(url)
    params = dict(urllib.parse.parse_qsl(parts.query))
    params[key] = value
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(params)))


def check_location(browser, name, url):
    context = browser.new_context(timezone_id="America/New_York")
    page = context.new_page()
    responses = []
    page.on(
        "response",
        lambda r: responses.append(r) if "availability" in r.url.lower() else None,
    )
    page.goto(url, wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(3000)

    dates, captured = set(), []
    for r in responses:
        try:
            data = r.json()
        except Exception:
            continue
        captured.append(r.url)
        extract_dates(data, dates)

    # The page only asks for the month it shows; ask for the next few too.
    month_urls = [u for u in captured if "month=" in u]
    if month_urls:
        base = month_urls[0]
        current = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(base).query))["month"]
        for y, m in upcoming_months(MONTHS_AHEAD):
            value = f"{y}-{m:02d}" + current[7:]  # keep the page's format (YYYY-MM or YYYY-MM-DD)
            try:
                r = context.request.get(with_param(base, "month", value))
                if r.ok:
                    extract_dates(r.json(), dates)
            except Exception as e:
                print(f"[{name}] month {value} failed: {e}")

    print(f"[{name}] availability requests seen:")
    for u in captured:
        print("   ", u)
    context.close()
    return bool(captured), dates


def main():
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    state.setdefault("open", {})
    state.setdefault("broken", {})

    today = date.today().isoformat()
    summary = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        for name, url in LOCATIONS.items():
            try:
                ok, dates = check_location(browser, name, url)
            except Exception as e:
                print(f"[{name}] error: {e}")
                ok, dates = False, set()

            if not ok:
                if not state["broken"].get(name):
                    notify(
                        "Owl Tree watcher needs a fix",
                        f"Couldn't read the {name} calendar. Check the GitHub Actions log.",
                    )
                state["broken"][name] = True
                summary.append(f"{name}: couldn't read calendar")
                continue
            state["broken"][name] = False

            current = sorted(d for d in dates if today <= d < ALERT_BEFORE)
            previous = set(state["open"].get(name, []))
            new = [d for d in current if d not in previous]
            print(f"[{name}] open before {ALERT_BEFORE}: {current or 'none'}")

            if new:
                pretty = ", ".join(date.fromisoformat(d).strftime("%a %b %-d") for d in new)
                notify(f"Owl Tree opening: {name}", f"New availability: {pretty}. Tap to book.", url)
            state["open"][name] = current
            summary.append(f"{name}: {', '.join(current) if current else 'nothing open'}")

        browser.close()

    if MANUAL_RUN:
        notify("Owl Tree watcher is running", "\n".join(summary))

    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


if __name__ == "__main__":
    main()
