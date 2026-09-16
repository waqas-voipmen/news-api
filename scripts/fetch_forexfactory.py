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
from playwright_stealth import Stealth

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
# Real Chrome + stealth patches + a residential IP still didn't clear
# Cloudflare's challenge here (confirmed by hand) -- if this file exists
# (git-ignored; export it once from a real, already-cleared browser session
# with something like the Cookie-Editor extension), its cookies are loaded
# into the browser context before navigating so Cloudflare sees an
# already-verified session instead of a fresh one. cf_clearance-style cookies
# are typically good for hours, not indefinitely, so this needs re-exporting
# every so often when the scrape starts failing again.
COOKIES_PATH = Path(__file__).parent.parent / "forexfactory_cookies.json"


def _previous_periods():
    if not OUTPUT_PATH.exists():
        return {}
    return json.loads(OUTPUT_PATH.read_text()).get("periods", {})


_SAME_SITE = {"no_restriction": "None", "lax": "Lax", "strict": "Strict", "unspecified": "Lax"}


def _load_cookies():
    if not COOKIES_PATH.exists():
        return None
    cookies = json.loads(COOKIES_PATH.read_text())
    for c in cookies:
        # Cookie-Editor (and most export tools) call the expiry field
        # "expirationDate"; Playwright wants "expires". Session cookies have
        # neither, which Playwright is fine with (they just won't persist
        # past this run, which doesn't matter for a one-shot script).
        if "expirationDate" in c and "expires" not in c:
            c["expires"] = c.pop("expirationDate")
        c.pop("hostOnly", None)
        c.pop("session", None)
        c.pop("storeId", None)
        # Chrome's own internal names ("no_restriction", null, ...) aren't
        # what Playwright's API wants (exactly "Strict"/"Lax"/"None") --
        # add_cookies() rejects the whole batch on the first mismatch.
        same_site = (c.get("sameSite") or "").lower()
        c["sameSite"] = _SAME_SITE.get(same_site, "Lax")
    return cookies


def _normalize(text):
    return re.sub(r"\s+", " ", (text or "").strip().lower())


CDP_URL = "http://localhost:9222"


def _get_page(p):
    """Returns (page, cleanup) -- cleanup() closes whatever this opened,
    without ever closing a browser it didn't launch itself.

    Confirmed by hand, in order of increasing effort, that none of these get
    past Cloudflare's challenge here from a fresh automated session: plain
    curl_cffi TLS impersonation, Playwright's bundled Chromium (headless or
    headed), channel="chrome" (the machine's real, installed Chrome) with
    playwright_stealth patches and headed mode, and replaying an exported
    __cf_bm cookie into that session. Cloudflare is evidently scoring the
    whole live session (IP + TLS + fingerprint + behavior together), not any
    one replayable piece of it.

    The one thing that reliably has trust here is the user's own already-open,
    already-browsing-fine Chrome -- so if it's running with
    --remote-debugging-port=9222 (chrome.exe --remote-debugging-port=9222),
    this attaches to THAT live session via Chrome DevTools Protocol and opens
    a new tab in it, instead of spinning up a separate automated one. Falls
    back to launching a fresh browser (real Chrome + stealth + cookie file)
    if nothing's listening on that port -- worse odds, but something rather
    than a hard requirement to have Chrome running in debug mode."""
    try:
        browser = p.chromium.connect_over_cdp(CDP_URL, timeout=5000)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        print("actuals: attached to your already-running Chrome via CDP -- using that live session")
        return page, page.close  # never close someone's actual browser

    except Exception:
        print(f"actuals: no Chrome listening on {CDP_URL} (start it with "
              f"--remote-debugging-port=9222 for the best odds) -- launching a fresh automated browser instead")

    try:
        browser = p.chromium.launch(headless=False, channel="chrome", args=["--disable-blink-features=AutomationControlled"])
    except Exception:
        print("actuals: no installed Chrome found for channel='chrome' either -- falling back to Playwright's "
              "bundled Chromium, which is more likely to get stuck on Cloudflare's challenge")
        browser = p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])

    context = browser.new_context(user_agent=HEADERS["User-Agent"], viewport={"width": 1366, "height": 900})
    Stealth().apply_stealth_sync(context)
    cookies = _load_cookies()
    if cookies:
        context.add_cookies(cookies)
    page = context.new_page()
    return page, browser.close


