"""Official UK procurement connectors (OCDS JSON APIs). No scraping.

Verified 2026-11-22 against the live services and their /apidocumentation pages:
  Find a Tender   GET https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages
                  params: updatedFrom, updatedTo (YYYY-MM-DDTHH:MM:SS), stages, limit<=100, cursor
  Contracts Finder GET https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search
                  params: publishedFrom, publishedTo (ISO, Z), stages, limit<=100, cursor
Both return OCDS release packages with links.next for pagination. Data is OGL v3.
"""
import time
from datetime import datetime, timedelta, timezone

import requests

SOURCES = {
    "fts": {
        "name": "Find a Tender",
        "url": "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages",
        "from": "updatedFrom", "to": "updatedTo", "fmt": "%Y-%m-%dT%H:%M:%S",
    },
    "cf": {
        "name": "Contracts Finder",
        "url": "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search",
        "from": "publishedFrom", "to": "publishedTo", "fmt": "%Y-%m-%dT%H:%M:%SZ",
    },
}


class SourceError(Exception):
    pass


def _get(session, url, params, cfg):
    delay = 2.0
    for attempt in range(4):
        try:
            r = session.get(url, params=params, timeout=cfg["timeout_seconds"])
        except requests.RequestException as e:
            err = f"network: {e.__class__.__name__}"
        else:
            if r.status_code == 200:
                return r.json()
            err = f"HTTP {r.status_code}"
            if r.status_code not in (429, 500, 502, 503, 504):
                raise SourceError(err)
            ra = r.headers.get("Retry-After", "")
            if ra.isdigit():
                delay = max(delay, min(int(ra), 120))
        if attempt < 3:
            time.sleep(delay)
            delay *= 2
    raise SourceError(err)


def fetch_releases(source, stage, since, until, cfg, session=None):
    """Yield (release, page_no) for one source/stage window, following links.next."""
    s = SOURCES[source]
    session = session or requests.Session()
    session.headers["User-Agent"] = cfg.get("user_agent", "FIC/1.0")
    params = {s["from"]: since.strftime(s["fmt"]), s["to"]: until.strftime(s["fmt"]),
              "stages": stage, "limit": cfg["page_limit"]}
    url = s["url"]
    for page in range(1, cfg["max_pages_per_run"] + 1):
        data = _get(session, url, params, cfg)
        for rel in data.get("releases", []):
            yield rel, page
        nxt = (data.get("links") or {}).get("next")
        if not nxt or not data.get("releases"):
            return
        url, params = nxt, None  # next link already carries all params + cursor
        time.sleep(cfg["request_delay_seconds"])


def window(watermark, lookback_days):
    until = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
    if watermark:
        since = datetime.fromisoformat(watermark) - timedelta(hours=1)  # overlap; dedup absorbs repeats
    else:
        since = until - timedelta(days=lookback_days)
    return since, until
