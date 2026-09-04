import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests
from curl_cffi.requests.exceptions import RequestException as CurlRequestException
from flask import Flask, jsonify, render_template, request
import requests

import analysis

NETWORK_ERRORS = (requests.RequestException, CurlRequestException)

app = Flask(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "*/*",
}

CURRENCIES = ["USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD"]
FILTERABLE_INSTRUMENTS = CURRENCIES + ["XAU", "XAG"]  # Gold, Silver

FF_FEEDS = {
    "lastweek": "https://nfs.faireconomy.media/ff_calendar_lastweek.json",
    "week": "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "nextweek": "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
}

FXSTREET_RSS = "https://www.fxstreet.com/rss/news"
FXSTREET_ANALYSIS_RSS = "https://www.fxstreet.com/rss/analysis"
INVESTING_RSS = "https://www.investing.com/rss/news_1.rss"  # category 1 = Forex News
MYFXBOOK_NEWS_URL = "https://www.myfxbook.com/news"
STOCKTWITS_STREAM_URL = "https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"
SOCIAL_PAIRS = list(analysis.PAIRS)  # StockTwits recognizes these same 9 tickers directly

# DailyFX sits behind Akamai's bot-protection, which blocks at the network/IP level
# (every path, including /sitemap.xml, returns an Akamai "Access Denied" edge block).
# Unlike Myfxbook's Cloudflare check, this isn't a TLS-fingerprint issue curl_cffi can
# bypass -- requests just time out. Getting past it would need a residential proxy or
# real browser automation, which is fragile, costly, and against DailyFX's ToS, so it's
# listed here to show as unavailable in the UI instead of being silently built.
UNAVAILABLE_SOURCES = {
    "dailyfx": "blocked by Akamai bot-protection at the network level (needs a residential proxy)",
}

# Free hosting tiers (e.g. PythonAnywhere) restrict outbound requests to a domain
# whitelist that most scraped sources aren't on. Routing through a Cloudflare Worker
# (itself an allowed domain) lets those hosts reach otherwise-blocked sources.
SCRAPE_PROXY_URL = os.environ.get("SCRAPE_PROXY_URL", "https://news-api-proxy.ali45.workers.dev")


def _via_proxy(url):
    if not SCRAPE_PROXY_URL:
        return url
    return f"{SCRAPE_PROXY_URL}/?url={quote(url, safe='')}"


CACHE_TTL = 300  # seconds
CACHE_DIR = Path(__file__).parent / ".cache"


class RateLimited(Exception):
    def __init__(self, retry_after):
        self.retry_after = retry_after


def _cache_file(key):
    return CACHE_DIR / f"{key}.json"


def _read_cache(key):
    path = _cache_file(key)
    if not path.exists():
        return None
    payload = json.loads(path.read_text())
    return payload["fetched_at"], payload["events"]


def _write_cache(key, events):
    CACHE_DIR.mkdir(exist_ok=True)
    _cache_file(key).write_text(json.dumps({"fetched_at": time.time(), "events": events}))


def _cached_fetch(key, loader):
    """Run loader() with disk caching, retry-after handling, and stale-on-failure fallback."""
    cached = _read_cache(key)
    if cached and time.time() - cached[0] < CACHE_TTL:
        return cached[1]

    try:
        events = loader()
    except (*NETWORK_ERRORS, RateLimited):
        if cached:
            return cached[1]
        raise

    _write_cache(key, events)
    return events


def fetch_forexfactory(period):
    url = FF_FEEDS.get(period)
    if not url:
        return None

    def loader():
        response = requests.get(_via_proxy(url), headers=HEADERS, timeout=10)
        if response.status_code == 429:
            raise RateLimited(int(response.headers.get("Retry-After", 60)))
        response.raise_for_status()
        raw_events = response.json()
        for e in raw_events:
            e["source"] = "ForexFactory"
            e["type"] = "calendar"
            e["link"] = None
        return raw_events

    return _cached_fetch(f"ff_{period}", loader)


def _parse_pub_date(raw):
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).isoformat()  # RFC 2822, e.g. FXStreet
    except (TypeError, ValueError):
        pass
    try:
        # Investing.com uses "YYYY-MM-DD HH:MM:SS" in GMT with no offset
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None


def _strip_html(text):
    return re.sub(r"<[^>]+>", " ", text or "").strip()


