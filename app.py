import json
import os
import re
import secrets
import time
import xml.etree.ElementTree as ET
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
# A source can return HTTP 200 with a body that isn't what we expect (a Cloudflare
# challenge page instead of RSS, a truncated JSON response, an unexpected shape),
# which raise_for_status() doesn't catch. Treating these the same as a network
# failure means one misbehaving source reports itself as unavailable instead of
# taking down the whole /api/news request with an unhandled 500.
PARSE_ERRORS = (ET.ParseError, ValueError, KeyError, TypeError)

app = Flask(__name__)
# Flask's JSON provider sorts dict keys alphabetically by default, which silently
# scrambled the deliberate pair/instrument ordering (matching Live Prices' DXY,
# Gold, Silver, Bitcoin, Oil, then currencies) every time a dict went through
# jsonify() -- e.g. pair_bias always came back alphabetized regardless of what
# order analysis.PAIRS was actually built in.
app.json.sort_keys = False
# This repo is public, so a hardcoded fallback here would let anyone forge a
# signed session cookie (e.g. claiming admin) using a secret they can just read
# on GitHub. Production must set SECRET_KEY (e.g. in the WSGI file, which isn't
# git-tracked) for sessions to survive a restart; a per-process random key is
# still a safe fallback for local/dev use where losing sessions on restart is
# only a minor inconvenience, not a hole.
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "*/*",
}

CURRENCIES = ["USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD"]
# Same order as Live Prices: USD (-> DXY), Gold, Silver, Bitcoin, Oil first, then
# the rest of the currencies.
FILTERABLE_INSTRUMENTS = ["USD", "XAU", "XAG", "BTC", "WTI"] + [c for c in CURRENCIES if c != "USD"]

