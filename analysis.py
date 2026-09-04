"""Rule-based heuristic analysis of news events -> which pairs they affect and which way.

This is NOT financial advice and does not predict real market moves; it just encodes
common trading-desk heuristics (a currency beating forecast tends to strengthen it,
"hawkish"/"dovish" language, unemployment being an inverse indicator, etc.) so the
news list can be scanned quickly. Treat it as a starting point, not a signal to trade on.

News articles are read as title + description/summary together, split into clauses
(on commas, "as", "while", "but", ...) so a single headline like "dollar weakens,
gold gains" is attributed correctly to BOTH USD (bearish) and Gold (bullish) instead
of being collapsed into one instrument.
"""
import re

CURRENCIES = ["USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD"]
INSTRUMENT_LABELS = {"XAU": "Gold", "XAG": "Silver", **{c: c for c in CURRENCIES}}

# base/quote for the pairs retail traders watch most, including Gold/Silver vs USD
PAIRS = {
    "EURUSD": ("EUR", "USD"),
    "GBPUSD": ("GBP", "USD"),
    "USDJPY": ("USD", "JPY"),
    "USDCHF": ("USD", "CHF"),
    "USDCAD": ("USD", "CAD"),
    "AUDUSD": ("AUD", "USD"),
    "NZDUSD": ("NZD", "USD"),
    "XAUUSD": ("XAU", "USD"),
    "XAGUSD": ("XAG", "USD"),
    # US Dollar Index -- USD's strength vs a currency basket, not a real base/quote
    # pair, but pair_bias() already handles a None quote correctly (it only ever
    # matches the `base` side, so DXY just tracks USD strength 1:1).
    "DXY": ("USD", None),
}

# indicators where a HIGHER number is actually bad news for the currency
INVERSE_INDICATOR_KEYWORDS = [
    "unemployment", "jobless", "claims", "redundanc", "trade deficit",
]

BULLISH_WORDS = [
    "rise", "rises", "rising", "risen", "gain", "gains", "gaining", "surge", "surges",
    "surging", "rally", "rallies", "rallying", "jump", "jumps", "jumping", "strengthen",
    "strengthens", "strengthening", "hawkish", "rate hike", "raises rates", "hikes rates",
    "beats forecast", "beats expectations", "better than expected", "climb", "climbs",
    "climbing", "soar", "soars", "soaring", "advance", "advances", "advancing", "bullish",
    "recovers", "recovery", "rebounds", "rebounding", "outperform", "outperforms",
    "higher", "up", "boosts", "boosted", "extends gains", "firmer", "firms",
    "increased", "increases", "increasing", "rose", "grew", "grows", "growing", "growth",
    "expanded", "expands", "expansion", "accelerated", "accelerates", "accelerating",
    "picked up", "inched up", "ticked up", "beat", "beats", "upbeat", "improves",
    "improved", "improvement", "improving", "widens", "widened", "widen",
]
BEARISH_WORDS = [
    "fall", "falls", "falling", "fallen", "drop", "drops", "dropping", "decline",
    "declines", "declining", "plunge", "plunges", "plunging", "slump", "slumps",
    "slumping", "weaken", "weakens", "weakening", "dovish", "rate cut", "cuts rates",
    "cuts interest rates", "misses forecast", "misses expectations", "worse than expected",
    "retreat", "retreats", "retreating", "bearish", "tumbles", "tumbling", "sinks",
    "sinking", "slides", "sliding", "lower", "down", "extends losses", "softer", "eases",
    "decreased", "decreases", "decreasing", "shrank", "shrinks", "contracted",
    "contraction", "slowed", "slows", "slowdown", "cooled", "cooling", "disappoints",
    "disappointing", "disappoint", "downbeat", "worsens", "worsened", "narrows",
    "narrowed", "narrowing",
]

NEGATION_WORDS = ["not", "no", "never", "fails to", "failed to", "unable to", "struggles to"]

# Trader slang used in social posts (StockTwits) that plain news headlines rarely use.
SOCIAL_BULLISH_WORDS = [
    "long", "longs", "buy", "buying", "bought", "moon", "mooning", "breakout",
    "buy the dip", "oversold", "accumulating", "loading up", "calls",
]
SOCIAL_BEARISH_WORDS = [
    "short", "shorts", "shorting", "sell", "selling", "sold", "dump", "dumping",
    "breakdown", "overbought", "distribution", "puts",
]
_SOCIAL_BULLISH_PATTERNS = [re.compile(r"\b" + re.escape(w) + r"\b") for w in SOCIAL_BULLISH_WORDS]
_SOCIAL_BEARISH_PATTERNS = [re.compile(r"\b" + re.escape(w) + r"\b") for w in SOCIAL_BEARISH_WORDS]