def fetch_actuals():
    """Returns {(country, normalized title): actual value} scraped from the
    real calendar page. Returns {} (not raising) on any failure -- a missing
    Actual column is a lesser problem than losing the whole week's data over
    a scrape hiccup; the caller just leaves "actual" blank for every event."""
    try:
        with sync_playwright() as p:
            page, cleanup = _get_page(p)
            try:
                page.goto(CALENDAR_URL, timeout=30000, wait_until="domcontentloaded")

                # Some of Cloudflare's challenges auto-clear after a few
                # seconds; others are an actual "Verify you are human"
                # checkbox (Turnstile, in its own iframe) that needs a real
                # click. Try that once, best-effort -- if the iframe/checkbox
                # isn't there (because it already auto-cleared, or the DOM
                # doesn't match), this just falls through to the poll below.
                try:
                    page.wait_for_timeout(2000)
                    checkbox = page.frame_locator("iframe[title*='Cloudflare' i], iframe[src*='challenges.cloudflare.com']").locator("input[type=checkbox]")
                    checkbox.click(timeout=3000)
                except Exception:
                    pass

                # Poll for the real calendar table instead of one fixed wait,
                # and say plainly if it's still stuck on the challenge page
                # when time runs out, rather than quietly returning zero rows
                # that look identical to "the page loaded but had nothing".
                cleared = False
                for _ in range(30):
                    if page.query_selector("tr.calendar__row"):
                        cleared = True
                        break
                    page.wait_for_timeout(1000)
                if not cleared:
                    print(f"actuals: still on Cloudflare's challenge page after 30s (title: {page.title()!r})")
                    return {}

                rows = page.query_selector_all("tr.calendar__row")

                actuals = {}
                last_country = ""
                for row in rows:
                    # ForexFactory only prints the currency once per group of
                    # same-time events, leaving every row after the first in
                    # that group with an empty (not missing) currency cell --
                    # falling back only when the *element* is absent, like the
                    # first version of this did, silently dropped every one of
                    # those follow-up rows instead of inheriting the group's
                    # currency.
                    country_el = row.query_selector(".calendar__currency")
                    country_text = country_el.inner_text().strip() if country_el else ""
                    country = country_text or last_country
                    if country:
                        last_country = country

                    # A comma-separated selector matches whichever of the two
                    # comes first in the DOM, not "prefer the specific one" --
                    # .calendar__event is the whole cell (icons and all), so
                    # picking it over the actual title span pulled in extra
                    # text that never matched the feed's clean title.
                    title_el = row.query_selector(".calendar__event-title") or row.query_selector(".calendar__event")
                    actual_el = row.query_selector(".calendar__actual")
                    if not title_el or not actual_el:
                        continue
                    actual = actual_el.inner_text().strip()
                    if not actual:
                        continue
                    actuals[(country, _normalize(title_el.inner_text()))] = actual
                return actuals
            finally:
                cleanup()
    except Exception as e:
        print(f"actuals: failed ({e}), leaving Actual blank for this run")
        return {}


def fetch():
    result = _previous_periods()
    actuals = fetch_actuals()
    unmatched_past = []
    now = datetime.now(timezone.utc)

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
            if not e["actual"]:
                try:
                    already_happened = datetime.fromisoformat(e["date"]) < now
                except (KeyError, ValueError):
                    already_happened = False
                if already_happened:
                    unmatched_past.append((e.get("country", ""), e.get("title", "")))
        result[period] = events

    if unmatched_past:
        print(f"{len(unmatched_past)} already-released event(s) got no Actual match "
              f"(scrape found nothing for that country+title, or the row wasn't on the page):")
        for country, title in unmatched_past[:20]:
            print(f"  {country}: {title}")

    return result


if __name__ == "__main__":
    periods = fetch()
    OUTPUT_PATH.write_text(json.dumps({"fetched_at": datetime.now(timezone.utc).isoformat(), "periods": periods}, indent=2))
    counts = {p: len(events) for p, events in periods.items()}
    with_actual = {p: sum(1 for e in events if e.get("actual")) for p, events in periods.items()}
    print(f"Wrote {counts} to {OUTPUT_PATH} ({with_actual} had an Actual value)")
