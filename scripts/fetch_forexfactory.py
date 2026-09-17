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
    (r"\bretail price index\b", "rpi"),
    (r"\bgross domestic product\b", "gdp"),
    (r"\bpurchasing managers?'?s? index\b", "pmi"),
    (r"\bnon[- ]?farm payrolls\b", "nfp"),
    # Each central bank's own rate-decision wording collapses to one token;
    # already scoped to one currency's candidates, so no cross-bank risk.
    (r"\bfederal funds rate\b", "cbrate"),
    (r"\bfed interest rate decision\b", "cbrate"),
    (r"\binterest rate decision\b", "cbrate"),
    (r"\bofficial cash rate\b", "cbrate"),
    (r"\bcash rate\b", "cbrate"),
    (r"\bmain refinancing rate\b", "cbrate"),
    (r"\bofficial bank rate\b", "cbrate"),
    # "Core retail sales" is the conventional name for "retail sales ex autos".
    (r"\bcore retail sales?\b", "retailsalesexauto"),
    (r"\bretail sales? ex[- ]?autos?\b", "retailsalesexauto"),
    # ForexFactory's EIA/API weekly oil-inventory reports vs FXStreet's naming.
    (r"\bcrude oil inventor(?:y|ies)\b", "eiacrudeoil"),
    (r"\beia crude oil stocks? change\b", "eiacrudeoil"),
    (r"\bapi weekly statistical bulletin\b", "apicrudeoil"),
    (r"\bapi weekly crude oil stocks?\b", "apicrudeoil"),
    # Same Australian series, branded differently by different aggregators.
    (r"\bmi leading index\b", "wmileadingindex"),
    (r"\bwestpac leading index\b", "wmileadingindex"),
    (r"\bbusinessnz services index\b", "businessnzpsi"),
    (r"\bbusiness ?nz psi\b", "businessnzpsi"),
    # China's NBS new-home-price release, FF's own "HPI" abbreviation (e.g. UK's
    # HPI y/y), and FXStreet's spelled-out "House Price Index" are all the same
    # concept -- safely shared across currencies since matching is already
    # currency-scoped, so this never mixes a UK HPI candidate into a CNY event.
    (r"\bnew home prices?\b", "housepriceindex"),
    (r"\bhouse price index\b", "housepriceindex"),
    (r"\bhpi\b", "housepriceindex"),
    # Japan's headline is always "core" machinery orders regardless of who's naming it.
    (r"\bcore machinery orders?\b", "machineryorders"),
    (r"\bmachinery orders?\b", "machineryorders"),
    # ADP's own weekly report headlines its 4-week average by design (the raw
    # weekly print is too noisy to be meaningful alone) -- confirmed by hand
    # against forexfactory.com's own Actual (16.3K) matching FXStreet's 4-week
    # average (16.25) for the same release, not two different numbers.
    (r"\badp weekly employment change\b", "adpemployment"),
    (r"\badp employment change 4-week average\b", "adpemployment"),
    # The UK's "3m/y" and FXStreet's "3Mo/Yr" are the same 3-month-average-YoY
    # notation, just abbreviated differently.
    (r"\b3m/y\b", "3myoy"),
    (r"\b3mo/yr\b", "3myoy"),
    # ForexFactory's unqualified "Average Earnings Index" is, by UK convention,
    # specifically the *including*-bonus headline figure -- confirmed by hand
    # against forexfactory.com's own Actual (3.9%) matching the Including
    # Bonus variant (3.9%), not Excluding Bonus (3.5%) that week.
    (r"\baverage earnings index\b", "avgweeklyearnings"),
    (r"\baverage earnings including bonus\b", "avgweeklyearnings"),
]
# Genuinely different sub-series within the same release batch -- must match
# exactly. Revision-stage labels (final/preliminary/flash/revised) describe
# the SAME series at a different publish stage, not a different one, so they
# are filler, not a qualifier to require agreement on. "eu" also belongs here:
# a country can publish its trade balance against all partners alongside an
# EU-only sub-total (e.g. Italy's "Global Trade Balance" vs "Trade Balance
# EU") -- ForexFactory's unqualified title should never silently match the
# EU-only variant just because it happened to be the other thing nearby, and
# requiring equal qualifier sets means an unqualified title (no "eu") simply
# can't match a "...EU" candidate, confirmed by hand needed to get Italy's
# Trade Balance to land on the Global figure forexfactory.com itself shows.
_QUALIFIERS = {"core", "median", "trimmed", "common", "underlying", "headline", "eu"}
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
# Speeches/press conferences/summits are calendar placeholders, not data
# releases -- they can never have a real "actual", and FF times them on
# round numbers (14:00, 15:00) that can coincidentally collide with an
# unrelated real release at the same instant (confirmed by hand: "Treasury
# Sec Bessent Speaks" landed on the Redbook Index's timestamp and would have
# picked up its 8.5% as if it were the speech's own number). Skip matching
# for these outright rather than rely on topic overlap to save us every time.
_NO_ACTUAL_RE = re.compile(r"\b(speaks?|speech|testimony|remarks|press conference|summit)\b", re.I)
_PERIOD_MAP = {
    "m/m": "mom", "mom": "mom",
    "y/y": "yoy", "yoy": "yoy",
    "q/q": "qoq", "qoq": "qoq",
    "w/w": "wow", "wow": "wow",
    "ytd/y": "yoy", "ytdy": "yoy", "ytd": "yoy",
}
_PERIOD_RE = re.compile(r"\b(m/m|y/y|q/q|w/w|ytd/y|mom|yoy|qoq|wow|ytdy|ytd)\b")