SOURCE_LABELS = {
    "forexfactory": "ForexFactory (calendar)",
    "fxstreet": "FXStreet (news)",
    "fxstreet_analysis": "FXStreet (analyst forecasts)",
    "investing": "Investing.com (news)",
    "investing_crypto": "Investing.com (crypto news)",
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
INVESTING_CRYPTO_RSS = "https://www.investing.com/rss/news_301.rss"  # category 301 = Cryptocurrency News
MYFXBOOK_CACHE_URL = "https://raw.githubusercontent.com/waqas-voipmen/news-api/master/myfxbook_cache.json"
FF_CACHE_URL = "https://raw.githubusercontent.com/waqas-voipmen/news-api/master/forexfactory_cache.json"
STOCKTWITS_STREAM_URL = "https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"
SOCIAL_PAIRS = list(analysis.PAIRS)  # StockTwits recognizes these same tickers directly (BTCUSD included)

# Reddit's search doesn't understand pair tickers as a "topic" the way StockTwits'
# cashtags do, so each pair gets a plain-English query instead, run across a
# handful of trading-focused subreddits.
REDDIT_SUBREDDITS = "Forex+Gold+Silverbugs+CryptoCurrency+investing+StockMarket+Economics+wallstreetbets"
REDDIT_SEARCH_QUERIES = {
    "DXY": 'DXY OR "dollar index"',
    "XAUUSD": "gold OR XAUUSD",
    "XAGUSD": "silver OR XAGUSD",
    "BTCUSD": "bitcoin OR BTC",
    "CL_F": '"crude oil" OR WTI',
    "EURUSD": "EURUSD",
    "GBPUSD": "GBPUSD OR cable",
    "USDJPY": "USDJPY",
    "USDCHF": "USDCHF",
    "USDCAD": "USDCAD",
    "AUDUSD": "AUDUSD",
    "NZDUSD": "NZDUSD",
}
REDDIT_SEARCH_URL = f"https://www.reddit.com/r/{REDDIT_SUBREDDITS}/search.json"

# Public channels' web preview (t.me/s/<name>) needs no login or API token, but
# also isn't per-pair -- each channel's whole recent feed is fetched once and
# cached, then filtered per pair the same way News detects an instrument from
# free text (analysis.infer_instrument), matched against analysis.pair_identity.
TELEGRAM_CHANNELS = ["FXStreetNews", "Cointelegraph"]

# Live prices are shown via TradingView's own embeddable widgets (see
# templates/live_prices.html) rather than fetched server-side -- that gives
# real spot XAUUSD/XAGUSD/DXY ticks straight from TradingView's feed instead of
# an approximated proxy, with no scraping, rate limits, or backend fetch at all.
TRADINGVIEW_SYMBOLS = {
    "DXY": "CAPITALCOM:DXY",
    "XAUUSD": "FOREXCOM:XAUUSD",
    "XAGUSD": "FOREXCOM:XAGUSD",
    # Coinbase's BTCUSD feed doesn't carry the previous-close data this widget
    # needs, so it silently renders with no % change line; Bitstamp's does.
    "BTCUSD": "BITSTAMP:BTCUSD",
    "USOIL": "TVC:USOIL",
    "EURUSD": "OANDA:EURUSD",
    "GBPUSD": "OANDA:GBPUSD",
    "USDJPY": "OANDA:USDJPY",
    "USDCHF": "OANDA:USDCHF",
    "USDCAD": "OANDA:USDCAD",
    "AUDUSD": "OANDA:AUDUSD",
    "NZDUSD": "OANDA:NZDUSD",
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


def _get_theme():
    # Dark is the default look for a first-time visitor (no cookie yet) --
    # only an explicit "light" choice from the theme toggle opts back out.
    return "light" if request.cookies.get("theme") == "light" else "dark"


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = _load_users().get(username)
        if user and check_password_hash(user["password_hash"], password):
            session["username"] = username
            return redirect(request.args.get("next") or url_for("news_page"))
        error = "Invalid username or password"
    return render_template("login.html", error=error, theme=_get_theme())


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
        theme=_get_theme(),
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
        theme=_get_theme(),
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
    except (*NETWORK_ERRORS, *PARSE_ERRORS, RateLimited):
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
            if period in ("lastweek", "nextweek"):
                raise requests.RequestException(
                    "ForexFactory's free feed only publishes the current week's calendar -- "
                    "last/next week's events aren't available from this source"
                )
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


def fetch_investing_crypto_news():
    return _fetch_rss_news(INVESTING_CRYPTO_RSS, "Investing.com (Crypto)", "rss_investing_crypto")


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


def fetch_stocktwits_sentiment(pair):
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
                "source": "StockTwits",
            })
        return posts

    return _cached_fetch(f"social_{pair}", loader)


def fetch_reddit_sentiment(pair):
    """Reddit's search doesn't recognize pair tickers as cashtags, so this runs a
    plain-English query (REDDIT_SEARCH_QUERIES) across a handful of trading
    subreddits instead, then classifies each result's title+selftext the same
    keyword-scoring way as a StockTwits post with no explicit tag. Free and
    needs no API token, but Reddit's own bot-detection blocks non-browser
    traffic aggressively -- routed through the same Cloudflare Worker proxy
    used for other sources PythonAnywhere can't reach directly."""
    query = REDDIT_SEARCH_QUERIES.get(pair)
    if not query:
        return []

    def loader():
        url = f"{REDDIT_SEARCH_URL}?q={quote(query)}&restrict_sr=on&sort=new&limit=15&t=month"
        response = requests.get(_via_proxy(url), headers=HEADERS, timeout=10)
        if response.status_code == 429:
            raise RateLimited(int(response.headers.get("Retry-After", 60)))
        response.raise_for_status()
        data = response.json()

        posts = []
        for child in data.get("data", {}).get("children", []):
            item = child.get("data") or {}
            title = item.get("title", "")
            selftext = (item.get("selftext") or "")[:280]
            body = f"{title}. {selftext}".strip(". ")
            if not body:
                continue
            result = analysis.classify_social_post(body)
            created = item.get("created_utc")
            permalink = item.get("permalink")
            posts.append({
                "sentiment": result["sentiment"],
                "reason": result["reason"],
                "body": body,
                "user": item.get("author"),
                "date": datetime.fromtimestamp(created, tz=timezone.utc).isoformat() if created else None,
                "source": "Reddit",
                "link": f"https://www.reddit.com{permalink}" if permalink else None,
            })
        return posts

    return _cached_fetch(f"reddit_{pair}", loader)


