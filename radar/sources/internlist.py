"""Scraper for intern-list.com (?k=pm and ?k=cd feeds).

STATUS: UNVERIFIED. intern-list.com is blocked by the network egress policy of
the environment this was written in, so the page's real markup was never
observed. The parsing below covers the three shapes such a site realistically
serves, tries them in order, and returns an empty list rather than raising if
none match -- a broken feed must not take down the jobright source.

To finish this properly, run ``tools/probe_internlist.py`` (or the
"probe-internlist" workflow job, which runs on a GitHub runner with open
network access) and inspect the captured HTML, then tighten the selectors.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import List, Optional

import requests

from ..models import Posting

log = logging.getLogger(__name__)

BASE = "https://www.intern-list.com/"
# Candidate JSON endpoints, tried before falling back to HTML scraping.
API_CANDIDATES = [
    "https://www.intern-list.com/api/jobs?k={k}",
    "https://www.intern-list.com/api/listings?k={k}",
]

RELATIVE_AGE_RE = re.compile(
    r"(?P<n>\d+)\s*(?P<unit>minute|min|hour|hr|day|week)s?\s*ago", re.I
)


def stable_id(feed: str, url: str) -> str:
    """A dedup key that survives restarts.

    Python's builtin hash() is salted per process, so using it here would mint a
    fresh id every run and re-append every listing on every cron tick.
    """
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    return f"internlist:{feed}:{digest}"


def parse_relative_age(text: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """Turn '3 hours ago' / '2 days ago' into an absolute UTC timestamp."""
    now = now or datetime.now(timezone.utc)
    m = RELATIVE_AGE_RE.search(text or "")
    if not m:
        return None
    n = int(m.group("n"))
    unit = m.group("unit").lower()
    if unit.startswith(("minute", "min")):
        delta = timedelta(minutes=n)
    elif unit.startswith(("hour", "hr")):
        delta = timedelta(hours=n)
    elif unit.startswith("day"):
        delta = timedelta(days=n)
    else:
        delta = timedelta(weeks=n)
    return now - delta


class _RowExtractor(HTMLParser):
    """Collect table rows and their cell text, plus the first link per row.

    Uses the stdlib parser so the scraper has no BeautifulSoup dependency.
    """

    def __init__(self) -> None:
        super().__init__()
        self.rows: List[dict] = []
        self._row: Optional[dict] = None
        self._cell: Optional[List[str]] = None

    def handle_starttag(self, tag, attrs):
        attrs_d = dict(attrs)
        if tag == "tr":
            self._row = {"cells": [], "href": ""}
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag == "a" and self._row is not None and not self._row["href"]:
            self._row["href"] = attrs_d.get("href", "")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._row is not None and self._cell is not None:
            self._row["cells"].append(" ".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row["cells"]:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            text = data.strip()
            if text:
                self._cell.append(text)


def _from_json(payload, feed: str) -> List[Posting]:
    """Map a JSON listing feed onto Postings, tolerating key-name drift."""
    items = payload
    if isinstance(payload, dict):
        for key in ("jobs", "listings", "results", "data", "items"):
            if isinstance(payload.get(key), list):
                items = payload[key]
                break
    if not isinstance(items, list):
        return []

    def pick(d: dict, *names: str) -> str:
        for n in names:
            v = d.get(n)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""

    out: List[Posting] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = pick(item, "title", "job_title", "role", "position")
        company = pick(item, "company", "company_name", "employer", "org")
        url = pick(item, "url", "link", "apply_url", "job_url")
        if not title or not url:
            continue
        posted_raw = pick(item, "posted_at", "date_posted", "created_at", "posted", "age")
        posted = None
        if posted_raw:
            try:
                posted = datetime.fromisoformat(posted_raw.replace("Z", "+00:00"))
                if posted.tzinfo is None:
                    posted = posted.replace(tzinfo=timezone.utc)
            except ValueError:
                posted = parse_relative_age(posted_raw)

        out.append(Posting(
            job_id=(f"internlist:{pick(item, 'id')}" if pick(item, "id")
                    else stable_id(feed, url)),
            title=title, company=company, source=f"intern-list:{feed}",
            listing_url=url, location=pick(item, "location", "city", "place"),
            posted_at=posted,
            posted_precision="exact" if posted else "unknown",
        ))
    return out


def _from_html(html: str, feed: str) -> List[Posting]:
    parser = _RowExtractor()
    try:
        parser.feed(html)
    except Exception as exc:  # noqa: BLE001 - malformed markup must not abort the run
        log.warning("intern-list HTML parse error (%s)", exc)
        return []

    out: List[Posting] = []
    for row in parser.rows:
        cells = [c for c in row["cells"] if c]
        if len(cells) < 2 or not row["href"]:
            continue
        # Skip the header row.
        if cells[0].lower() in ("company", "role", "title", "position"):
            continue
        title = max(cells[:3], key=len)
        company = cells[0] if cells[0] != title else (cells[1] if len(cells) > 1 else "")
        posted = parse_relative_age(" ".join(cells))
        url = row["href"]
        if url.startswith("/"):
            url = BASE.rstrip("/") + url
        out.append(Posting(
            job_id=stable_id(feed, url),
            title=title, company=company, source=f"intern-list:{feed}",
            listing_url=url, posted_at=posted,
            posted_precision="exact" if posted else "unknown",
        ))
    return out


def fetch(feed: str, session: Optional[requests.Session] = None) -> List[Posting]:
    """Fetch one feed (``pm`` or ``cd``). Returns [] on any failure."""
    session = session or requests.Session()
    session.headers.setdefault("User-Agent", "Mozilla/5.0 (compatible; internship-radar/1.0)")

    for tmpl in API_CANDIDATES:
        url = tmpl.format(k=feed)
        try:
            resp = session.get(url, timeout=30)
            if resp.ok and "json" in resp.headers.get("Content-Type", ""):
                postings = _from_json(resp.json(), feed)
                if postings:
                    log.info("intern-list %s: %d listings via JSON (%s)", feed, len(postings), url)
                    return postings
        except (requests.RequestException, json.JSONDecodeError):
            continue

    try:
        resp = session.get(f"{BASE}?k={feed}", timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("intern-list %s unreachable: %s", feed, exc)
        return []

    postings = _from_html(resp.text, feed)
    if not postings:
        log.warning(
            "intern-list %s: parsed 0 listings. The page is likely JavaScript-rendered "
            "or the markup changed -- run tools/probe_internlist.py and update the parser.",
            feed,
        )
    else:
        log.info("intern-list %s: %d listings via HTML", feed, len(postings))
    return postings
