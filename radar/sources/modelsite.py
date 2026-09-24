"""Read a job list by asking the model what is on the page.

Every other source here has a hand-written parser, which is right when the
markup is known and stable. It is the wrong tool for a site that cannot be
reached from where the parser would be written: the first intern-list parser
was written against a guessed response shape and had to be thrown away.

This reads the page on the runner -- which can reach it -- and has the model
return the listings as structured rows. The cost is one call per source per
run and some non-determinism; the gain is that a new source needs a URL in
config.yaml rather than a module, and that a redesign of the page does not
silently produce zero rows.

Nothing is invented: the prompt returns only what the page states, and a row
missing a title or company is dropped rather than guessed at.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlparse

import requests

from ..models import Posting

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-5"
# The page is reduced before it is sent: a job board is mostly chrome, and the
# listings are a small fraction of the bytes.
MAX_PAGE_CHARS = 60000

PROMPT = """Below is the content of an internship job board page.

Return every job listing it shows, as a JSON array. One object per listing:

{"title": "...", "company": "...", "location": "...", "url": "...",
 "posted": "...", "term": "..."}

Rules:
- Report only what the page states. Do not infer, complete or invent a field.
  Use "" for anything the page does not give.
- "url" must be the application or listing link exactly as the page gives it.
  Links appear in the page below as [[the-url]] immediately after their text.
  If it is relative, return it relative; it will be resolved afterwards.
- "posted" is whatever the page shows for when it was posted -- a date, or a
  relative age like "2d" or "3 hours ago". Copy it verbatim.
- "term" is the season the listing names, such as "Summer 2027", or "" if it
  names none. Do not guess from context.
- Ignore navigation, adverts, newsletter prompts and footer links. A row is a
  listing only if it names a company and a role.
- Return ONLY the JSON array, no prose. Return [] if the page shows no listings.

PAGE:
%s
"""

_TAG = re.compile(r"<(script|style|svg|noscript)\b.*?</\1>", re.S | re.I)
_ANGLE = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t]*\n[ \t\n]*")


def _readable(html: str) -> str:
    """Strip a page to the text and links a reader would see."""
    body = _TAG.sub(" ", html)
    # Keep hrefs: the model needs them for the url field. The delimiter must
    # not look like a tag, or the tag stripper below eats the URL with it.
    body = re.sub(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                  r"\2 [[\1]]", body, flags=re.S | re.I)
    body = _ANGLE.sub(" ", body)
    body = re.sub(r"&nbsp;?", " ", body)
    body = re.sub(r"[ \t]{2,}", " ", body)
    return _WS.sub("\n", body).strip()


def _page_text(url: str, session: requests.Session, timeout: int) -> str:
    """Fetch the page as text the model can read, embedded payload included."""
    resp = session.get(url, timeout=timeout)
    resp.raise_for_status()
    html = resp.text

    # A server-rendered app carries its listings in a JSON payload that survives
    # tag-stripping poorly, so hand that over directly when it is the richer of
    # the two.
    payload = ""
    for blob in re.findall(
            r'<script[^>]+(?:id="__NEXT_DATA__"|type="application/json")[^>]*>(.*?)</script>',
            html, re.S):
        if len(blob) > len(payload):
            payload = blob

    text = _readable(html)
    if len(payload) > len(text):
        return payload[:MAX_PAGE_CHARS]
    return text[:MAX_PAGE_CHARS]



# "2d ago", "3 hours ago", "Sep 23, 2026", "2026-09-23". Boards write the age
# of a posting every way there is, and the page is copied verbatim, so the
# parsing happens here rather than being asked of the model.
_REL = re.compile(r"(\d+)\s*(minute|min|hour|hr|h|day|d|week|w|month|mo)s?\b", re.I)
_UNIT_HOURS = {"minute": 1 / 60, "min": 1 / 60, "hour": 1, "hr": 1, "h": 1,
               "day": 24, "d": 24, "week": 168, "w": 168, "month": 720, "mo": 720}
_DATE_FORMATS = ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%m/%d/%Y")


def parse_posted(text: str, now: Optional[datetime] = None) -> Tuple[Optional[datetime], str]:
    """Read a board's posted field. Returns (when, precision)."""
    raw = (text or "").strip()
    if not raw:
        return None, ""
    now = now or datetime.now(timezone.utc)
    low = raw.lower()
    if low in ("today", "just posted", "new"):
        return now, "day"
    if low == "yesterday":
        return now - timedelta(days=1), "day"

    match = _REL.search(low)
    if match:
        hours = int(match.group(1)) * _UNIT_HOURS[match.group(2).lower()]
        # An age in hours is a real time of day; an age in days or longer is
        # only good to the day, and saying otherwise overstates it.
        precision = "relative" if hours < 24 else "day"
        return now - timedelta(hours=hours), precision

    cleaned = raw.replace("Posted", "").replace("posted", "").strip(" ·-–—")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).replace(tzinfo=timezone.utc), "day"
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(cleaned.replace("Z", "+00:00")), "day"
    except ValueError:
        return None, ""