def fetch_telegram_posts():
    """Fetches each configured public channel's web preview (t.me/s/<name> --
    no login or bot token needed) ONCE, cached, covering every pair at once;
    the caller filters this same list down per pair by keyword afterward,
    same as fetch_reddit_sentiment's callers do for the News page's instrument
    detection. One channel failing doesn't take the others down with it."""
    from bs4 import BeautifulSoup

    posts = []
    last_error = None
    for channel in TELEGRAM_CHANNELS:
        def loader(channel=channel):
            url = f"https://t.me/s/{channel}"
            # Unlike Reddit, Telegram's web preview is directly reachable from
            # PythonAnywhere -- routing it through the Cloudflare Worker proxy
            # actually breaks it (403), so this skips _via_proxy() on purpose.
            response = requests.get(url, headers=HEADERS, timeout=10)
            if response.status_code == 429:
                raise RateLimited(int(response.headers.get("Retry-After", 60)))
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            channel_posts = []
            for wrap in soup.select(".tgme_widget_message_wrap"):
                text_el = wrap.select_one(".tgme_widget_message_text")
                if not text_el:
                    continue
                body = text_el.get_text(" ", strip=True)[:400]
                if not body:
                    continue
                time_el = wrap.select_one("time.time")
                date = time_el.get("datetime") if time_el else None
                result = analysis.classify_social_post(body)
                channel_posts.append({
                    "sentiment": result["sentiment"],
                    "reason": result["reason"],
                    "body": body,
                    "user": channel,
                    "date": date,
                    "source": "Telegram",
                })
            return channel_posts

        try:
            posts += _cached_fetch(f"telegram_{channel}", loader)
        except (RateLimited, *NETWORK_ERRORS, *PARSE_ERRORS) as e:
            last_error = e
            continue
    # Only surface a failure if EVERY channel failed -- one dead channel
    # shouldn't hide posts the others returned just fine. If at least one
    # succeeded (even with zero matching posts), that's not an error.
    if not posts and last_error is not None:
        raise last_error
    return posts


