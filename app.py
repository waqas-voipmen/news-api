import json
import os
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from functools import wraps
from pathlib import Path
from urllib.parse import quote

from flask import Flask, jsonify, render_template, request, redirect, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
import requests

import analysis

NETWORK_ERRORS = (requests.RequestException,)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-secret-change-me-in-production-8f2a1c9d")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "*/*",
}

CURRENCIES = ["USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD"]
FILTERABLE_INSTRUMENTS = CURRENCIES + ["XAU", "XAG"]  # Gold, Silver

SOURCE_LABELS = {
    "forexfactory": "ForexFactory (calendar)",
    "fxstreet": "FXStreet (news)",
    "fxstreet_analysis": "FXStreet (analyst forecasts)",
    "investing": "Investing.com (news)",
    "myfxbook": "Myfxbook (news)",
}

FF_FEEDS = {
    "lastweek": "https://nfs.faireconomy.media/ff_calendar_lastweek.json",
    "week": "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "nextweek": "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
}

FXSTREET_RSS = "https://www.fxstreet.com/rss/news"
FXSTREET_ANALYSIS_RSS = "https://www.fxstreet.com/rss/analysis"
INVESTING_RSS = "https://www.investing.com/rss/news_1.rss"  # category 1 = Forex News
MYFXBOOK_CACHE_URL = "https://raw.githubusercontent.com/waqas-voipmen/news-api/master/myfxbook_cache.json"
FF_CACHE_URL = "https://raw.githubusercontent.com/waqas-voipmen/news-api/master/forexfactory_cache.json"
STOCKTWITS_STREAM_URL = "https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"
SOCIAL_PAIRS = list(analysis.PAIRS)  # StockTwits recognizes these same 9 tickers directly

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
# Yahoo has no XAUUSD=X/XAG=X spot tickers; COMEX front-month futures (GC=F, SI=F)
# track spot gold/silver closely enough for a reference quote. DX-Y.NYB is the ICE
# US Dollar Index cash ticker, same instrument DXY refers to elsewhere in this app.
LIVE_PRICE_SYMBOLS = {
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "USDCHF": "USDCHF=X",
    "USDCAD": "USDCAD=X",
    "AUDUSD": "AUDUSD=X",
    "NZDUSD": "NZDUSD=X",
    "XAUUSD": "GC=F",
    "XAGUSD": "SI=F",
    "DXY": "DX-Y.NYB",
}

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
USERS_FILE = Path(__file__).parent / "users.json"
SETTINGS_FILE = Path(__file__).parent / "settings.json"
USER_EXPIRY_DAYS = 30  # non-admin users are auto-deleted this many days after creation

DEFAULT_SETTINGS = {
    "sources": {key: True for key in SOURCE_LABELS},
    "social_sentiment_enabled": True,
    "market_bias_enabled": True,
    "news_sentiment_enabled": True,
    "live_prices_enabled": True,
}


class RateLimited(Exception):
    def __init__(self, retry_after):
        self.retry_after = retry_after


def _prune_expired_users(users):
    """Deletes non-admin users older than USER_EXPIRY_DAYS and backfills a
    missing created_at (for users made before this feature existed) with the
    current time, so they get a fresh 30-day window instead of being deleted
    immediately. Returns True if anything changed (so the caller can persist)."""
    now = datetime.now(timezone.utc)
    changed = False
    for username in list(users.keys()):
        info = users[username]
        if not info.get("created_at"):
            info["created_at"] = now.isoformat()
            changed = True
        if info.get("is_admin"):
            continue
        if (now - datetime.fromisoformat(info["created_at"])).days >= USER_EXPIRY_DAYS:
            del users[username]
            changed = True
    return changed


def _load_users():
    if not USERS_FILE.exists():
        default_users = {
            "admin": {
                "password_hash": generate_password_hash("admin"),
                "is_admin": True,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        }
        _save_users(default_users)
        return default_users
    users = json.loads(USERS_FILE.read_text())
    if _prune_expired_users(users):
        _save_users(users)
    return users


def _load_settings():
    if not SETTINGS_FILE.exists():
        _save_settings(DEFAULT_SETTINGS)
        return json.loads(json.dumps(DEFAULT_SETTINGS))
    settings = json.loads(SETTINGS_FILE.read_text())
    # backfill any source/flag added after a settings.json was already saved
    settings.setdefault("sources", {})
    for key, default_value in DEFAULT_SETTINGS["sources"].items():
        settings["sources"].setdefault(key, default_value)
    settings.setdefault("social_sentiment_enabled", True)
    settings.setdefault("market_bias_enabled", True)
    settings.setdefault("news_sentiment_enabled", True)
    settings.setdefault("live_prices_enabled", True)
    return settings


def _save_settings(settings):
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2))


def _save_users(users):
    USERS_FILE.write_text(json.dumps(users, indent=2))


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        # Re-checking against the user store (not just the session cookie) means an
        # account deleted by the admin, or auto-expired after USER_EXPIRY_DAYS, is
        # logged out immediately instead of staying valid until the browser session ends.
        if "username" not in session or session["username"] not in _load_users():
            session.pop("username", None)
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        users = _load_users()
        if "username" not in session or session["username"] not in users:
            session.pop("username", None)
            return redirect(url_for("login", next=request.path))
        if not users[session["username"]].get("is_admin"):
            return "Forbidden: admin access required", 403
        return view(*args, **kwargs)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = _load_users().get(username)
        if user and check_password_hash(user["password_hash"], password):
            session["username"] = username
            return redirect(request.args.get("next") or url_for("home"))
        error = "Invalid username or password"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.pop("username", None)
    return redirect(url_for("login"))


@app.route("/admin")
@admin_required
def admin_panel():
    return redirect(url_for("admin_users"))


@app.route("/admin/users", methods=["GET", "POST"])
@admin_required
def admin_users():
    users = _load_users()
    message = None

    if request.method == "POST":
        action = request.form.get("action")
        if action == "create":
            new_username = request.form.get("new_username", "").strip()
            new_password = request.form.get("new_password", "")
            if not new_username or not new_password:
                message = "Username and password are both required"
            elif new_username in users:
                message = f"'{new_username}' already exists"
            else:
                users[new_username] = {
                    "password_hash": generate_password_hash(new_password),
                    "is_admin": False,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                _save_users(users)
                message = f"User '{new_username}' created"
        elif action == "delete":
            target = request.form.get("target_username")
            if target == "admin":
                message = "The admin account can't be deleted"
            elif target in users:
                del users[target]
                _save_users(users)
                message = f"User '{target}' deleted"

    users = _load_users()
    now = datetime.now(timezone.utc)
    users_info = []
    for uname, info in users.items():
        created_at = info.get("created_at")
        days_active = (now - datetime.fromisoformat(created_at)).days if created_at else None
        is_admin = info.get("is_admin", False)
        days_left = None if (is_admin or days_active is None) else max(USER_EXPIRY_DAYS - days_active, 0)
        users_info.append({
            "username": uname,
            "is_admin": is_admin,
            "days_active": days_active,
            "days_left": days_left,
        })

    return render_template(
        "admin_users.html",
        users_info=users_info,
        message=message,
        active="users",
        expiry_days=USER_EXPIRY_DAYS,
    )


@app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    message = None

    if request.method == "POST":
        settings = _load_settings()
        for key in SOURCE_LABELS:
            settings["sources"][key] = request.form.get(f"source_{key}") == "on"
        settings["social_sentiment_enabled"] = request.form.get("social_sentiment_enabled") == "on"
        settings["market_bias_enabled"] = request.form.get("market_bias_enabled") == "on"
        settings["news_sentiment_enabled"] = request.form.get("news_sentiment_enabled") == "on"
        settings["live_prices_enabled"] = request.form.get("live_prices_enabled") == "on"
        _save_settings(settings)
        message = "Settings saved"

    return render_template(
        "admin_settings.html",
        message=message,
        settings=_load_settings(),
        source_labels=SOURCE_LABELS,
        active="settings",
    )


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


def _cached_fetch(key, loader, ttl=CACHE_TTL):
    """Run loader() with disk caching, retry-after handling, and stale-on-failure fallback."""
    cached = _read_cache(key)
    if cached and time.time() - cached[0] < ttl:
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
    """ForexFactory's feed (nfs.faireconomy.media) rate-limits the Cloudflare Worker
    proxy's shared IPs too often to be usable live from PythonAnywhere, so this reads
    a cache pre-fetched from a residential machine (see scripts/fetch_forexfactory.py)
    and published to this GitHub repo, same approach as Myfxbook."""
    if period not in FF_FEEDS:
        return None

    def loader():
        response = requests.get(FF_CACHE_URL, headers=HEADERS, timeout=10)
        if response.status_code == 429:
            raise RateLimited(int(response.headers.get("Retry-After", 60)))
        response.raise_for_status()
        raw_events = response.json()["periods"].get(period)
        if raw_events is None:
            raise requests.RequestException(f"no cached ForexFactory data for period '{period}'")
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


def fetch_myfxbook_news():
    """Myfxbook's Cloudflare check blocks every datacenter/cloud IP range regardless
    of TLS fingerprint (confirmed against a direct request, a Cloudflare Worker, and
    a Vercel function), so it can't be scraped live from any free host. Instead this
    reads a cache pre-fetched from a residential machine (see scripts/fetch_myfxbook.py)
    and published to this GitHub repo."""
    def loader():
        response = requests.get(MYFXBOOK_CACHE_URL, headers=HEADERS, timeout=10)
        if response.status_code == 429:
            raise RateLimited(int(response.headers.get("Retry-After", 60)))
        response.raise_for_status()
        return response.json()["events"]

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


def fetch_live_price(pair):
    """Live-ish reference quote for a pair via Yahoo Finance's public chart endpoint
    (query1.finance.yahoo.com is reachable directly from PythonAnywhere's free-tier
    whitelist, unlike most other sources in this file, so no proxy needed here).
    Cached briefly (not the usual 5-minute CACHE_TTL) so the on-demand refresh
    button feels live while still protecting against a user mashing it repeatedly."""
    symbol = LIVE_PRICE_SYMBOLS[pair]
    url = YAHOO_CHART_URL.format(symbol=symbol)

    def loader():
        response = requests.get(url, headers=HEADERS, timeout=10)
        if response.status_code == 429:
            raise RateLimited(int(response.headers.get("Retry-After", 60)))
        response.raise_for_status()
        data = response.json()
        result = (data.get("chart") or {}).get("result")
        if not result:
            error = (data.get("chart") or {}).get("error") or {}
            raise requests.RequestException(error.get("description", "no data returned"))

        meta = result[0]["meta"]
        price = meta.get("regularMarketPrice")
        previous_close = meta.get("previousClose") or meta.get("chartPreviousClose")
        change_percent = meta.get("regularMarketChangePercent")
        if change_percent is None and price is not None and previous_close:
            change_percent = (price - previous_close) / previous_close * 100
        return {
            "price": price,
            "previous_close": previous_close,
            "change_percent": change_percent,
            "time": meta.get("regularMarketTime"),
        }

    return _cached_fetch(f"price_{pair}", loader, ttl=20)


@app.route("/api/live-prices")
@login_required
def get_live_prices():
    if not _load_settings()["live_prices_enabled"]:
        return jsonify({"pairs": {}, "error": "Live prices have been disabled by the admin"}), 403

    pairs_param = request.args.get("pairs")
    pairs = [p.strip().upper() for p in pairs_param.split(",")] if pairs_param else list(LIVE_PRICE_SYMBOLS)
    pairs = [p for p in pairs if p in LIVE_PRICE_SYMBOLS]

    def fetch_one(pair):
        try:
            return pair, fetch_live_price(pair)
        except RateLimited as e:
            return pair, {"error": f"rate limited, try again in {e.retry_after}s"}
        except NETWORK_ERRORS as e:
            return pair, {"error": f"failed to fetch: {e}"}

    # Each pair is an independent HTTP round-trip to Yahoo, so fetching sequentially
    # took ~20s for all 10; a small thread pool turns that into ~1 slowest request.
    if pairs:
        with ThreadPoolExecutor(max_workers=len(pairs)) as pool:
            result = dict(pool.map(fetch_one, pairs))
    else:
        result = {}

    return jsonify({"pairs": result})


@app.route("/api/social-sentiment")
@login_required
def get_social_sentiment():
    if not _load_settings()["social_sentiment_enabled"]:
        return jsonify({"pairs": {}, "error": "Social sentiment has been disabled by the admin"}), 403

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
@login_required
def home():
    is_admin = _load_users().get(session["username"], {}).get("is_admin", False)
    settings = _load_settings()
    enabled_sources = [
        {"key": key, "label": label}
        for key, label in SOURCE_LABELS.items() if settings["sources"].get(key, True)
    ]
    return render_template(
        "index.html",
        currencies=[{"code": c, "label": analysis.INSTRUMENT_LABELS[c]} for c in FILTERABLE_INSTRUMENTS],
        unavailable_sources=UNAVAILABLE_SOURCES,
        current_user=session["username"],
        is_admin=is_admin,
        enabled_sources=enabled_sources,
        social_sentiment_enabled=settings["social_sentiment_enabled"],
        market_bias_enabled=settings["market_bias_enabled"],
        live_prices_enabled=settings["live_prices_enabled"],
        settings_json=json.dumps(settings, sort_keys=True),
    )


@app.route("/api/settings")
@login_required
def get_settings():
    return jsonify(_load_settings())


@app.route("/api/news")
@login_required
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

    settings = _load_settings()
    allowed_sources = {key for key, enabled in settings["sources"].items() if enabled}
    wanted_sources = {s.strip().lower() for s in sources.split(",") if s.strip()} & allowed_sources
    events = []
    source_errors = {}

    def _fetch_source(name, fn):
        try:
            return fn()
        except RateLimited as e:
            source_errors[name] = f"rate limited, try again in {e.retry_after}s"
        except NETWORK_ERRORS as e:
            source_errors[name] = f"failed to fetch: {e}"
        return []

    if "forexfactory" in wanted_sources:
        for fetch_period in fetch_periods:
            try:
                ff_events = fetch_forexfactory(fetch_period)
            except RateLimited as e:
                source_errors["forexfactory"] = f"rate limited, try again in {e.retry_after}s"
                continue
            except NETWORK_ERRORS as e:
                # today/tomorrow merge >1 period -- one feed being briefly down
                # (e.g. next week's calendar not published yet) shouldn't fail
                # the whole request when another period may still have data.
                if len(fetch_periods) > 1:
                    continue
                source_errors["forexfactory"] = f"failed to fetch: {e}"
                continue
            if ff_events is None:
                return jsonify({"error": f"invalid period '{period}', use one of {['today', 'tomorrow'] + list(FF_FEEDS)}"}), 400
            events += ff_events

    if "fxstreet" in wanted_sources:
        events += _fetch_source("fxstreet", fetch_fxstreet_news)

    if "fxstreet_analysis" in wanted_sources:
        events += _fetch_source("fxstreet_analysis", fetch_fxstreet_analysis)

    if "investing" in wanted_sources:
        events += _fetch_source("investing", fetch_investing_news)

    if "myfxbook" in wanted_sources:
        events += _fetch_source("myfxbook", fetch_myfxbook_news)

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
        e["analysis"] = analysis.analyze_event(e) if settings["news_sentiment_enabled"] else None

    pair_bias = analysis.aggregate_pair_bias(events) if settings["market_bias_enabled"] else {}

    return jsonify({"count": len(events), "events": events, "pair_bias": pair_bias, "source_errors": source_errors})


def _event_time(event):
    raw = event.get("date")
    if not raw:
        return None
    return datetime.fromisoformat(raw)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "true").lower() == "true"
    app.run(debug=debug, host="0.0.0.0", port=port)