def _fetch_rss_news(url, source_name, cache_key, impact="News"):
    """Shared parser for simple RSS news feeds (FXStreet, Investing.com)."""
    def loader():
        response = requests.get(_via_proxy(url), headers=HEADERS, timeout=10)
        if response.status_code == 429:
            raise RateLimited(int(response.headers.get("Retry-After", 60)))
        response.raise_for_status()
        root = ET.fromstring(response.content)

        events = []
        for item in root.findall("./channel/item"):
            title = (item.findtext("title") or "").strip()
            description = _strip_html(item.findtext("description"))
            pub_date = _parse_pub_date(item.findtext("pubDate"))

            events.append({
                "source": source_name,
                "type": "news",
                "country": analysis.infer_instrument(f"{title} {description}"),
                "title": title,
                "description": description,
                "date": pub_date,
                "impact": impact,
                "forecast": "",
                "previous": "",
                "link": item.findtext("link"),
            })
        return events

    return _cached_fetch(cache_key, loader)


def fetch_fxstreet_news():
    return _fetch_rss_news(FXSTREET_RSS, "FXStreet", "rss_fxstreet")


def fetch_fxstreet_analysis():
    """FXStreet's Analysis feed is analyst-written forecasts that name the pair
    directly (e.g. "XAU/USD Price Forecast: ..."), a much stronger signal than
    inferring direction from a generic news headline."""
    return _fetch_rss_news(FXSTREET_ANALYSIS_RSS, "FXStreet Analysis", "rss_fxstreet_analysis", impact="Analysis")


def fetch_investing_news():
    return _fetch_rss_news(INVESTING_RSS, "Investing.com", "rss_investing")


def _parse_relative_time(text):
    """Turns strings like '2h 37min ago' / '26 minutes ago' / '1 day ago' into a UTC datetime."""
    text = text.lower()
    day_m = re.search(r"(\d+)\s*d(?:ay)?s?\b", text)
    hour_m = re.search(r"(\d+)\s*h(?:our)?s?\b", text)
    min_m = re.search(r"(\d+)\s*m(?:in(?:ute)?)?s?\b", text)

    if not (day_m or hour_m or min_m):
        return None

    delta = timedelta(
        days=int(day_m.group(1)) if day_m else 0,
        hours=int(hour_m.group(1)) if hour_m else 0,
        minutes=int(min_m.group(1)) if min_m else 0,
    )
    return datetime.now(timezone.utc) - delta


def fetch_myfxbook_news():
    """Myfxbook has no public feed for its news page, so this parses the rendered HTML.
    Myfxbook's Cloudflare in front blocks plain `requests` by TLS fingerprint even with
    browser headers, so this uses curl_cffi to impersonate a real Chrome TLS handshake.
    It also depends on Myfxbook's current markup and will need updating if their layout changes."""
    def loader():
        response = curl_requests.get(_via_proxy(MYFXBOOK_NEWS_URL), headers=HEADERS, impersonate="chrome124", timeout=10)
        if response.status_code == 429:
            raise RateLimited(int(response.headers.get("Retry-After", 60)))
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        events = []
        seen_links = set()
        for block in soup.select(".news-info"):
            link_el = block.select_one("h2 a")
            if not link_el:
                continue

            link = link_el.get("href", "")
            if link.startswith("/"):
                link = "https://www.myfxbook.com" + link
            if link in seen_links:
                continue
            seen_links.add(link)

            title = link_el.get_text(strip=True)
            summary_el = block.select_one(".news-description, .news-summary, [class*='margin-top-10']")
            description = summary_el.get_text(strip=True) if summary_el else ""
            details_el = block.select_one(".news-details")
            date = _parse_relative_time(details_el.get_text(" ", strip=True)) if details_el else None

            events.append({
                "source": "Myfxbook",
                "type": "news",
                "country": analysis.infer_instrument(f"{title} {description}"),
                "title": title,
                "description": description,
                "date": date.isoformat() if date else None,
                "impact": "News",
                "forecast": "",
                "previous": "",
                "link": link,
            })
        return events

    return _cached_fetch("myfxbook_news", loader)


def fetch_social_sentiment(pair):
    """Pulls the latest StockTwits posts tagged with a pair's ticker (StockTwits uses
    the same EURUSD/XAUUSD-style symbols we already use) and classifies each one --
    using the poster's own Bullish/Bearish tag when they set it, otherwise scoring
    the post text for trader slang (long/short/buy/sell/...)."""
    url = STOCKTWITS_STREAM_URL.format(symbol=pair)

    def loader():
        response = requests.get(url, headers=HEADERS, timeout=10)
        if response.status_code == 429:
            raise RateLimited(int(response.headers.get("Retry-After", 60)))
        response.raise_for_status()
        data = response.json()

        posts = []
        for msg in data.get("messages", []):
            explicit = (msg.get("entities") or {}).get("sentiment") or {}
            result = analysis.classify_social_post(msg.get("body", ""), explicit.get("basic"))
            posts.append({
                "sentiment": result["sentiment"],
                "reason": result["reason"],
                "body": msg.get("body", ""),
                "user": (msg.get("user") or {}).get("username"),
                "date": msg.get("created_at"),
            })
        return posts

    return _cached_fetch(f"social_{pair}", loader)