@app.route("/api/social-sentiment")
@login_required
def get_social_sentiment():
    if not _load_settings()["social_sentiment_enabled"]:
        return jsonify({"pairs": {}, "error": "Social sentiment has been disabled by the admin"}), 403

    pairs_param = request.args.get("pairs")
    pairs = [p.strip().upper() for p in pairs_param.split(",")] if pairs_param else SOCIAL_PAIRS
    pairs = [p for p in pairs if p in SOCIAL_PAIRS]

    # The shared Pairs filter chips use instrument-level codes (USD, XAU, WTI, ...),
    # not raw StockTwits tickers (EURUSD, XAUUSD, CL_F) -- same pair_identity match
    # already used to narrow Market Bias's pair_bias.
    currencies = request.args.get("currencies")
    if currencies:
        wanted_currencies = {c.strip().upper() for c in currencies.split(",") if c.strip()}
        pairs = [p for p in pairs if analysis.pair_identity(p) in wanted_currencies]

    # StockTwits' stream endpoint only ever returns its ~30 most recent posts for a
    # symbol (no deep history), so this can only narrow DOWN to a period, not fetch
    # further back than that -- e.g. "Last Week" may show fewer/no posts if none of
    # the most recent 30 happen to fall in that window.
    period = request.args.get("period", "week")
    raw_from, raw_to = _period_date_range(period, request.args.get("from"), request.args.get("to"))
    date_from = _parse_iso(raw_from) if raw_from else None
    date_to = _parse_iso(raw_to) if raw_to else None

    # Telegram isn't fetched per-pair (each channel's whole recent feed is one
    # request), so it's pulled once up front and filtered per pair below,
    # same instrument-detection match already used for Market Bias/Live Prices.
    telegram_posts = []
    source_errors = {}
    try:
        telegram_posts = fetch_telegram_posts()
    except (*NETWORK_ERRORS, *PARSE_ERRORS) as e:
        source_errors["telegram"] = f"failed to fetch: {e}"

    def _in_range(post):
        if not post.get("date"):
            return False
        post_time = _parse_iso(post["date"])
        if date_from and post_time < date_from:
            return False
        if date_to and post_time > date_to:
            return False
        return True

    result = {}
    for pair in pairs:
        posts = []

        try:
            posts += fetch_stocktwits_sentiment(pair)
        except RateLimited as e:
            source_errors.setdefault("stocktwits", f"rate limited, try again in {e.retry_after}s")
        except (*NETWORK_ERRORS, *PARSE_ERRORS) as e:
            source_errors.setdefault("stocktwits", f"failed to fetch: {e}")

        try:
            posts += fetch_reddit_sentiment(pair)
        except RateLimited as e:
            source_errors.setdefault("reddit", f"rate limited, try again in {e.retry_after}s")
        except (*NETWORK_ERRORS, *PARSE_ERRORS) as e:
            source_errors.setdefault("reddit", f"failed to fetch: {e}")

        pair_identity = analysis.pair_identity(pair)
        posts += [p for p in telegram_posts if analysis.infer_instrument(p["body"]) == pair_identity]

        if date_from or date_to:
            posts = [p for p in posts if _in_range(p)]

        summary = analysis.aggregate_social_posts(posts)
        # Newest first across all three sources combined, not just whichever
        # source's own posts happened to come first in the merge above.
        directional = sorted(
            (p for p in posts if p["sentiment"] != "Neutral"),
            key=lambda p: p.get("date") or "",
            reverse=True,
        )
        summary["sample_posts"] = directional[:5]
        result[pair] = summary

    return jsonify({"pairs": result, "source_errors": source_errors})


# Every Live Prices key is spelled exactly like its analysis.PAIRS key already
# (EURUSD, XAUUSD, DXY, ...) except crude oil, which analysis.PAIRS keys as
# "CL_F" (StockTwits' real ticker) instead of "USOIL".
_TV_KEY_TO_PAIR = {"USOIL": "CL_F"}


def _tv_pair_matches(code, tv_key):
    """Whether a Pairs-filter instrument code (USD, XAU, WTI, ...) matches a Live
    Prices TradingView key (EURUSD, XAUUSD, USOIL, DXY, ...) -- same pair_identity
    match used to narrow Market Bias and Social Sentiment, so e.g. selecting "USD"
    alone matches only DXY here too, not every USD-quoted instrument on the page."""
    return analysis.pair_identity(_TV_KEY_TO_PAIR.get(tv_key, tv_key)) == code


def _page_context(active_page, **extra):
    """Template variables shared by every page: header/nav chrome, the filter bar's
    options, and feature-flag/theme state. Each page's filters are independent --
    the nav links deliberately carry no query string, so applying a filter on one
    page never bleeds into another (see base.html)."""
    is_admin = _load_users().get(session["username"], {}).get("is_admin", False)
    settings = _load_settings()
    enabled_sources = [
        {"key": key, "label": label}
        for key, label in SOURCE_LABELS.items() if settings["sources"].get(key, True)
    ]
    theme = _get_theme()
    ctx = dict(
        active_page=active_page,
        currencies=[{"code": c, "label": analysis.INSTRUMENT_LABELS[c]} for c in FILTERABLE_INSTRUMENTS],
        unavailable_sources=UNAVAILABLE_SOURCES,
        current_user=session["username"],
        is_admin=is_admin,
        enabled_sources=enabled_sources,
        social_sentiment_enabled=settings["social_sentiment_enabled"],
        market_bias_enabled=settings["market_bias_enabled"],
        live_prices_enabled=settings["live_prices_enabled"],
        settings_json=json.dumps(settings, sort_keys=True),
        theme=theme,
        tradingview_color_theme=theme,
        hide_sources=False,
        default_period="week",
    )
    ctx.update(extra)
    return ctx


@app.route("/")
@login_required
def news_page():
    return render_template("news.html", **_page_context("news"))


