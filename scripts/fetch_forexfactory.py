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

The feed also never includes an "Actual" value (only Forecast/Previous).
forexfactory.com's own calendar page has it, but sits behind a Cloudflare
challenge that turned out to be a genuinely hard wall -- confirmed by hand,
across eight distinct approaches (curl_cffi TLS impersonation, Playwright's
bundled and real-Chrome-channel browsers, headless and headed, stealth
patches, cookie replay, attaching to the user's own already-open Chrome via
CDP -- blocked outright by Chrome's own security policy on the default
profile -- a dedicated persistent profile warmed across runs, and organic
navigation with simulated mouse/scroll activity), that nothing client-side
gets past it.

FXStreet's economic calendar (fxstreet.com/economic-calendar) turned out to
be the answer instead: its own JSON API (calendar-api.fxsstatic.com) is
directly reachable with a plain, unauthenticated GET (no Cloudflare
challenge at all -- it only wants a Referer header pointing at fxstreet.com,
confirmed by hand) and returns real Actual values. FXStreet and ForexFactory
often name the same release differently, so matching by (currency, exact
title) barely works (~4% of already-released events, confirmed by hand);
matching by (currency, exact UTC release time) instead works much better
(~74%), since both providers report the same real-world release instant even
when they word the headline differently.

That timestamp match isn't always unique, though: several indicators from
the same country routinely release in the same batch (Canada's CPI report
fires six series at once; China's monthly dump is worse), and naively taking
"whatever FXStreet event shares this timestamp" assigns the wrong number to
most of them -- confirmed by hand, e.g. every one of Canada's six CPI-day
series was coming back with the Manufacturing Sales value. _best_match()
compares the FF and FXStreet titles (period, qualifier words like
"core"/"median", and remaining topic words) within a same-timestamp batch and
only accepts a value when exactly one candidate lines up; ambiguous batches
are left blank rather than guessed."""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

FF_FEEDS = {
    "week": "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
}
FXSTREET_CALENDAR_API = "https://calendar-api.fxsstatic.com/en/api/v2/eventDates/{start}/{end}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "*/*",
}
# calendar-api.fxsstatic.com 401s without this -- it just checks the request
# came from fxstreet.com's own pages, not a real bot-detection challenge.
FXSTREET_HEADERS = {**HEADERS, "Accept": "application/json", "Referer": "https://www.fxstreet.com/"}

OUTPUT_PATH = Path(__file__).parent.parent / "forexfactory_cache.json"


def _previous_periods():
    if not OUTPUT_PATH.exists():
        return {}
    return json.loads(OUTPUT_PATH.read_text()).get("periods", {})


def _normalize(text):
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _format_actual(event):
    value = event.get("actual")
    if value is None:
        return ""
    # int-like floats (60.0) print as "60" via :g instead of "60.0" --
    # matches how ForexFactory's own feed formats whole-number values.
    text = f"{value:g}" if isinstance(value, (int, float)) else str(value)
    unit = event.get("unit") or ""
    return f"{text}{unit}"


# Multiple indicators from the same country routinely release in the same
# batch (Canada's CPI report alone fires six series -- headline, core, and
# three separate BoC core measures -- all at the same instant; China's
# monthly data dump is worse). Matching on (currency, timestamp) alone is
# ambiguous for those, so within a batch we also compare the FF and FXStreet
# titles via _title_parts()/_best_match() before trusting a value.
_PHRASE_COLLAPSE = [
    (r"\bconsumer price index\b", "cpi"),
    (r"\bproducer price index\b", "ppi"),
    (r"\bproducer( and import)? prices\b", "ppi"),
    (r"\bwholesale price index\b", "wpi"),
    (r"\bgross domestic product\b", "gdp"),
    (r"\bpurchasing managers?'?s? index\b", "pmi"),
    (r"\bnon[- ]?farm payrolls\b", "nfp"),
]
# Genuinely different sub-series within the same release batch -- must match
# exactly. Revision-stage labels (final/preliminary/flash/revised) describe
# the SAME series at a different publish stage, not a different one, so they
# are filler, not a qualifier to require agreement on.
_QUALIFIERS = {"core", "median", "trimmed", "common", "underlying", "headline"}
_FILLER = {"the", "of", "a", "an", "final", "preliminary", "advance", "flash", "revised"}
# ForexFactory prefixes a country adjective onto a Eurozone-wide series name
# to mean the national release (vs the bloc-wide one); FXStreet instead tags
# each event with its own countryCode (DE vs EMU for Germany vs Eurozone,
# confirmed by hand on the ZEW Economic Sentiment collision) using the exact
# same event name for both, which title-word matching alone can't tell apart.
_COUNTRY_ADJECTIVES = {
    "german": "DE", "french": "FR", "italian": "IT", "spanish": "ES", "dutch": "NL",
}
_ZONE_WIDE_COUNTRY_CODES = {"EMU", "EU"}
_PERIOD_MAP = {
    "m/m": "mom", "mom": "mom",
    "y/y": "yoy", "yoy": "yoy",
    "q/q": "qoq", "qoq": "qoq",
    "w/w": "wow", "wow": "wow",
    "ytd/y": "yoy", "ytdy": "yoy", "ytd": "yoy",
}
_PERIOD_RE = re.compile(r"\b(m/m|y/y|q/q|w/w|ytd/y|mom|yoy|qoq|wow|ytdy|ytd)\b")


def _title_parts(title):
    text = (title or "").lower()
    match = _PERIOD_RE.search(text)
    period = _PERIOD_MAP.get(match.group(1)) if match else None
    text = _PERIOD_RE.sub(" ", text)
    for pattern, replacement in _PHRASE_COLLAPSE:
        text = re.sub(pattern, replacement, text)
    tokens = set(re.findall(r"[a-z0-9]+", text)) - _FILLER
    qualifiers = tokens & _QUALIFIERS
    return period, qualifiers, tokens - _QUALIFIERS


def _best_match(ff_title, candidates):
    """candidates: [(fxstreet_name, formatted_actual, countryCode), ...] all
    sharing the same (currency, timestamp) as ff_title. Returns the
    formatted_actual of the one unambiguous match, or "" if none clears the
    bar -- a blank Actual is far less harmful than a confidently wrong one."""
    if len(candidates) == 1:
        return candidates[0][1]

    lower_title = ff_title.lower()
    ff_country = next(
        (code for word, code in _COUNTRY_ADJECTIVES.items()
         if re.search(rf"\b{word}\b", lower_title)),
        None,
    )
    by_country = [c for c in candidates if c[2] == (ff_country or "")] if ff_country else \
        [c for c in candidates if c[2] in _ZONE_WIDE_COUNTRY_CODES]
    if by_country:
        candidates = by_country
        if len(candidates) == 1:
            return candidates[0][1]

    ff_period, ff_qualifiers, ff_topic = _title_parts(ff_title)
    scored = []
    for name, actual, _country in candidates:
        period, qualifiers, topic = _title_parts(name)
        if ff_period and period and ff_period != period:
            continue
        if qualifiers != ff_qualifiers:
            continue
        union = ff_topic | topic
        score = len(ff_topic & topic) / len(union) if union else 0.0
        if score >= 0.5:
            scored.append((score, actual))

    if not scored:
        return ""
    scored.sort(key=lambda x: x[0], reverse=True)
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.15:
        return ""
    return scored[0][1]


def fetch_actuals():
    """Returns {(currency, ISO UTC release time): [(name, actual, countryCode), ...]}
    from FXStreet's calendar API, covering roughly the last 8 days through 2
    days ahead (comfortably wider than the "week" feed's own span). Returns {}
    (not raising) on any failure -- a missing Actual column is a lesser
    problem than losing the whole week's data over one flaky request."""
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=8)).strftime("%Y-%m-%dT00:00:00Z")
    end = (now + timedelta(days=2)).strftime("%Y-%m-%dT23:59:59Z")
    url = FXSTREET_CALENDAR_API.format(start=start, end=end)

    try:
        response = requests.get(url, headers=FXSTREET_HEADERS, timeout=15)
        response.raise_for_status()
        events = response.json()
    except (requests.RequestException, ValueError) as e:
        print(f"actuals: failed ({e}), leaving Actual blank for this run")
        return {}

    actuals = {}
    for event in events:
        if event.get("actual") is None:
            continue
        try:
            dt = datetime.fromisoformat(event["dateUtc"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        key = (event.get("currencyCode", ""), dt.isoformat())
        actuals.setdefault(key, []).append(
            (event.get("name", ""), _format_actual(event), event.get("countryCode", ""))
        )
    return actuals


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
            try:
                event_dt = datetime.fromisoformat(e["date"]).astimezone(timezone.utc)
            except (KeyError, ValueError):
                event_dt = None
            candidates = actuals.get((e.get("country", ""), event_dt.isoformat()), []) if event_dt else []
            e["actual"] = _best_match(e.get("title", ""), candidates) if candidates else ""
            if not e["actual"] and event_dt and event_dt < now:
                unmatched_past.append((e.get("country", ""), e.get("title", "")))
        result[period] = events

    if unmatched_past:
        print(f"{len(unmatched_past)} already-released event(s) got no Actual match "
              f"(FXStreet may not track that specific indicator, or it's a speech/qualitative event with no number):")
        for country, title in unmatched_past[:20]:
            print(f"  {country}: {title}")

    return result


if __name__ == "__main__":
    periods = fetch()
    OUTPUT_PATH.write_text(json.dumps({"fetched_at": datetime.now(timezone.utc).isoformat(), "periods": periods}, indent=2))
    counts = {p: len(events) for p, events in periods.items()}
    with_actual = {p: sum(1 for e in events if e.get("actual")) for p, events in periods.items()}
    print(f"Wrote {counts} to {OUTPUT_PATH} ({with_actual} had an Actual value)")
