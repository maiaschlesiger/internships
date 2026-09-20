"""Fill in the columns that need inference or an extra request.

``resolve_portal`` finds the employer's own application page; ``jobdesc`` then
fetches the posting text from it. When that succeeds, keywords and skill
requirements are drawn from the real description. When it fails -- a dead link,
a JS-only page with no embedded payload, a login wall -- the pass falls back to
inferring from the role title, which is weaker and marked as such in the output.
"""

from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Sequence
from html import unescape as html_unescape
from urllib.parse import urljoin, urlparse

import requests

from . import jobdesc
from .dedupe import _is_real_portal
from .models import Posting

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-5"

# Used when no API key is configured, so the columns are never simply blank.
FALLBACK_KEYWORDS = {
    "Product Management": ["product roadmap", "user stories", "A/B testing", "stakeholder management",
                           "product requirements", "metrics", "prioritization", "cross-functional"],
    "Product Design": ["user research", "wireframing", "prototyping", "Figma", "design systems",
                       "usability testing", "information architecture", "interaction design"],
    "Consulting / Strategy": ["market sizing", "competitive analysis", "financial modeling",
                              "client presentations", "data-driven recommendations", "stakeholder interviews"],
    "Technical / Adjacent": ["SQL", "Python", "data analysis", "dashboards", "experimentation",
                             "technical documentation", "API fundamentals"],
}
GENERIC_SKILLS = "See posting"

# Hosts that mean we landed on a real employer-side application page rather
# than back on an aggregator.
ATS_HINTS = ("greenhouse.io", "lever.co", "myworkdayjobs.com", "workday", "icims.com",
             "smartrecruiters.com", "ashbyhq.com", "jobvite.com", "taleo.net",
             "successfactors", "oraclecloud.com", "brassring.com", "eightfold.ai")


# Keys an aggregator's embedded JSON payload may use for the employer's own
# posting, best first. The "original" spellings are unambiguous; the "apply"
# ones are only trusted when they point off the aggregator, because jobright's
# own listing feed uses applyUrl for a link back to itself.
ORIGINAL_URL_KEYS = ("originaljobposturl", "originaljoburl", "originalpostingurl",
                     "originalposturl", "originalurl", "sourceurl", "sourcejoburl",
                     "externalapplyurl", "externalurl", "companyapplyurl",
                     "employerapplyurl", "joburl", "jobposturl", "postingurl",
                     "applyurl", "applylink", "applyurllink", "redirecturl",
                     "hiringurl")

# Link text on the button a human would click to leave the aggregator.
ORIGINAL_LINK_TEXT = ("original job post", "original posting", "original post",
                      "apply on company", "company website", "company site",
                      "employer site", "apply externally", "external apply",
                      "view original", "apply on the company")

# Hosts that are never the employer's posting, whatever the link text says.
_NOT_EMPLOYER = ("facebook.com", "twitter.com", "x.com", "linkedin.com/share",
                 "instagram.com", "youtube.com", "t.me", "wa.me", "mailto:",
                 "apple.com/app-store", "play.google.com", "chrome.google.com",
                 "w3.org", "schema.org", "googletagmanager.com", "cdn.")


def _clean_json_url(raw: str) -> str:
    """Undo the escaping a URL picks up inside an embedded JSON payload."""
    return (raw.replace("\\u0026", "&").replace("\\u002F", "/")
               .replace("\\/", "/").replace("\\&", "&").strip())


def _absolute(url: str, page_url: str) -> str:
    """Make a page-relative href absolute, so it can be judged like any other."""
    if page_url and url and not url.lower().startswith(("http://", "https://", "mailto:")):
        return urljoin(page_url, url)
    return url


def _usable_employer_url(url: str) -> bool:
    low = (url or "").lower()
    if not low.startswith(("http://", "https://")):
        return False
    if any(bad in low for bad in _NOT_EMPLOYER):
        return False
    return _is_real_portal(low)