@app.route("/live-prices")
@login_required
def live_prices_page():
    selected = request.args.get("currencies")
    codes = [c.strip().upper() for c in selected.split(",") if c.strip()] if selected else None
    pairs = list(TRADINGVIEW_SYMBOLS.items())
    if codes:
        pairs = [(pair, sym) for pair, sym in pairs if any(_tv_pair_matches(c, pair) for c in codes)]
    return render_template(
        "live_prices.html",
        tradingview_pairs=pairs,
        **_page_context("live_prices"),
    )


@app.route("/market-bias")
@login_required
def market_bias_page():
    return render_template(
        "market_bias.html",
        **_page_context("market_bias", hide_sources=True, default_period="today"),
    )


@app.route("/social-sentiment")
@login_required
def social_sentiment_page():
    return render_template(
        "social_sentiment.html",
        **_page_context("social_sentiment", hide_sources=True, default_period="today"),
    )


@app.route("/api/settings")
@login_required
def get_settings():
    return jsonify(_load_settings())


def _period_date_range(period, date_from, date_to):
    """Derives a (date_from, date_to) ISO range for a period, unless the caller
    already gave an explicit range. Shared by /api/news and /api/social-sentiment
    so picking e.g. "Last Week" narrows both sections the same way."""
    if date_from or date_to:
        return date_from, date_to

    if period == "tomorrow":
        target_date = datetime.now(timezone.utc).date() + timedelta(days=1)
        date_from = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc).isoformat()
        date_to = datetime.combine(target_date, datetime.max.time(), tzinfo=timezone.utc).isoformat()
    elif period == "today":
        target_date = datetime.now(timezone.utc).date()
        date_from = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc).isoformat()
        date_to = datetime.combine(target_date, datetime.max.time(), tzinfo=timezone.utc).isoformat()
    elif period in ("week", "lastweek", "nextweek"):
        # ForexFactory's own calendar feed is scoped Sun-Fri per week, but the other
        # sources (FXStreet/Investing/Myfxbook/StockTwits) are plain recent-post
        # feeds with no built-in notion of "last/next week" -- without this,
        # picking "Last Week" or "Next Week" left every one of them showing the
        # same generic "recent" items regardless of which period was selected.
        today = datetime.now(timezone.utc).date()
        days_since_sunday = (today.weekday() + 1) % 7  # Mon=0..Sun=6 -> Sun=0..Sat=6
        this_sunday = today - timedelta(days=days_since_sunday)
        week_offset = {"lastweek": -7, "week": 0, "nextweek": 7}[period]
        week_start = this_sunday + timedelta(days=week_offset)
        week_end = week_start + timedelta(days=5)  # Sunday through Friday
        date_from = datetime.combine(week_start, datetime.min.time(), tzinfo=timezone.utc).isoformat()
        date_to = datetime.combine(week_end, datetime.max.time(), tzinfo=timezone.utc).isoformat()

    return date_from, date_to


