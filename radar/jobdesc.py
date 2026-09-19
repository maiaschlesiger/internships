"""Fetch the real job description behind a listing.

Resume keywords and skill requirements inferred from a job *title* are weak.
The actual description is one request away once the application URL has been
resolved, so this module fetches it and hands the text to the enrichment pass.

Applicant tracking systems differ in how much they render server-side:

* Greenhouse, Lever, SmartRecruiters -- usable static HTML.
* Workday -- a JavaScript shell, but the same URL returns clean JSON when asked
  for it, which is far more reliable than scraping the rendered page.
* Ashby and friends -- JS-rendered, but embed the posting as JSON in the page.

Every path is best-effort. A description that cannot be fetched is not an error:
the caller falls back to title-only inference.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from typing import Dict, Optional, Sequence

import requests

from .models import Posting

log = logging.getLogger(__name__)

SKIP_TAGS = {"script", "style", "noscript", "svg", "head", "nav", "footer", "header"}
WHITESPACE_RE = re.compile(r"[ \t\r\f\v]+")
BLANKLINES_RE = re.compile(r"\n{3,}")


class _TextExtractor(HTMLParser):
    """Collect visible text, skipping script/style/chrome."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self._skip_depth += 1
        elif tag in ("p", "li", "br", "div", "h1", "h2", "h3", "h4", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self.parts.append(text + " ")

    def text(self) -> str:
        joined = "".join(self.parts)
        joined = WHITESPACE_RE.sub(" ", joined)
        return BLANKLINES_RE.sub("\n\n", joined).strip()


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception as exc:  # noqa: BLE001 - malformed markup is common
        log.debug("text extraction failed: %s", exc)
        return ""
    return parser.text()


def _from_embedded_json(html: str) -> str:
    """Pull a description out of a JS-rendered page's embedded JSON payload.

    Ashby, SmartRecruiters and several others ship the posting body inside a
    __NEXT_DATA__ or similar script block even though the DOM is empty.
    """
    for match in re.finditer(r'"(?:descriptionHtml|jobDescription|description)"\s*:\s*"', html):
        start = match.end()
        # Walk the JSON string manually so escaped quotes do not end it early.
        out, i, n = [], start, len(html)
        while i < n:
            ch = html[i]
            if ch == "\\" and i + 1 < n:
                out.append(html[i:i + 2])
                i += 2
                continue
            if ch == '"':
                break
            out.append(ch)
            i += 1
        try:
            decoded = json.loads('"' + "".join(out) + '"')
        except json.JSONDecodeError:
            continue
        text = html_to_text(decoded) if "<" in decoded else decoded.strip()
        # Low bar here on purpose: fetch_one applies the real length floor.
        # Duplicating a high threshold in both places silently discarded short
        # but perfectly good descriptions.
        if len(text) > 100:
            return text
    return ""


def fetch_one(url: str, session: requests.Session, timeout: int = 20,
              max_chars: int = 6000) -> str:
    """Return the description text for one application URL, or "" on failure."""
    if not url:
        return ""

    # Workday renders client-side but serves JSON from the same URL.
    if "myworkdayjobs.com" in url or ".wd" in url:
        try:
            resp = session.get(url, timeout=timeout,
                               headers={"Accept": "application/json"})
            if resp.ok and "json" in resp.headers.get("Content-Type", ""):
                payload = resp.json()
                info = payload.get("jobPostingInfo") or {}
                body = info.get("jobDescription") or ""
                if body:
                    return html_to_text(body)[:max_chars]
        except (requests.RequestException, json.JSONDecodeError, AttributeError):
            pass  # fall through to the HTML path

    try:
        resp = session.get(url, timeout=timeout)
        if not resp.ok:
            return ""
        html = resp.text
    except requests.RequestException as exc:
        log.debug("description fetch failed for %s: %s", url, exc)
        return ""

    text = html_to_text(html)
    # A near-empty body means the page is JS-rendered; try its JSON payload.
    if len(text) < 400:
        embedded = _from_embedded_json(html)
        if len(embedded) > len(text):
            text = embedded

    if len(text) < 200:
        return ""
    return text[:max_chars]


def fetch_all(postings: Sequence[Posting], max_chars: int = 6000,
              workers: int = 6, timeout: int = 20) -> Dict[str, str]:
    """Fetch descriptions concurrently. Returns job_id -> text (missing on failure)."""
    if not postings:
        return {}

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; internship-radar/1.0)",
        "Accept-Language": "en-US,en;q=0.9",
    })

    out: Dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(fetch_one, p.portal_url or p.listing_url, session,
                        timeout, max_chars): p.job_id
            for p in postings
        }
        for fut in as_completed(futures):
            job_id = futures[fut]
            try:
                text = fut.result()
            except Exception as exc:  # noqa: BLE001 - one bad page must not stop the rest
                log.debug("description worker failed for %s: %s", job_id, exc)
                continue
            if text:
                out[job_id] = text

    log.info("fetched descriptions for %d/%d listings", len(out), len(postings))
    return out
