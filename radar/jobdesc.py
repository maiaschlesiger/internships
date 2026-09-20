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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

import requests

from .dedupe import _is_real_portal
from .models import Posting

log = logging.getLogger(__name__)

# Most job boards publish a schema.org JobPosting block. datePosted is the
# employer's own timestamp, which beats any date we can infer from a list.
# Be aware it is frequently a bare calendar date with no time of day -- see
# extract_posted_at, which reports which of the two it got.
DATE_KEYS = ("datePosted", "postedDate", "posted_at", "publishedAt",
             "first_published", "createdAt", "postedOn", "publishTime")
DATE_VALUE_RE = re.compile(
    r'"(?:' + "|".join(DATE_KEYS) + r')"\s*:\s*"([^"]{4,40})"')
DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

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


def extract_posted_at(html: str) -> Tuple[Optional[datetime], str]:
    """Find the employer's own posting timestamp.

    Returns ``(when, precision)`` where precision is "scraped" if the page gave
    a real time of day and "day" if it only gave a calendar date. Most postings
    publish only a date -- the hour a job went live is simply not something most
    applicant tracking systems disclose -- so "day" is the common answer and the
    caller should keep a more precise estimate if it already has one.
    """
    for match in DATE_VALUE_RE.finditer(html):
        raw = match.group(1).strip()
        date_only = bool(DATE_ONLY_RE.match(raw))
        try:
            when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            if raw.isdigit():
                value = int(raw)
                if value > 1_000_000_000_000:
                    value //= 1000
                try:
                    when = datetime.fromtimestamp(value, tz=timezone.utc)
                except (OverflowError, OSError, ValueError):
                    continue
                date_only = False
            else:
                continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        # Guard against a parse that lands absurdly far from now.
        now = datetime.now(timezone.utc)
        if not (now.replace(year=now.year - 3) < when < now.replace(year=now.year + 1)):
            continue
        return when, ("day" if date_only else "scraped")
    return None, "unknown"


# Headings that introduce what a candidate needs. Ordered by how specific they
# usually are, so the most useful section wins when a posting has several.
REQUIREMENT_HEADINGS = (
    "basic qualifications", "minimum qualifications", "required qualifications",
    "what you'll need", "what you will need", "what we're looking for",
    "what we are looking for", "requirements", "qualifications",
    "required skills", "desired skills", "skills", "who you are",
    "about you", "you have", "preferred qualifications", "nice to have",
)
# Headings that end the section -- everything past these is boilerplate.
STOP_HEADINGS = (
    "benefits", "what we offer", "perks", "compensation", "salary", "pay range",
    "about us", "about the company", "equal opportunity", "eeo", "diversity",
    "accommodation", "how to apply", "application process", "privacy",
    "disclaimer", "next steps", "our values",
)
BULLET_RE = re.compile(r"^\s*[-*\u2022\u25cf\u25aa\u2023\u2043\d]+[.)]?\s+")


def _is_heading(line: str, names: tuple) -> bool:
    probe = line.strip().strip(":").lower()
    if len(probe) > 60:
        return False
    return any(probe == n or probe.startswith(n) for n in names)


def extract_requirements(text: str, max_chars: int = 900) -> str:
    """Pull the requirements/qualifications section out of a job description.

    This is what fills the Skill Requirements column when no model is available
    to summarise the posting. It returns the employer's own words rather than an
    inference from the job title, which is the whole point.

    Returns "" when the posting has no recognisable requirements section.
    """
    if not text:
        return ""
    lines = [ln.strip() for ln in text.splitlines()]

    start = None
    for i, line in enumerate(lines):
        if line and _is_heading(line, REQUIREMENT_HEADINGS):
            start = i + 1
            break
    if start is None:
        return ""

    collected = []
    for line in lines[start:]:
        if not line:
            continue
        if _is_heading(line, STOP_HEADINGS):
            break
        # A new requirements-style heading continues the same idea; keep going.
        if _is_heading(line, REQUIREMENT_HEADINGS):
            continue
        item = BULLET_RE.sub("", line).strip(" ;")
        if len(item) < 8:
            continue
        collected.append(item)
        if sum(len(c) + 2 for c in collected) > max_chars:
            break

    if not collected:
        return ""
    out = "; ".join(collected)
    return out[:max_chars].rstrip(" ;") + ("..." if len(out) > max_chars else "")


EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# A local part naming a hiring function -- the ones actually worth writing to.
RECRUITING_HINTS = ("recruit", "talent", "campus", "university", "intern",
                    "career", "jobs", "hiring", "staffing", "earlycareer",
                    "early-career", "people", "hr@", "hr.")