def _stem(token):
    # Just enough to line up "Prices" (FF) with "Price" (FXStreet) etc. --
    # not a real stemmer, and deliberately conservative (skips short words
    # and "-ss" endings) to avoid mangling unrelated tokens.
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _title_parts(title):
    text = (title or "").lower()
    match = _PERIOD_RE.search(text)
    period = _PERIOD_MAP.get(match.group(1)) if match else None
    text = _PERIOD_RE.sub(" ", text)
    for pattern, replacement in _PHRASE_COLLAPSE:
        text = re.sub(pattern, replacement, text)
    tokens = {_stem(t) for t in re.findall(r"[a-z0-9]+", text)} - _FILLER
    qualifiers = tokens & _QUALIFIERS
    return period, qualifiers, tokens - _QUALIFIERS


def _best_match(ff_title, candidates):
    """candidates: [(fxstreet_name, formatted_actual, countryCode), ...], all
    within _MATCH_WINDOW of ff_title's event. A lone candidate is NOT trusted
    on proximity alone -- being the only other thing FXStreet reported in
    that window doesn't make it the same series (see _MATCH_WINDOW's comment
    for two real, confirmed cases where it wasn't); it still has to clear the
    topic-overlap bar below. Returns the formatted_actual of the one
    unambiguous match, or "" if none clears the bar -- a blank Actual is far
    less harmful than a confidently wrong one."""
    if not candidates:
        return ""

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

    ff_period, ff_qualifiers, ff_topic = _title_parts(ff_title)
    if ff_country:
        ff_topic = ff_topic - {word for word in _COUNTRY_ADJECTIVES if _COUNTRY_ADJECTIVES[word] == ff_country}
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


# The original version of this trusted a single candidate at the exact same
# timestamp with no title comparison at all, reasoning that two DIFFERENT
# real indicators from the same country essentially never fire in the same
# minute. That's true, but incomplete: a single statistics release routinely
# reports several period-variants of itself at once -- confirmed by hand on
# two real, wrong-data cases -- NZD's Visitor Arrivals (MoM) landed on the
# YoY figure, and USD's ADP Weekly Employment Change landed on the 4-week
# average, both because that was the only other thing FXStreet had at that
# exact minute. A "the only candidate nearby" isn't a "the same series"; only
# _best_match()'s period/qualifier/topic agreement is, so every candidate now
# goes through it regardless of how close the timestamp is. FXStreet and
# ForexFactory also don't always log the exact same publish minute for the
# same real release (confirmed by hand: Germany's 30-y Bond Auction off by 5
# minutes, NZD's GDT Price Index off by 39, AU's MI/Westpac Leading Index off
# by 30), which is what the window width is actually sized for.
_MATCH_WINDOW = timedelta(minutes=45)


def fetch_actuals():
    """Returns a list of (currency, datetime, name, formatted actual,
    countryCode) tuples from FXStreet's calendar API, covering roughly the
    last 8 days through 2 days ahead (comfortably wider than the "week"
    feed's own span). Returns [] (not raising) on any failure -- a missing
    Actual column is a lesser problem than losing the whole week's data over
    one flaky request."""
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
        return []

    actuals = []
    for event in events:
        if event.get("actual") is None:
            continue
        try:
            dt = datetime.fromisoformat(event["dateUtc"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        actuals.append((
            event.get("currencyCode", ""), dt,
            event.get("name", ""), _format_actual(event), event.get("countryCode", ""),
        ))
    return actuals


def _candidates_near(actuals, currency, event_dt, window):
    return [
        (name, actual, country) for cur, dt, name, actual, country in actuals
        if cur == currency and abs(dt - event_dt) <= window
    ]


# A last-resort third tier for the rare case where the SAME weekly report
# lands on a different calendar day between the two providers, not just a
# different minute -- confirmed by hand on the API Weekly Statistical
# Bulletin, which FXStreet logged a full ~24.5 hours later one week. Safe at
# this width specifically because it demands an EXACT normalized-title match
# (not just >=0.5 overlap like _best_match) and uniqueness, and because a day
# and a half is comfortably under half of any weekly indicator's own release
# cadence, so it can't accidentally reach into an adjacent week's number.
_WIDE_EXACT_WINDOW = timedelta(hours=36)


def _exact_title_match(actuals, currency, event_dt, title):
    candidates = _candidates_near(actuals, currency, event_dt, _WIDE_EXACT_WINDOW)
    if not candidates:
        return ""
    ff_parts = _title_parts(title)
    matches = [actual for name, actual, _country in candidates if _title_parts(name) == ff_parts]
    return matches[0] if len(matches) == 1 else ""


def _match_actual(actuals, country, title, event_dt):
    if not event_dt or _NO_ACTUAL_RE.search(title or ""):
        return ""
    wide = _candidates_near(actuals, country, event_dt, _MATCH_WINDOW)
    result = _best_match(title, wide) if wide else ""
    return result or _exact_title_match(actuals, country, event_dt, title)


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
            e["actual"] = _match_actual(actuals, e.get("country", ""), e.get("title", ""), event_dt)
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
