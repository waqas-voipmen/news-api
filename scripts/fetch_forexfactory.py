"""Run on a machine with a residential IP. ForexFactory's calendar feed
(nfs.faireconomy.media) rate-limits the shared IPs of the Cloudflare Worker
proxy used to get past PythonAnywhere's outbound whitelist, so the deployed
app's cache goes stale. This fetches the current-week feed directly and writes
it to forexfactory_cache.json, published to GitHub for the deployed app to read.

ForexFactory's free feed only ever publishes the CURRENT week -- confirmed
directly against nfs.faireconomy.media, ff_calendar_lastweek.json and
ff_calendar_nextweek.json both 404 there (not a caching/rate-limit issue, the
endpoints simply don't exist), so this only fetches "week" now. app.py already
derives its own date range for the Last Week/Next Week UI filters from the
other (non-ForexFactory) sources instead.

The feed also never includes an "Actual" value (only Forecast/Previous) --
that only exists on forexfactory.com's own calendar page, which sits behind a
Cloudflare JS challenge. curl_cffi's TLS impersonation (used for Myfxbook)
doesn't clear that specific challenge since it never runs the page's
JavaScript, so this uses Playwright (a real Chromium browser) instead, which
does. Each scraped row is matched back to a feed event by (country, title) --
both are ForexFactory's own data either way, so titles line up exactly --
rather than replacing the reliable feed with a full HTML-scraped parse."""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

FF_FEEDS = {
    "week": "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
}
CALENDAR_URL = "https://www.forexfactory.com/calendar?week=this"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "*/*",
}

OUTPUT_PATH = Path(__file__).parent.parent / "forexfactory_cache.json"


def _previous_periods():
    if not OUTPUT_PATH.exists():
        return {}
    return json.loads(OUTPUT_PATH.read_text()).get("periods", {})


def _normalize(text):
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def fetch_actuals():
    """Returns {(country, normalized title): actual value} scraped from the
    real calendar page. Returns {} (not raising) on any failure -- a missing
    Actual column is a lesser problem than losing the whole week's data over
    a scrape hiccup; the caller just leaves "actual" blank for every event."""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=HEADERS["User-Agent"])
                page.goto(CALENDAR_URL, timeout=30000, wait_until="domcontentloaded")
                # Cloudflare's challenge runs its own JS and redirects once
                # cleared -- give it a few seconds before looking for the
                # actual calendar table rather than a fixed sleep.
                page.wait_for_selector("tr.calendar__row", timeout=20000)
                rows = page.query_selector_all("tr.calendar__row")

                actuals = {}
                last_country = ""
                for row in rows:
                    country_el = row.query_selector(".calendar__currency")
                    country = country_el.inner_text().strip() if country_el else last_country
                    if country:
                        last_country = country

                    title_el = row.query_selector(".calendar__event-title, .calendar__event")
                    actual_el = row.query_selector(".calendar__actual")
                    if not title_el or not actual_el:
                        continue
                    actual = actual_el.inner_text().strip()
                    if not actual:
                        continue
                    actuals[(country, _normalize(title_el.inner_text()))] = actual
                return actuals
            finally:
                browser.close()
    except Exception as e:
        print(f"actuals: failed ({e}), leaving Actual blank for this run")
        return {}


def fetch():
    result = _previous_periods()
    actuals = fetch_actuals()

    for period, url in FF_FEEDS.items():
        try:
            response = requests.get(url, headers=HEADERS, timeout=10)
            response.raise_for_status()
            events = response.json()
        except requests.RequestException as e:
            print(f"{period}: failed ({e}), keeping previous cache")
            continue
        for e in events:
            e["source"] = "ForexFactory"
            e["type"] = "calendar"
            e["link"] = None
            e["actual"] = actuals.get((e.get("country", ""), _normalize(e.get("title"))), "")
        result[period] = events
    return result


if __name__ == "__main__":
    periods = fetch()
    OUTPUT_PATH.write_text(json.dumps({"fetched_at": datetime.now(timezone.utc).isoformat(), "periods": periods}, indent=2))
    counts = {p: len(events) for p, events in periods.items()}
    with_actual = {p: sum(1 for e in events if e.get("actual")) for p, events in periods.items()}
    print(f"Wrote {counts} to {OUTPUT_PATH} ({with_actual} had an Actual value)")