IMPACT_WEIGHT = {"High": 3, "Medium": 2, "Analysis": 2.5, "Low": 1, "News": 1, "Holiday": 0}

# Comma splits only OUTSIDE numbers, so "$4,500" doesn't get cut into "$4" / "500"
CLAUSE_SPLIT_RE = re.compile(
    r"(?<!\d),(?!\d)|;|(?: as )|(?: while )|(?: but )|(?: after )|(?: despite )|(?: amid )"
    r"|(?: although )|(?: even as )|(?: however )|\. "
)

# Headlines just as often name the COUNTRY ("US jobless claims", "Japan GDP") as the
# currency itself, so each instrument matches ISO code + common currency name + country
# name. Order matters: national-dollar/country variants are checked before the bare
# "dollar"/"US" -> USD fallback so e.g. "Canadian dollar" isn't read as USD.
_INSTRUMENT_PATTERNS = [
    ("XAU", re.compile(r"\bGOLD\b|\bXAU\b")),
    ("XAG", re.compile(r"\bSILVER\b|\bXAG\b")),
    ("AUD", re.compile(r"\bAUSTRALIAN\s+DOLLAR\b|\bAUSSIE\b|\bAUD\b|\bAUSTRALIA\b|\bAUSTRALIAN\b")),
    ("CAD", re.compile(r"\bCANADIAN\s+DOLLAR\b|\bLOONIE\b|\bCAD\b|\bCANADA\b|\bCANADIAN\b")),
    ("NZD", re.compile(r"\bNEW\s+ZEALAND\s+DOLLAR\b|\bKIWI\b|\bNZD\b|\bNEW\s+ZEALAND\b")),
    ("CHF", re.compile(r"\bSWISS\s+FRANC\b|\bSWISSY\b|\bCHF\b|\bSWITZERLAND\b|\bSWISS\b")),
    ("GBP", re.compile(r"\bBRITISH\s+POUND\b|\bSTERLING\b|\bPOUND\b|\bCABLE\b|\bGBP\b|\bUK\b|\bBRITAIN\b|\bBRITISH\b")),
    ("JPY", re.compile(r"\bYEN\b|\bJPY\b|\bJAPAN\b|\bJAPANESE\b")),
    ("EUR", re.compile(r"\bEURO\b|\bEUR\b|\bEUROZONE\b")),
    # "U\.S\.?" (trailing period optional) because clause-splitting on ". " can strip
    # the second period off "U.S." before this pattern ever sees the text.
    ("USD", re.compile(r"\bDOLLAR\b|\bGREENBACK\b|\bUSD\b|\bU\.S\.?|\bUS\b|\bUNITED\s+STATES\b|\bAMERICA\b|\bAMERICAN\b")),  # generic, checked last
]

# Precompiled word-boundary patterns so e.g. "gain" doesn't also match inside "gains"
# (both are separate list entries; substring matching double-counted that one word).
_BULLISH_PATTERNS = [re.compile(r"\b" + re.escape(w) + r"\b") for w in BULLISH_WORDS]
_BEARISH_PATTERNS = [re.compile(r"\b" + re.escape(w) + r"\b") for w in BEARISH_WORDS]

# Analyst "forecast" articles (FXStreet's Analysis feed) name the pair directly, e.g.
# "XAU/USD Price Forecast: Gold defies sellers around $4,500" -- matching the ticker
# straight away is more reliable than inferring it from two separate currency mentions.
_PAIR_TICKER_PATTERNS = {
    pair: re.compile(rf"\b{base}\s*/\s*{quote}\b") for pair, (base, quote) in PAIRS.items() if quote
}
_PAIR_TICKER_PATTERNS["DXY"] = re.compile(r"\bDXY\b|\bDOLLAR\s+INDEX\b|\bUSDX\b")


def _find_direct_pair(text):
    upper = text.upper()
    for pair, pattern in _PAIR_TICKER_PATTERNS.items():
        if pattern.search(upper):
            return pair
    return None


