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
other (non-ForexFactory) sources instead."""
import json
from datetime import datetime, timezone
from pathlib import Path

import requests

FF_FEEDS = {
    "week": "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
}
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


def fetch():
    result = _previous_periods()
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
        result[period] = events
    return result


if __name__ == "__main__":
    periods = fetch()
    OUTPUT_PATH.write_text(json.dumps({"fetched_at": datetime.now(timezone.utc).isoformat(), "periods": periods}, indent=2))
    counts = {p: len(events) for p, events in periods.items()}
    print(f"Wrote {counts} to {OUTPUT_PATH}")