# Never useful: automated senders, legal and compliance inboxes.
JUNK_LOCAL = ("noreply", "no-reply", "donotreply", "do-not-reply", "mailer",
              "postmaster", "webmaster", "abuse", "privacy", "legal",
              "compliance", "security", "dmca", "unsubscribe", "example",
              "sample", "test@", "you@", "name@", "email@", "sentry")
# The applicant tracking vendor's own addresses, not the employer's.
VENDOR_DOMAINS = ("greenhouse.io", "lever.co", "myworkdayjobs.com", "workday.com",
                  "icims.com", "smartrecruiters.com", "ashbyhq.com", "jobvite.com",
                  "taleo.net", "successfactors", "oraclecloud.com", "brassring.com",
                  "eightfold.ai", "sentry.io", "wixpress.com", "schema.org",
                  "w3.org", "example.com", "sentry-next.wixpress.com", "jobright.ai",
                  "simplify.jobs", "dreamworkhq.com", "godaddy.com")


def extract_contact_emails(html: str) -> str:
    """Return the most useful contact address printed in a posting, or "".

    Employers routinely publish a hiring address in the posting itself -- a
    university recruiting inbox, an accommodations contact, sometimes a named
    recruiter. It is free, already on a page being fetched, and more reliable
    than anything inferred, so it is worth preferring over a paid lookup.

    Addresses belonging to the applicant tracking vendor, and automated or
    legal inboxes, are discarded. A hiring-function local part wins over a
    generic one; nothing is ever guessed or constructed.
    """
    if not html:
        return ""
    seen, best_generic = set(), ""
    for match in EMAIL_RE.finditer(html):
        email = match.group(0).strip(".,;:)").lower()
        if email in seen:
            continue
        seen.add(email)
        if any(v in email for v in VENDOR_DOMAINS):
            continue
        if any(j in email for j in JUNK_LOCAL):
            continue
        # Image and asset filenames occasionally match the pattern.
        if email.rsplit(".", 1)[-1] in ("png", "jpg", "jpeg", "gif", "svg", "webp", "css", "js"):
            continue
        if any(h in email for h in RECRUITING_HINTS):
            return email          # a hiring inbox: stop looking
        if not best_generic:
            best_generic = email  # keep the first plausible one as a fallback
    return best_generic


# Facts worth knowing before deciding whether to spend an evening applying.
# Each returns a short label; none of them guess.
PAY_RANGE_RE = re.compile(
    r"\$\s?\d{1,3}(?:,\d{3})*(?:\.\d{2})?\s*(?:-|\u2013|\u2014|to)\s*\$?\s?\d{1,3}(?:,\d{3})*(?:\.\d{2})?")
PAY_RATE_RE = re.compile(
    r"\$\s?\d{1,3}(?:,\d{3})*(?:\.\d{2})?\s*(?:/|\s*per\s+)(?:hour|hr|month|mo|year|yr|annum)", re.I)
DEADLINE_RE = re.compile(
    r"(?:appl(?:y|ications?)\s+(?:by|close[sd]?|deadline)|deadline(?:\s+is)?|"
    r"closes?\s+on)[^.\n]{0,50}?"
    r"(\d{1,2}/\d{1,2}/\d{2,4}|"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}(?:,?\s*20\d\d)?)", re.I)
DURATION_RE = re.compile(r"\b(\d{1,2})[\s-]*(?:week|month)s?\b(?!\s*(?:of|notice))", re.I)
GPA_RE = re.compile(r"(?:minimum\s+)?(?:GPA|grade point average)[^.\n]{0,24}?(\d\.\d{1,2})|"
                    r"(\d\.\d{1,2})\s*(?:GPA|or higher GPA)", re.I)

NOTE_PHRASES = (
    ("No visa sponsorship", ("not able to sponsor", "unable to sponsor",
                             "no sponsorship", "does not offer sponsorship",
                             "will not sponsor", "not provide sponsorship",
                             "not offer visa")),
    ("US work authorization required", ("authorized to work in the united states",
                                        "must be authorized to work",
                                        "legally authorized to work")),
    ("US citizenship required", ("must be a u.s. citizen", "u.s. citizenship is required",
                                 "us citizenship required", "united states citizen")),
    ("Security clearance", ("security clearance", "able to obtain a clearance")),
    ("Relocation or housing support", ("relocation assistance", "housing stipend",
                                       "corporate housing", "relocation package",
                                       "housing is provided")),
    ("Return offer possible", ("return offer", "full-time offer upon",
                               "conversion to full-time")),
    ("Remote", ("fully remote", "100% remote", "remote-first")),
)