def stable_id(company: str, title: str, url: str) -> str:
    """A durable id, so the same listing collapses across runs and sources."""
    basis = f"{company.strip().lower()}|{title.strip().lower()}|{url.strip()}"
    return "ms-" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def rows_to_postings(rows: Sequence[dict], page_url: str,
                     source_name: str) -> List[Posting]:
    """Turn model output into Postings, dropping anything underspecified."""
    out: List[Posting] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = (row.get("title") or "").strip()
        company = (row.get("company") or "").strip()
        if not title or not company:
            continue  # a listing without both is not a listing
        url = (row.get("url") or "").strip()
        if url and not url.lower().startswith(("http://", "https://")):
            url = urljoin(page_url, url)
        term = (row.get("term") or "").strip()
        when, precision = parse_posted(row.get("posted") or "")
        out.append(Posting(
            job_id=stable_id(company, title, url or page_url),
            title=title,
            company=company,
            listing_url=url or page_url,
            portal_url=url,
            location=(row.get("location") or "").strip(),
            source=source_name,
            term=term,
            term_confidence="stated" if term else "",
            posted_at=when,
            posted_precision=precision,
        ))
    return out


def fetch(url: str, api_key: str, source_name: str = "",
          timeout: int = 30, session: Optional[requests.Session] = None,
          client=None) -> List[Posting]:
    """Read one job-board page. Returns [] on any failure, never raises."""
    source_name = source_name or urlparse(url).netloc.replace("www.", "")
    if not api_key:
        log.info("%s: skipped (no ANTHROPIC_API_KEY)", source_name)
        return []

    session = session or requests.Session()
    session.headers.setdefault(
        "User-Agent", "Mozilla/5.0 (compatible; internship-radar/1.0)")
    try:
        text = _page_text(url, session, timeout)
    except requests.RequestException as exc:
        log.warning("%s: could not fetch (%s)", source_name, exc)
        return []
    if len(text) < 200:
        log.warning("%s: page had almost no readable content", source_name)
        return []

    if client is None:
        try:
            import anthropic
        except ImportError:
            log.warning("%s: anthropic SDK missing", source_name)
            return []
        client = anthropic.Anthropic(api_key=api_key)

    try:
        resp = client.messages.create(
            model=MODEL, max_tokens=8000,
            messages=[{"role": "user", "content": PROMPT % text}])
        answer = resp.content[0].text
    except Exception as exc:  # noqa: BLE001 - a bad source must not stop the run
        log.warning("%s: extraction call failed (%s)", source_name, exc)
        return []

    match = re.search(r"\[.*\]", answer, re.S)
    if not match:
        log.warning("%s: no JSON array in the response", source_name)
        return []
    try:
        rows = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        log.warning("%s: response did not parse (%s)", source_name, exc.msg)
        return []

    postings = rows_to_postings(rows, url, source_name)
    log.info("%s: %d listings", source_name, len(postings))
    return postings


def fetch_all(sources: Sequence[dict], api_key: str) -> List[Posting]:
    """Read every configured model-read source."""
    out: List[Posting] = []
    session = requests.Session()
    for entry in sources or []:
        url = entry.get("url") if isinstance(entry, dict) else entry
        if not url:
            continue
        name = entry.get("name", "") if isinstance(entry, dict) else ""
        out += fetch(url, api_key, source_name=name, session=session)
    return out