@app.route("/api/social-sentiment")
def get_social_sentiment():
    pairs_param = request.args.get("pairs")
    pairs = [p.strip().upper() for p in pairs_param.split(",")] if pairs_param else SOCIAL_PAIRS
    pairs = [p for p in pairs if p in SOCIAL_PAIRS]

    result = {}
    for pair in pairs:
        try:
            posts = fetch_social_sentiment(pair)
        except RateLimited as e:
            result[pair] = {"error": f"rate limited, try again in {e.retry_after}s"}
            continue
        except NETWORK_ERRORS as e:
            result[pair] = {"error": f"failed to fetch: {e}"}
            continue

        summary = analysis.aggregate_social_posts(posts)
        summary["sample_posts"] = [p for p in posts if p["sentiment"] != "Neutral"][:5]
        result[pair] = summary

    return jsonify({"pairs": result})


@app.route("/")
def home():
    return render_template(
        "index.html",
        currencies=[{"code": c, "label": analysis.INSTRUMENT_LABELS[c]} for c in FILTERABLE_INSTRUMENTS],
        unavailable_sources=UNAVAILABLE_SOURCES,
    )


@app.route("/api/news")
def get_news():
    period = request.args.get("period", "week")
    sources = request.args.get("sources", "forexfactory,fxstreet,fxstreet_analysis,investing,myfxbook")
    currencies = request.args.get("currencies")  # comma separated, e.g. "USD,CAD"
    impact = request.args.get("impact")
    date_from = request.args.get("from")  # ISO datetime, e.g. 2026-09-03T00:00
    date_to = request.args.get("to")

    # "today"/"tomorrow" aren't their own feeds -- pull the underlying week(s) and,
    # unless the user picked an explicit range, narrow every source down to just
    # that single day. "tomorrow" can fall in either this week's or next week's feed
    # (e.g. today is Sunday), so both are fetched and merged to be safe.
    fetch_periods = [period]
    if period in ("today", "tomorrow"):
        fetch_periods = ["week", "nextweek"] if period == "tomorrow" else ["week"]
        if not date_from and not date_to:
            target_date = datetime.now(timezone.utc).date()
            if period == "tomorrow":
                target_date += timedelta(days=1)
            date_from = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc).isoformat()
            date_to = datetime.combine(target_date, datetime.max.time(), tzinfo=timezone.utc).isoformat()

    wanted_sources = {s.strip().lower() for s in sources.split(",") if s.strip()}
    events = []

    try:
        if "forexfactory" in wanted_sources:
            for fetch_period in fetch_periods:
                try:
                    ff_events = fetch_forexfactory(fetch_period)
                except NETWORK_ERRORS:
                    # today/tomorrow merge >1 period -- one feed being briefly down
                    # (e.g. next week's calendar not published yet) shouldn't fail
                    # the whole request when another period may still have data.
                    if len(fetch_periods) > 1:
                        continue
                    raise
                if ff_events is None:
                    return jsonify({"error": f"invalid period '{period}', use one of {['today', 'tomorrow'] + list(FF_FEEDS)}"}), 400
                events += ff_events

        if "fxstreet" in wanted_sources:
            events += fetch_fxstreet_news()

        if "fxstreet_analysis" in wanted_sources:
            events += fetch_fxstreet_analysis()

        if "investing" in wanted_sources:
            events += fetch_investing_news()

        if "myfxbook" in wanted_sources:
            events += fetch_myfxbook_news()
    except RateLimited as e:
        return jsonify({"error": f"rate limited by news source, try again in {e.retry_after}s"}), 429
    except NETWORK_ERRORS as e:
        return jsonify({"error": f"failed to fetch news: {e}"}), 502

    if currencies:
        wanted = {c.strip().upper() for c in currencies.split(",") if c.strip()}
        events = [e for e in events if e.get("country", "").upper() in wanted]

    if impact:
        events = [e for e in events if e.get("impact", "").lower() == impact.lower()]

    if date_from:
        start = datetime.fromisoformat(date_from)
        events = [e for e in events if _event_time(e) and _event_time(e) >= start.replace(tzinfo=_event_time(e).tzinfo)]

    if date_to:
        end = datetime.fromisoformat(date_to)
        events = [e for e in events if _event_time(e) and _event_time(e) <= end.replace(tzinfo=_event_time(e).tzinfo)]

    events = [e for e in events if e.get("date")]
    events.sort(key=lambda e: e["date"])

    for e in events:
        e["analysis"] = analysis.analyze_event(e)

    pair_bias = analysis.aggregate_pair_bias(events)

    return jsonify({"count": len(events), "events": events, "pair_bias": pair_bias})


def _event_time(event):
    raw = event.get("date")
    if not raw:
        return None
    return datetime.fromisoformat(raw)


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "true").lower() == "true"
    app.run(debug=debug, host="0.0.0.0", port=port)