def _parse_number(raw):
    if not raw:
        return None
    text = raw.strip().replace(",", "").replace("%", "")
    multiplier = 1
    if text[-1:] in "KkMmBb":
        multiplier = {"K": 1e3, "M": 1e6, "B": 1e9}[text[-1].upper()]
        text = text[:-1]
    try:
        return float(text) * multiplier
    except ValueError:
        return None


def infer_instrument(text):
    """Detect the currency/commodity a piece of text is about, matching both ISO
    codes (USD, JPY, ...) and the plain-English names news headlines actually use
    (dollar, yen, aussie, ...)."""
    upper = text.upper()
    for instrument, pattern in _INSTRUMENT_PATTERNS:
        if pattern.search(upper):
            return instrument
    return "General"


def _is_negated(lowered_text, match_start, window=20):
    start = max(0, match_start - window)
    preceding = lowered_text[start:match_start]
    return any(re.search(rf"\b{re.escape(neg)}\b", preceding) for neg in NEGATION_WORDS)


def _sentiment_score(text):
    """Counts bullish/bearish keyword hits, flipping polarity for negated matches
    (e.g. 'fails to rise' counts as bearish, not bullish). Uses word-boundary
    matching so e.g. 'gain' doesn't also match inside the separately-listed 'gains'.
    Also flips for inverse indicators (jobless claims, unemployment, ...) where a
    rising number is bad news, same logic as the calendar-event comparison."""
    lowered = text.lower()
    bull = bear = 0
    for pattern in _BULLISH_PATTERNS:
        for m in pattern.finditer(lowered):
            if _is_negated(lowered, m.start()):
                bear += 1
            else:
                bull += 1
    for pattern in _BEARISH_PATTERNS:
        for m in pattern.finditer(lowered):
            if _is_negated(lowered, m.start()):
                bull += 1
            else:
                bear += 1

    if any(k in lowered for k in INVERSE_INDICATOR_KEYWORDS):
        bull, bear = bear, bull
    return bull, bear


_ABBREVIATION_RE = re.compile(r"\bU\.S\.|\bU\.K\.", re.IGNORECASE)


def _split_clauses(text):
    # Strip periods from "U.S."/"U.K." first so the ". " sentence-split below doesn't
    # sever the country name from the rest of its own clause (e.g. "U.S. Trade Deficit
    # Widens" would otherwise become two clauses, losing the USD attribution).
    text = _ABBREVIATION_RE.sub(lambda m: m.group(0).replace(".", ""), text)
    return [c.strip() for c in CLAUSE_SPLIT_RE.split(text) if c.strip()]


def _classify_calendar_event(event):
    forecast = _parse_number(event.get("forecast"))
    previous = _parse_number(event.get("previous"))
    if forecast is None or previous is None:
        return {"sentiment": "Neutral", "reason": "abhi forecast/previous ke figures maujood nahi hain, is liye compare nahi ho saka", "strength": 0.0}

    diff = forecast - previous
    if diff == 0:
        return {"sentiment": "Neutral", "reason": "forecast previous jaisa hi hai, koi tabdeeli nahi", "strength": 0.0}

    inverse = any(k in event["title"].lower() for k in INVERSE_INDICATOR_KEYWORDS)
    improving = diff > 0
    sentiment = "Bullish" if (improving != inverse) else "Bearish"

    trend = "zyada" if improving else "kam"
    reason = f"forecast ({event['forecast']}) previous ({event['previous']}) sy {trend} hai"
    if inverse:
        reason += " -- ye ulta (inverse) indicator hai, is liye zyada number currency ke liye bura hota hai"

    relative_move = abs(diff) / abs(previous) if previous else 0.5
    return {"sentiment": sentiment, "reason": reason, "strength": min(relative_move, 1.0)}


def pair_bias(instrument, sentiment):
    """Which pairs move, and which way, if `instrument` moves in `sentiment` direction."""
    if sentiment == "Neutral" or instrument not in INSTRUMENT_LABELS:
        return {}

    effects = {}
    for pair, (base, quote) in PAIRS.items():
        if instrument == base:
            effects[pair] = "Up" if sentiment == "Bullish" else "Down"
        elif instrument == quote:
            effects[pair] = "Down" if sentiment == "Bullish" else "Up"
    return effects


