"""Scraper for intern-list.com.

The site itself contains no job data. Every listing renders inside an iframe,
so scraping the landing page's HTML yields nothing -- an earlier version of this
module tried exactly that and was always going to fail.

The page's own JavaScript builds the iframe URL from a ``data-job-path``
attribute on each category:

    const jobPath = item.getAttribute("data-job-path");   // "/us/product_management"
    let newUrl = `https://jobright.ai/minisites-jobs/intern/${cleanPath}?embed=true`;

so that embed URL is what this module fetches. Each category also carries an
``airtable-link`` fallback embed, kept below as a second path.

Note that intern-list.com is operated by jobright.ai, the same source as the
GitHub repos in sources/jobright.py. Listings overlap; main.py de-duplicates by
job id across all sources. The value here is breadth -- the repos are explicitly
"a fraction of available intern positions" while the site advertises tens of
thousands.

STATUS: the URL construction above is read directly from the live page and is
correct. What the embed endpoint *returns* has not been observed, because
jobright.ai is blocked by network policy where this was written. The response
handling below covers the realistic shapes and degrades to an empty list.
Run tools/probe_internlist.py from a runner to capture the real response.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

from ..models import Posting

log = logging.getLogger(__name__)

EMBED = "https://jobright.ai/minisites-jobs/intern/{path}?embed=true"

# short-link -> data-job-path, both read from the live page's markup.
# The "k" query parameter on intern-list.com URLs is the short link, so
# ?k=pm is the product_management feed.
FEEDS: Dict[str, str] = {
    "pm": "us/product_management",
    "cd": "us/creatives_design",
    "cst": "us/consulting",
    "ba": "us/business_analyst",
    "da": "us/data_analysis",
    "me": "us/management_executive",
    "swe": "us/swe",
}

# Each category's Airtable fallback embed, also from the live page.
AIRTABLE: Dict[str, str] = {
    "pm": "apprzZO4NFGouLji9/shrApQMVthWyRpdyu",
    "cd": "appTrsSSLTPwEjG9Q/shrMZcqheJOCdXyQj",
    "cst": "appvVpV9JOUFrdDyu/shrPvpTT69P8fVy6H",
    "ba": "appFuMULJB6cXtL6L/shrabXspvMx1kfuQw",
    "da": "appbsiP1flCoaXCSm/shreRS1cFLbduwBaU",
}

RELATIVE_AGE_RE = re.compile(
    r"(?P<n>\d+)\s*(?P<unit>minute|min|hour|hr|day|week)s?\s*ago", re.I
)
JOBRIGHT_ID_RE = re.compile(r"[0-9a-f]{24}")


def stable_id(feed: str, url: str) -> str:
    """A dedup key that survives restarts.

    Python's builtin hash() is salted per process, so using it here would mint a
    fresh id every run and re-append every listing on every cron tick. Where the
    URL carries jobright's own 24-hex id, reuse it -- that makes listings from
    this source de-duplicate against the same listings from the GitHub repos.
    """
    native = JOBRIGHT_ID_RE.search(url or "")
    if native:
        return native.group(0)
    digest = hashlib.sha1((url or feed).encode("utf-8")).hexdigest()[:16]
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


def _parse_timestamp(raw: str) -> Optional[datetime]:
    if not raw:
        return None
    raw = raw.strip()
    if raw.isdigit():  # epoch seconds or milliseconds
        value = int(raw)
        if value > 1_000_000_000_000:
            value //= 1000
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return parse_relative_age(raw)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _pick(d: dict, *names: str) -> str:
    for n in names:
        v = d.get(n)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (int, float)):
            return str(v)
    return ""


def _walk_for_listings(node, out: List[dict], depth: int = 0) -> None:
    """Find listing-shaped dicts anywhere in a JSON payload.

    The embed's response envelope is unknown, so rather than guess at a key path
    this looks for any object carrying both a title-ish and a company-ish field.
    """
    if depth > 8:
        return
    if isinstance(node, dict):
        has_title = any(k in node for k in ("jobTitle", "title", "job_title", "positionName"))
        has_company = any(k in node for k in ("companyName", "company", "employerName", "company_name"))
        if has_title and has_company:
            out.append(node)
            return
        for value in node.values():
            _walk_for_listings(value, out, depth + 1)
    elif isinstance(node, list):
        for item in node:
            _walk_for_listings(item, out, depth + 1)


def postings_from_payload(payload, feed: str) -> List[Posting]:
    """Map whatever JSON the embed returns onto Postings."""
    found: List[dict] = []
    _walk_for_listings(payload, found)

    out: List[Posting] = []
    for item in found:
        title = _pick(item, "jobTitle", "title", "job_title", "positionName")
        company = _pick(item, "companyName", "company", "employerName", "company_name")
        if not title or not company:
            continue
        url = _pick(item, "applyLink", "jobUrl", "url", "link", "apply_url", "originalUrl")
        native_id = _pick(item, "jobId", "id", "job_id")
        posted = _parse_timestamp(
            _pick(item, "publishTimeDesc", "publishTime", "postedAt", "postDate",
                  "createTime", "publishedAt")
        )
        out.append(Posting(
            job_id=(native_id if JOBRIGHT_ID_RE.fullmatch(native_id) else stable_id(feed, url or title + company)),
            title=title,
            company=company,
            source=f"intern-list:{feed}",
            listing_url=url,
            location=_pick(item, "jobLocation", "location", "city", "workLocation"),
            work_model=_pick(item, "workModel", "workday", "remote"),
            posted_at=posted,
            posted_precision="exact" if posted else "unknown",
        ))
    return out


def _embedded_json(html: str):
    """Pull the largest JSON blob out of a server-rendered app shell."""
    for pattern in (r'__NEXT_DATA__[^>]*>(\{.*?\})</script>',
                    r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});',
                    r'<script[^>]*type="application/json"[^>]*>(\{.*?\})</script>'):
        for match in re.finditer(pattern, html, re.S):
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
    return None


def fetch(feed: str, session: Optional[requests.Session] = None) -> List[Posting]:
    """Fetch one feed by short link (``pm``, ``cd``, ``cst`` ...).

    Returns [] on any failure -- this source must never take down the run.
    """
    path = FEEDS.get(feed)
    if not path:
        log.warning("unknown intern-list feed %r; known: %s", feed, ", ".join(sorted(FEEDS)))
        return []

    session = session or requests.Session()
    session.headers.setdefault("User-Agent", "Mozilla/5.0 (compatible; internship-radar/1.0)")

    url = EMBED.format(path=path)
    try:
        resp = session.get(url, timeout=30, headers={"Accept": "application/json, text/html"})
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("intern-list %s unreachable (%s)", feed, exc)
        return []

    payload = None
    if "json" in resp.headers.get("Content-Type", ""):
        try:
            payload = resp.json()
        except json.JSONDecodeError:
            payload = None
    if payload is None:
        payload = _embedded_json(resp.text)

    if payload is None:
        log.warning(
            "intern-list %s: %s returned no parseable JSON (%d bytes). The embed is "
            "probably client-rendered; run tools/probe_internlist.py to capture the "
            "real response and its XHR endpoint.", feed, url, len(resp.text),
        )
        return []

    postings = postings_from_payload(payload, feed)
    if postings:
        log.info("intern-list %s: %d listings", feed, len(postings))
    else:
        log.warning("intern-list %s: payload parsed but held no listings", feed)
    return postings
