"""Run on a machine with a residential IP (Myfxbook's Cloudflare check blocks
datacenter/cloud IPs regardless of TLS fingerprint). Scrapes Myfxbook's news
page and writes the result to myfxbook_cache.json so the deployed app -- which
runs on a cloud host Myfxbook blocks -- can read pre-fetched data from GitHub
instead of hitting Myfxbook directly."""
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

MYFXBOOK_NEWS_URL = "https://www.myfxbook.com/news"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "*/*",
}

sys.path.insert(0, str(Path(__file__).parent.parent))
import analysis  # noqa: E402

OUTPUT_PATH = Path(__file__).parent.parent / "myfxbook_cache.json"


def _parse_relative_time(text):
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


def fetch():
    response = curl_requests.get(MYFXBOOK_NEWS_URL, headers=HEADERS, impersonate="chrome124", timeout=15)
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


if __name__ == "__main__":
    events = fetch()
    OUTPUT_PATH.write_text(json.dumps({"fetched_at": datetime.now(timezone.utc).isoformat(), "events": events}, indent=2))
    print(f"Wrote {len(events)} events to {OUTPUT_PATH}")