def _merge_pair_scores(signals):
    """Combines every directional signal's effect on each pair, weighted by strength,
    so pairs backed by multiple/stronger signals win and weak conflicting ones cancel
    out. A signal that named its pair directly (an analyst "XAU/USD Forecast") applies
    straight to that pair; one that only named a currency goes through pair_bias()."""
    scores = {}
    for sig in signals:
        weight = max(sig["strength"], 0.2)
        if sig.get("pair"):
            scores[sig["pair"]] = scores.get(sig["pair"], 0.0) + (weight if sig["sentiment"] == "Bullish" else -weight)
        else:
            for pair, direction in pair_bias(sig["instrument"], sig["sentiment"]).items():
                scores[pair] = scores.get(pair, 0.0) + (weight if direction == "Up" else -weight)
    return {pair: ("Up" if score > 0 else "Down") for pair, score in scores.items() if abs(score) > 1e-9}


def _analyze_news_text(title, description=""):
    combined = f"{title}. {description}" if description else title
    per_pair = {}        # pair ticker named directly -> [bull, bear]
    per_instrument = {}  # currency/commodity named -> [bull, bear]

    for clause in _split_clauses(combined):
        direct_pair = _find_direct_pair(clause)
        if direct_pair:
            bull, bear = _sentiment_score(clause)
            if bull == 0 and bear == 0:
                continue
            acc = per_pair.setdefault(direct_pair, [0, 0])
            acc[0] += bull
            acc[1] += bear
            continue

        instrument = infer_instrument(clause)
        if instrument == "General":
            continue
        bull, bear = _sentiment_score(clause)
        if bull == 0 and bear == 0:
            continue
        acc = per_instrument.setdefault(instrument, [0, 0])
        acc[0] += bull
        acc[1] += bear

    if not per_pair and not per_instrument:
        bull, bear = _sentiment_score(combined)
        if bull == bear:
            return {
                "sentiment": "Neutral", "reason": "koi clear direction wale lafz nahi milay, aur na hi koi specific currency/commodity ka zikar hai",
                "strength": 0.0, "instrument": None, "instrument_label": None, "affected": {}, "signals": [],
            }
        sentiment = "Bullish" if bull > bear else "Bearish"
        return {
            "sentiment": sentiment,
            "reason": f"headline mein {bull} bullish aur {bear} bearish lafz milay, lekin koi specific currency/commodity naam nahi li gayi",
            "strength": min(abs(bull - bear) / 3, 1.0),
            "instrument": None, "instrument_label": None, "affected": {}, "signals": [],
        }

    signals = []
    for pair, (bull, bear) in per_pair.items():
        sentiment = "Neutral" if bull == bear else ("Bullish" if bull > bear else "Bearish")
        signals.append({
            "pair": pair, "instrument": None, "instrument_label": pair,
            "sentiment": sentiment,
            "reason": f"{pair} (pair ka naam seedha mila): {bull} bullish aur {bear} bearish lafz",
            "strength": min(abs(bull - bear) / 3, 1.0) if bull != bear else 0.0,
        })
    for instrument, (bull, bear) in per_instrument.items():
        sentiment = "Neutral" if bull == bear else ("Bullish" if bull > bear else "Bearish")
        signals.append({
            "pair": None, "instrument": instrument, "instrument_label": INSTRUMENT_LABELS[instrument],
            "sentiment": sentiment,
            "reason": f"{INSTRUMENT_LABELS[instrument]} ke qareeb {bull} bullish aur {bear} bearish lafz milay",
            "strength": min(abs(bull - bear) / 3, 1.0) if bull != bear else 0.0,
        })

    directional = [s for s in signals if s["sentiment"] != "Neutral"]
    lead = max(directional, key=lambda s: s["strength"], default=signals[0])

    return {
        "sentiment": lead["sentiment"],
        "reason": "; ".join(s["reason"] for s in signals),
        "strength": lead["strength"],
        "instrument": lead["instrument"],
        "instrument_label": lead["instrument_label"],
        "affected": _merge_pair_scores(directional),
        "signals": signals,
    }