def extract_notes(text: str, extra: Optional[Sequence[str]] = None,
                  max_chars: int = 400) -> str:
    """Pull out the details worth knowing before applying.

    Pay, deadline, programme length, GPA cut-off, and flags like sponsorship or
    clearance. Everything here is quoted or labelled from the posting; nothing
    is inferred. ``extra`` carries notes the source list already knew, such as
    the sponsorship glyphs the community lists use.
    """
    notes: List[str] = list(extra or [])
    if text:
        low = text.lower()

        pay = PAY_RANGE_RE.search(text) or PAY_RATE_RE.search(text)
        if pay:
            notes.append("Pay: " + re.sub(r"\s+", " ", pay.group(0)).strip())

        deadline = DEADLINE_RE.search(text)
        if deadline:
            notes.append("Deadline: " + deadline.group(1).strip())

        duration = DURATION_RE.search(text)
        if duration:
            unit = "week" if "week" in duration.group(0).lower() else "month"
            notes.append(f"{duration.group(1)}-{unit} programme")

        gpa = GPA_RE.search(text)
        if gpa:
            notes.append("GPA " + (gpa.group(1) or gpa.group(2)))

        for label, phrases in NOTE_PHRASES:
            if any(phrase in low for phrase in phrases):
                notes.append(label)

    # Preserve order, drop repeats.
    seen, out = set(), []
    for note in notes:
        key = note.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(note)
    joined = " · ".join(out)
    return joined[:max_chars].rstrip(" ·")


@dataclass
class PageData:
    """What one fetch of an application page yielded."""
    text: str = ""
    posted_at: Optional[datetime] = None
    posted_precision: str = "unknown"
    requirements: str = ""
    contact_email: str = ""
    notes: str = ""


def fetch_one(url: str, session: requests.Session, timeout: int = 20,
              max_chars: int = 6000) -> PageData:
    """Fetch one application page: description text plus the posted timestamp."""
    if not url:
        return PageData()

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
                    when, precision = extract_posted_at(resp.text)
                    plain = html_to_text(body)[:max_chars]
                    return PageData(plain, when, precision, extract_requirements(plain),
                                    extract_contact_emails(resp.text),
                                    extract_notes(plain))
        except (requests.RequestException, json.JSONDecodeError, AttributeError):
            pass  # fall through to the HTML path

    try:
        resp = session.get(url, timeout=timeout)
        if not resp.ok:
            return PageData()
        html = resp.text
    except requests.RequestException as exc:
        log.debug("page fetch failed for %s: %s", url, exc)
        return PageData()

    # An aggregator page describes the aggregator: its own boilerplate, its own
    # "posted 2 days ago". Step through to the employer's posting and read that
    # instead, which is what the requirements and the posted time should come
    # from. One extra request, and only for rows that need it.
    if not _is_real_portal(resp.url or url):
        from .enrich import original_post_link  # imported late: enrich imports this module

        original = original_post_link(html, resp.url or url)
        if original:
            try:
                deep = session.get(original, timeout=timeout)
                if deep.ok and len(deep.text) > 500:
                    log.debug("read %s from its original post at %s", url, original)
                    html = deep.text
            except requests.RequestException as exc:
                log.debug("original post fetch failed for %s: %s", original, exc)

    when, precision = extract_posted_at(html)
    text = html_to_text(html)
    # A near-empty body means the page is JS-rendered; try its JSON payload.
    if len(text) < 400:
        embedded = _from_embedded_json(html)
        if len(embedded) > len(text):
            text = embedded

    if len(text) < 200:
        text = ""
    text = text[:max_chars]
    return PageData(text, when, precision, extract_requirements(text),
                    extract_contact_emails(html), extract_notes(text))


def fetch_all(postings: Sequence[Posting], max_chars: int = 6000,
              workers: int = 6, timeout: int = 20) -> Dict[str, PageData]:
    """Fetch application pages concurrently. Returns job_id -> PageData."""
    if not postings:
        return {}

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; internship-radar/1.0)",
        "Accept-Language": "en-US,en;q=0.9",
    })

    out: Dict[str, PageData] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(fetch_one, p.portal_url or p.listing_url, session,
                        timeout, max_chars): p.job_id
            for p in postings
        }
        for fut in as_completed(futures):
            job_id = futures[fut]
            try:
                data = fut.result()
            except Exception as exc:  # noqa: BLE001 - one bad page must not stop the rest
                log.debug("page worker failed for %s: %s", job_id, exc)
                continue
            if data.text or data.posted_at:
                out[job_id] = data

    with_email = sum(1 for d in out.values() if d.contact_email)
    with_reqs = sum(1 for d in out.values() if d.requirements)
    with_text = sum(1 for d in out.values() if d.text)
    with_date = sum(1 for d in out.values() if d.posted_at)
    exact = sum(1 for d in out.values() if d.posted_precision == "scraped")
    log.info("fetched %d/%d pages: %d with description, %d with a requirements "
             "section, %d with a contact email, %d with a posted date "
             "(%d of those with a time of day)",
             len(out), len(postings), with_text, with_reqs, with_email, with_date, exact)
    return out