def original_post_link(html: str, page_url: str = "") -> str:
    """Find the employer's own posting URL inside an aggregator's page.

    jobright serves its listing detail pages from Next.js, so the link behind
    the "Original Job Post" button is present in the delivered HTML even though
    the button itself is drawn client-side. Three passes, most trustworthy
    first: the embedded JSON payload, then an anchor whose visible text says it
    leaves the site, then any link to a known applicant tracking system.

    Returns "" when the page offers nothing better, so callers can keep the
    aggregator URL rather than substituting something wrong.
    """
    if not html:
        return ""

    # 1. Embedded JSON. Keys are compared with punctuation stripped so that
    #    applyUrl, apply_url and APPLY-URL all match the same entry.
    found: Dict[str, str] = {}
    for raw_key, raw_url in re.findall(r'"([A-Za-z0-9_\-]{3,40})"\s*:\s*"(https?:[^"]{10,600})"',
                                       html):
        key = re.sub(r"[^a-z]", "", raw_key.lower())
        if key in ORIGINAL_URL_KEYS:
            url = _clean_json_url(raw_url)
            if _usable_employer_url(url):
                found.setdefault(key, url)
    for key in ORIGINAL_URL_KEYS:
        if key in found:
            return found[key]

    # 2. An anchor that says it takes you to the employer.
    for href, text in re.findall(r"<a\b[^>]*?href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
                                 html, re.I | re.S):
        label = re.sub(r"<[^>]+>", " ", text)
        label = re.sub(r"\s+", " ", label).strip().lower()
        if any(hint in label for hint in ORIGINAL_LINK_TEXT):
            url = _absolute(_clean_json_url(html_unescape(href)), page_url)
            if _usable_employer_url(url):
                return url

    # 3. Any link into an applicant tracking system. A jobright page carries
    #    exactly one of these -- the posting it was scraped from.
    for href in re.findall(r'href=[\"\']([^\"\']+)[\"\']', html, re.I):
        url = _absolute(_clean_json_url(html_unescape(href)), page_url)
        low = url.lower()
        if any(h in low for h in ATS_HINTS) and _usable_employer_url(url):
            return url

    return ""


def follow_to_original(url: str, session: requests.Session, timeout: int = 15) -> str:
    """Resolve an aggregator link to the employer's posting, or return it unchanged.

    Covers both shapes: aggregators that HTTP-redirect (the link resolves by
    itself) and jobright, which does not -- it serves its own page and puts the
    employer's URL behind a button, so the page body has to be read.
    """
    if not url:
        return url
    try:
        resp = session.get(url, timeout=timeout, allow_redirects=True)
    except requests.RequestException as exc:
        log.debug("could not follow %s: %s", url, exc)
        return url

    final = resp.url or url
    if _is_real_portal(final) and final != url:
        return final  # the redirect chain landed on the employer already

    original = original_post_link(resp.text, final)
    if original:
        log.debug("followed %s to its original post at %s", url, urlparse(original).netloc)
        return original
    return final


def resolve_portal(postings: Sequence[Posting], timeout: int = 15,
                   workers: int = 8) -> None:
    """Follow aggregator links through to the employer's own application page.

    Two things keep this cheap. Listings that already carry a real employer URL
    are skipped outright -- the community lists link straight to the applicant
    tracking system, so re-fetching them only confirms what is already known,
    and they are the majority of any run. What is left is resolved
    concurrently; done one at a time with a generous timeout, a bootstrap over
    several hundred listings can approach the job's own time limit.

    When a chain cannot be followed the listing URL is kept, so a row always
    has somewhere to click.
    """
    pending = []
    for p in postings:
        if _is_real_portal(p.portal_url):
            continue  # already an employer URL from the source list
        if not p.listing_url:
            continue
        pending.append(p)

    skipped = len(postings) - len(pending)
    if skipped:
        log.info("portal: %d listings already carry an employer URL", skipped)
    if not pending:
        return

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; internship-radar/1.0)"})

    def resolve(p: Posting) -> None:
        resolved = follow_to_original(p.listing_url, session, timeout)
        if _is_real_portal(resolved):
            p.portal_url = resolved
            host = urlparse(resolved).netloc.lower()
            if not any(h in host for h in ATS_HINTS):
                log.debug("%s resolved to non-ATS host %s", p.job_id, host)
        else:
            # Nothing better than the aggregator link; keep it so the row still
            # has somewhere to click.
            p.portal_url = p.listing_url

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(resolve, p) for p in pending]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as exc:  # noqa: BLE001 - one bad link must not stop the rest
                log.debug("portal worker failed: %s", exc)

    resolved = sum(1 for p in pending if _is_real_portal(p.portal_url))
    log.info("portal: resolved %d/%d redirects to an employer URL", resolved, len(pending))