def analyze_event(event):
    """Returns None if the event has no identifiable currency/commodity to analyze
    (e.g. a calendar row like 'G20 Meetings' tagged country='All')."""
    if event["type"] == "calendar":
        instrument = event.get("country", "").upper()
        if instrument not in CURRENCIES:
            return None
        result = _classify_calendar_event(event)
        result["instrument"] = instrument
        result["instrument_label"] = INSTRUMENT_LABELS.get(instrument)
        result["affected"] = pair_bias(instrument, result["sentiment"])
        result["signals"] = [{
            "instrument": instrument, "instrument_label": result["instrument_label"],
            "sentiment": result["sentiment"], "reason": result["reason"], "strength": result["strength"],
        }]
        return result

    return _analyze_news_text(event["title"], event.get("description", ""))


def aggregate_pair_bias(events):
    """Net bias per pair across a set of already-analyzed events, weighted by each
    event's impact level (High/Medium/Analysis/Low/News) and how strong its own
    signal was. The bullish/bearish totals returned are these SAME weights, not raw
    event counts, so the displayed numbers always agree with the bias label -- a
    High-impact bearish release can outweigh several small bullish headlines."""
    scores = {pair: 0.0 for pair in PAIRS}
    bull_weight = {pair: 0.0 for pair in PAIRS}
    bear_weight = {pair: 0.0 for pair in PAIRS}
    counts = {pair: 0 for pair in PAIRS}

    for event in events:
        analysis = event.get("analysis")
        if not analysis or not analysis.get("affected"):
            continue
        weight = IMPACT_WEIGHT.get(event.get("impact"), 1) * (0.3 + 0.7 * analysis.get("strength", 0.5))
        for pair, direction in analysis["affected"].items():
            counts[pair] += 1
            if direction == "Up":
                scores[pair] += weight
                bull_weight[pair] += weight
            else:
                scores[pair] -= weight
                bear_weight[pair] += weight

    summary = {}
    for pair, score in scores.items():
        if score > 0.5:
            bias = "Bullish"
        elif score < -0.5:
            bias = "Bearish"
        else:
            bias = "Neutral"
        summary[pair] = {
            "bias": bias,
            "score": round(score, 2),
            "bullish_weight": round(bull_weight[pair], 1),
            "bearish_weight": round(bear_weight[pair], 1),
            "signal_count": counts[pair],
        }
    return summary


def classify_social_post(body, explicit_sentiment=None):
    """Classifies one social-media post about a pair. Prefers the platform's own
    explicit Bullish/Bearish tag (StockTwits lets posters tag their own call) since
    that's a real stated opinion, not a guess; falls back to trader-slang keyword
    scoring (long/short/buy/sell/...) plus the general bullish/bearish word lists."""
    if explicit_sentiment in ("Bullish", "Bearish"):
        return {"sentiment": explicit_sentiment, "reason": "poster ne khud is post ko tag kiya", "strength": 1.0}

    bull, bear = _sentiment_score(body)
    lowered = body.lower()
    for pattern in _SOCIAL_BULLISH_PATTERNS:
        bull += len(pattern.findall(lowered))
    for pattern in _SOCIAL_BEARISH_PATTERNS:
        bear += len(pattern.findall(lowered))

    if bull == bear:
        return {"sentiment": "Neutral", "reason": "post mein koi clear direction nahi", "strength": 0.0}
    sentiment = "Bullish" if bull > bear else "Bearish"
    return {
        "sentiment": sentiment,
        "reason": f"post ke lafzon sy {sentiment} lag raha hai ({bull} bullish vs {bear} bearish)",
        "strength": min(abs(bull - bear) / 3, 1.0),
    }


def aggregate_social_posts(posts):
    """posts: list of {'sentiment': 'Bullish'|'Bearish'|'Neutral', ...}. Returns overall
    crowd bias plus what share of directional posts were bullish, for display."""
    bullish = sum(1 for p in posts if p["sentiment"] == "Bullish")
    bearish = sum(1 for p in posts if p["sentiment"] == "Bearish")
    neutral = sum(1 for p in posts if p["sentiment"] == "Neutral")
    directional = bullish + bearish

    if directional == 0:
        bias = "Neutral"
        bullish_pct = None
    else:
        bullish_pct = round(100 * bullish / directional, 1)
        if bullish_pct >= 60:
            bias = "Bullish"
        elif bullish_pct <= 40:
            bias = "Bearish"
        else:
            bias = "Neutral"

    return {
        "bias": bias,
        "bullish_pct": bullish_pct,
        "bullish_count": bullish,
        "bearish_count": bearish,
        "neutral_count": neutral,
        "total": len(posts),
    }
