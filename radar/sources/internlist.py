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

The embed serves server-rendered HTML with every listing in a __NEXT_DATA__
script block at ``props.pageProps.initialJobs``, 50 per feed. That was
confirmed against captured responses for all seven feeds.
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
    return ""


def _clean_qualifications(raw: str) -> str:
    """Flatten jobright's numbered qualifications blob into one line."""
    if not raw:
        return ""
    parts = re.split(r"\s*\d{1,2}\.\s+", raw)
    items = [re.sub(r"\s+", " ", part).strip(" ;.") for part in parts if part.strip()]
    return "; ".join(items)[:1800]


def _notes_from(job: dict) -> str:
    """Facts the feed supplies outright that no other source does."""
    notes = []
    salary = _pick(job, "salary")
    if salary and salary.upper() not in ("N/A", "NA", "NOT SPECIFIED"):
        notes.append("Pay: " + salary)
    grad = _pick(job, "graduateTime")
    if grad:
        notes.append("Graduates: " + grad.replace("/", " or "))
    if _pick(job, "h1bSponsored").lower() == "no":
        notes.append("No visa sponsorship")
    size = _pick(job, "companySize")
    if size:
        notes.append("Company size: " + size)
    return " \u00b7 ".join(notes)


def postings_from_payload(payload, feed: str) -> List[Posting]:
    """Map the embed's __NEXT_DATA__ payload onto Postings.

    Job objects live at ``props.pageProps.initialJobs`` and carry, per listing:
    id, title, company, location, applyUrl, postedDate, workModel, salary,
    qualifications, graduateTime, h1bSponsored, companySize.

    Two of those are better than anything the other sources publish.
    ``postedDate`` is epoch milliseconds -- a real timestamp with a time of day,
    where every markdown list gives a bare date at best. ``qualifications`` is
    the employer's requirements already extracted, so these rows do not need
    their application page read to fill the Skill Requirements column.

    ``applyUrl`` points back at jobright rather than the employer, so it is
    stored as the listing URL and enrich.resolve_portal follows it through to
    the real applicant tracking system.
    """
    jobs = payload
    if isinstance(payload, dict):
        jobs = (payload.get("props", {}).get("pageProps", {}).get("initialJobs")
                or payload.get("initialJobs"))
    if not isinstance(jobs, list):
        log.warning("intern-list %s: payload had no initialJobs list", feed)
        return []

    out: List[Posting] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        title = _pick(job, "title")
        company = _pick(job, "company")
        if not title or not company:
            continue

        posted, precision = None, "unknown"
        raw_date = job.get("postedDate")
        if isinstance(raw_date, (int, float)) and raw_date > 0:
            seconds = raw_date / 1000 if raw_date > 1_000_000_000_000 else raw_date
            try:
                posted = datetime.fromtimestamp(seconds, tz=timezone.utc)
                # Epoch milliseconds carry a real time of day, which outranks
                # both the commit-derived estimate and a bare calendar date.
                precision = "scraped"
            except (OverflowError, OSError, ValueError):
                posted = None

        job_id = _pick(job, "id", "jobId")
        apply_url = _pick(job, "applyUrl")
        qualifications = _clean_qualifications(_pick(job, "qualifications"))

        posting = Posting(
            # jobright's own 24-hex id, so a listing seen here and in the
            # jobright GitHub repos collapses to one row.
            job_id=job_id if JOBRIGHT_ID_RE.fullmatch(job_id) else stable_id(feed, apply_url or title + company),
            title=title,
            company=company,
            source=f"intern-list:{feed}",
            listing_url=apply_url,
            location=_pick(job, "location"),
            work_model=_pick(job, "workModel"),
            posted_at=posted,
            posted_precision=precision,
            notes=_notes_from(job),
        )
        if qualifications:
            posting.skills = [qualifications]
        out.append(posting)
    return out


def _embedded_json(html: str):
    """Pull the Next.js payload out of the server-rendered embed page."""
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

    # The embed serves server-rendered HTML with the listings in __NEXT_DATA__.
    payload = _embedded_json(resp.text)
    if payload is None and "json" in resp.headers.get("Content-Type", ""):
        try:
            payload = resp.json()
        except json.JSONDecodeError:
            payload = None

    if payload is None:
        log.warning("intern-list %s: no __NEXT_DATA__ in %s (%d bytes); the embed's "
                    "markup may have changed -- run tools/probe_internlist.py",
                    feed, url, len(resp.text))
        return []

    postings = postings_from_payload(payload, feed)
    if postings:
        log.info("intern-list %s: %d listings", feed, len(postings))
    else:
        log.warning("intern-list %s: payload parsed but held no listings", feed)
    return postings
