"""Parser for the community-maintained GitHub internship lists.

Three projects, three table schemas, two markup languages:

* vanshb03/Summer2027-Internships   markdown pipe table on ``main``
* SimplifyJobs/Summer2027-Internships   HTML <tr>/<td> table on ``master``
* dreamworkhq/Tech-Internships-2027   markdown pipe table on ``main``

All three share conventions worth honouring rather than parsing past:

* ``↳`` in the company cell means "same company as the row above".
* Emoji in the role cell encode eligibility, per each repo's own legend:
  🔒 the application is closed, 🎓 an advanced degree (Master's/PhD/MBA) is
  required, 🛂 no visa sponsorship, 🇺🇸 US citizenship required. The first two
  are disqualifying for an undergraduate and are dropped here rather than
  being left for the relevance filter to puzzle over.
* The apply cell links straight to the employer's real ATS, which is better
  than a redirect -- it is used directly as the application portal.

Dates are only ever a day ("Aug 21") or a whole number of days ("3d"), which is
why ``jobdesc.extract_posted_at`` exists.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import requests

from ..models import Posting

log = logging.getLogger(__name__)

RAW = "https://raw.githubusercontent.com/{repo}/{branch}/README.md"

CLOSED = "\U0001F512"          # 🔒
ADVANCED_DEGREE = "\U0001F393"  # 🎓
NO_SPONSORSHIP = "\U0001F6C2"   # 🛂
US_CITIZEN = "\U0001F1FA\U0001F1F8"  # 🇺🇸
CONTINUATION = "↳"        # ↳

# Query parameters these lists append for their own attribution. Stripping them
# is what lets the same posting from two different lists collapse into one row.
TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_content",
                   "utm_term", "ref", "source", "gh_src"}

REPOS: Dict[str, dict] = {
    "vanshb03/Summer2027-Internships": {
        "branch": "main", "format": "markdown",
        "cols": {"company": 0, "title": 1, "location": 2, "apply": 3, "age": 4},
    },
    "SimplifyJobs/Summer2027-Internships": {
        "branch": "master", "format": "html",
        "cols": {"company": 0, "title": 1, "location": 2, "apply": 3, "age": 4},
    },
    "dreamworkhq/Tech-Internships-2027": {
        "branch": "main", "format": "markdown",
        "cols": {"company": 0, "title": 1, "location": 2, "apply": 1, "age": 4},
        # Links go through dreamworkhq.com/job/<uuid>, not the employer's ATS,
        # so the URL identifies the listing on *this* board rather than the job.
        # Identity falls back to company + normalised title.
        "redirector": True,
    },
}

MD_ROW_RE = re.compile(r"^\|(.*)\|\s*$")
HTML_ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.S | re.I)
HTML_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)
TAG_RE = re.compile(r"<[^>]+>")
REL_DAYS_RE = re.compile(r"^\s*(\d+)\s*(d|day|days|mo|month|months)\s*$", re.I)
MONTH_DAY_RE = re.compile(r"^\s*([A-Z][a-z]{2})\s+(\d{1,2})\s*$")
MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def canonical_url(url: str) -> str:
    """Strip list-specific tracking so identical postings compare equal."""
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in TRACKING_PARAMS]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(),
                       parts.path.rstrip("/"), urlencode(query), ""))


def _md_cells(line: str) -> List[str]:
    m = MD_ROW_RE.match(line.strip())
    if not m:
        return []
    return [c.strip() for c in m.group(1).split("|")]


def _plain(cell: str) -> str:
    """Cell text with markup, images and convention emoji removed."""
    text = TAG_RE.sub(" ", cell)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)     # markdown images
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)   # markdown links
    for glyph in (CLOSED, ADVANCED_DEGREE, NO_SPONSORSHIP, US_CITIZEN, "\U0001F525"):
        text = text.replace(glyph, " ")
    text = text.replace("**", "").replace("*", "")
    return re.sub(r"\s+", " ", text).strip()


def _first_href(cell: str) -> str:
    m = HREF_RE.search(cell)
    if m:
        return m.group(1).strip()
    m = re.search(r"\]\(([^)\s]+)", cell)  # markdown link target
    return m.group(1).strip() if m else ""


def parse_age(text: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """Interpret the list's 'Date Posted' / 'Added' cell.

    Handles "0d", "3d", "2mo" and "Aug 21". Both forms are day-granularity at
    best; the returned value is midnight UTC on the implied day.
    """
    now = now or datetime.now(timezone.utc)
    text = _plain(text)
    if not text:
        return None

    m = REL_DAYS_RE.match(text)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        delta = timedelta(days=n * 30) if unit.startswith("mo") else timedelta(days=n)
        return (now - delta).replace(hour=0, minute=0, second=0, microsecond=0)

    m = MONTH_DAY_RE.match(text)
    if m and m.group(1) in MONTHS:
        month, day = MONTHS[m.group(1)], int(m.group(2))
        # No year in the cell: choose the most recent occurrence not in the future.
        year = now.year
        try:
            when = datetime(year, month, day, tzinfo=timezone.utc)
        except ValueError:
            return None
        if when > now + timedelta(days=1):
            when = when.replace(year=year - 1)
        return when
    return None


def _row_to_posting(cells: List[str], cfg: dict, repo: str,
                    last_company: str, now: datetime):
    """Build a Posting from one row. Returns (posting_or_None, company_to_carry)."""
    cols = cfg["cols"]
    if len(cells) <= max(cols.values()):
        return None, last_company

    raw_company = cells[cols["company"]]
    raw_title = cells[cols["title"]]
    title = _plain(raw_title)
    if not title or title.lower() in ("role", "job title", "position") or set(title) <= {"-", " "}:
        return None, last_company

    company_text = _plain(raw_company)
    if company_text == CONTINUATION or company_text in ("", "↳"):
        company = last_company
    else:
        company = company_text
        last_company = company
    if not company:
        return None, last_company

    # Eligibility glyphs live in the raw title cell, before _plain strips them.
    if CLOSED in raw_title or CLOSED in raw_company:
        return None, last_company
    if ADVANCED_DEGREE in raw_title:
        return None, last_company

    # These glyphs carry real eligibility information. They were being stripped
    # by _plain and thrown away; keep them as notes instead.
    flags = []
    if NO_SPONSORSHIP in raw_title or NO_SPONSORSHIP in raw_company:
        flags.append("No visa sponsorship")
    if US_CITIZEN in raw_title or US_CITIZEN in raw_company:
        flags.append("US citizenship required")

    apply_url = canonical_url(_first_href(cells[cols["apply"]]))
    identity_url = "" if cfg.get("redirector") else apply_url
    posted = parse_age(cells[cols["age"]], now)

    return Posting(
        job_id=content_id(company, title, identity_url),
        title=title,
        company=company,
        source=repo,
        listing_url=apply_url,
        portal_url=apply_url,  # these lists link straight to the real ATS
        location=_plain(cells[cols["location"]]),
        posted_at=posted,
        posted_precision="day" if posted else "unknown",
        notes=" \u00b7 ".join(flags),
    ), last_company


def content_id(company: str, title: str, apply_url: str) -> str:
    """Stable id keyed on the posting itself, not on which list carried it.

    Two lists advertising the same job must produce the same id, or the row
    lands twice. The canonical apply URL is the strongest signal; where it is
    missing, fall back to company plus title.
    """
    basis = apply_url or f"{company.strip().lower()}|{normalize_title(title)}"
    return "gh:" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:20]


def normalize_title(title: str) -> str:
    """Fold the cosmetic differences between two lists' wording of one role."""
    t = title.lower()
    t = re.sub(r"\b(summer|fall|winter|spring)\s*20\d\d\b", " ", t)
    t = re.sub(r"\b20\d\d\b", " ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def parse(text: str, cfg: dict, repo: str, now: Optional[datetime] = None) -> List[Posting]:
    now = now or datetime.now(timezone.utc)
    out: List[Posting] = []
    last_company = ""

    if cfg["format"] == "html":
        for match in HTML_ROW_RE.finditer(text):
            cells = HTML_CELL_RE.findall(match.group(1))
            posting, last_company = _row_to_posting(cells, cfg, repo, last_company, now)
            if posting:
                out.append(posting)
    else:
        for line in text.splitlines():
            if not line.lstrip().startswith("|"):
                continue
            cells = _md_cells(line)
            if not cells or set("".join(cells)) <= {"-", " ", ":"}:
                continue
            posting, last_company = _row_to_posting(cells, cfg, repo, last_company, now)
            if posting:
                out.append(posting)
    return out


def fetch(repo: str, session: Optional[requests.Session] = None) -> List[Posting]:
    cfg = REPOS.get(repo)
    if not cfg:
        log.warning("unknown github list %r", repo)
        return []
    session = session or requests.Session()
    url = RAW.format(repo=repo, branch=cfg["branch"])
    try:
        resp = session.get(url, timeout=45)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("%s unreachable (%s)", repo, exc)
        return []
    postings = parse(resp.text, cfg, repo)
    log.info("%s: parsed %d open listings", repo, len(postings))
    return postings