ENRICH_PROMPT = """For each internship listing below, identify what a strong \
resume should emphasise and what skills the role requires.

Where a DESCRIPTION is given, draw the skills and keywords from it -- quote the \
posting's own vocabulary, because that is what a resume screener matches against. \
Where only a title is given, infer conservatively and do not invent specifics.

Return ONLY a JSON array. Each element:
{"id": "<the id given>", "keywords": ["6-12 short resume keywords/phrases"], \
"skills": "one or two sentences naming the concrete required skills, \
qualifications and tools"}

Keywords should be terms a recruiter or resume screener would look for, e.g. \
"user research", "SQL", "roadmap prioritisation" -- not fluff like "hard working".

Listings:
%s
"""


def enrich_with_model(postings: Sequence[Posting], api_key: str,
                      descriptions: Optional[Dict[str, str]] = None) -> Dict[str, dict]:
    if not postings:
        return {}
    try:
        import anthropic
    except ImportError:
        log.warning("anthropic SDK missing; using fallback keywords")
        return {}

    descriptions = descriptions or {}
    blocks = []
    for p in postings:
        block = (f'- id={p.job_id} | title={p.title!r} | company={p.company!r} '
                 f'| category={p.category!r}')
        body = descriptions.get(p.job_id)
        if body:
            blocks.append(f"{block}\n  DESCRIPTION: {body}\n")
        else:
            blocks.append(block)
    lines = "\n".join(blocks)
    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=MODEL, max_tokens=4000,
            messages=[{"role": "user", "content": ENRICH_PROMPT % lines}],
        )
        text = resp.content[0].text
    except Exception as exc:  # noqa: BLE001
        log.warning("enrichment call failed (%s); using fallback keywords", exc)
        return {}

    match = re.search(r"\[.*\]", text, re.S)
    if not match:
        return {}
    try:
        rows = json.loads(match.group(0))
    except json.JSONDecodeError:
        log.warning("enrichment JSON did not parse; using fallback keywords")
        return {}
    return {r["id"]: r for r in rows if isinstance(r, dict) and "id" in r}


def apply_fallback(p: Posting, requirements: str = "") -> None:
    """Fill the row when the model pass did not run or did not answer.

    Keywords fall back to a per-category list. Skill requirements do NOT fall
    back to a sentence about the job title: where the application page had a
    requirements section, that text is the employer's own and goes in verbatim.
    Only a posting whose page could not be read gets the generic note.
    """
    p.resume_keywords = list(FALLBACK_KEYWORDS.get(p.category, FALLBACK_KEYWORDS["Product Management"]))
    # A source that already supplied structured requirements (intern-list ships
    # a qualifications field) beats anything scraped or generic -- never
    # overwrite it with a placeholder.
    existing = p.skills[0] if p.skills else ""
    if existing and existing != GENERIC_SKILLS:
        return
    p.skills = [requirements] if requirements else [GENERIC_SKILLS]


def enrich(postings: Sequence[Posting], cfg: Optional[dict] = None,
           pages: Optional[Dict[str, "jobdesc.PageData"]] = None) -> None:
    """Fill keywords and skills in place, model-first with a keyword fallback.

    ``pages`` comes from jobdesc.fetch_all, which main runs once so the same
    request serves both the posting timestamp and the description.
    """
    cfg = cfg or {}
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    pages = pages or {}
    descriptions = {jid: data.text for jid, data in pages.items() if data.text}

    results = enrich_with_model(postings, api_key, descriptions) if api_key else {}

    for p in postings:
        row = results.get(p.job_id)
        if row and row.get("keywords"):
            p.resume_keywords = [str(k)[:100] for k in row["keywords"]][:12]
            skills = row.get("skills")
            p.skills = [str(skills)] if skills else [GENERIC_SKILLS]
        else:
            data = pages.get(p.job_id)
            apply_fallback(p, requirements=data.requirements if data else "")