@app.route("/api/news")
@login_required
def get_news():
    period = request.args.get("period", "week")
    sources = request.args.get("sources", "forexfactory,fxstreet,fxstreet_analysis,investing,investing_crypto,myfxbook")
    currencies = request.args.get("currencies")  # comma separated, e.g. "USD,CAD"
    impact = request.args.get("impact")
    date_from = request.args.get("from")  # ISO datetime, e.g. 2026-09-03T00:00
    date_to = request.args.get("to")

    # "today"/"tomorrow" aren't their own feeds -- pull the underlying week(s) so
    # the events used to derive the date-filtered day actually exist. "tomorrow"
    # can fall in either this week's or next week's feed (e.g. today is Sunday),
    # so both are fetched and merged to be safe.
    fetch_periods = [period]
    if period in ("today", "tomorrow"):
        fetch_periods = ["week", "nextweek"] if period == "tomorrow" else ["week"]

    date_from, date_to = _period_date_range(period, date_from, date_to)

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
        except (*NETWORK_ERRORS, *PARSE_ERRORS) as e:
            source_errors[name] = f"failed to fetch: {e}"
        return []

    if "forexfactory" in wanted_sources:
        for fetch_period in fetch_periods:
            try:
                ff_events = fetch_forexfactory(fetch_period)
            except RateLimited as e:
                source_errors["forexfactory"] = f"rate limited, try again in {e.retry_after}s"
                continue
            except (*NETWORK_ERRORS, *PARSE_ERRORS) as e:
                # today/tomorrow merge >1 period -- one feed being briefly down
                # (e.g. next week's calendar not published yet) shouldn't fail
                # the whole request when another period may still have data.
                if len(fetch_periods) > 1:
                    continue
                # lastweek/nextweek are a permanent gap in ForexFactory's free feed
                # (it only ever publishes the current week), not a transient
                # failure -- phrase it as a plain note rather than an error.
                if fetch_period in ("lastweek", "nextweek"):
                    source_errors["forexfactory"] = str(e)
                else:
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

    if "investing_crypto" in wanted_sources:
        events += _fetch_source("investing_crypto", fetch_investing_crypto_news)

    if "myfxbook" in wanted_sources:
        events += _fetch_source("myfxbook", fetch_myfxbook_news)

    if impact:
        events = [e for e in events if e.get("impact", "").lower() == impact.lower()]

    if date_from:
        start = _parse_iso(date_from)
        events = [e for e in events if (t := _event_time(e)) and t >= start]

    if date_to:
        end = _parse_iso(date_to)
        events = [e for e in events if (t := _event_time(e)) and t <= end]

    events = [e for e in events if e.get("date")]
    # ForexFactory (the economic calendar) sorts ahead of the RSS sources --
    # applies whether or not any filter is active, since this is the same
    # sort every /api/news call goes through regardless of the request's
    # query params.
    events.sort(key=lambda e: (e.get("source") != "ForexFactory", e["date"]))

    for e in events:
        e["analysis"] = analysis.analyze_event(e) if settings["news_sentiment_enabled"] else None

    # Bias is aggregated from EVERY event above, before the Pairs filter narrows
    # `events` down below -- an event's own top-level `country` tag is just
    # whichever single instrument its headline names first, but analyze_event's
    # per-clause reading can still attribute it to other pairs too (e.g. a
    # "dollar weakens, gold gains" headline tagged country=USD still carries a
    # real XAUUSD signal). Aggregating post-filter would silently drop those
    # cross-references, leaving commodity/crypto pairs stuck at 0 signals
    # whenever a currency other than their own was the one selected.
    pair_bias = analysis.aggregate_pair_bias(events) if settings["market_bias_enabled"] else {}

    wanted_currencies = {c.strip().upper() for c in currencies.split(",") if c.strip()} if currencies else None
    if wanted_currencies:
        # aggregate_pair_bias always returns all of analysis.PAIRS (so a pair with
        # zero matching signals still shows as a "Neutral, 0 signals" card) --
        # the Pairs filter is expected to actually hide cards for pairs that
        # don't match a selected currency/instrument, not just zero them out.
        # Matching on pair_identity (not "either leg") keeps selecting USD alone
        # from pulling in every pair on the board -- USD is a leg of all twelve.
        pair_bias = {
            pair: info for pair, info in pair_bias.items()
            if analysis.pair_identity(pair) in wanted_currencies
        }
        events = [e for e in events if e.get("country", "").upper() in wanted_currencies]

    return jsonify({"count": len(events), "events": events, "pair_bias": pair_bias, "source_errors": source_errors})


def _parse_iso(value):
    """Parses an ISO datetime string, including a trailing "Z" (as produced by
    JS's Date.toISOString()) -- datetime.fromisoformat() only accepts "Z"
    natively on Python 3.11+, and this app also runs on 3.10. A naive result
    (no offset at all) is treated as UTC rather than left ambiguous."""
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _event_time(event):
    raw = event.get("date")
    if not raw:
        return None
    return datetime.fromisoformat(raw)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "true").lower() == "true"
    app.run(debug=debug, host="0.0.0.0", port=port)
