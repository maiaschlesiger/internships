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
from typing import Dict, List, Sequence
from urllib.parse import urlparse

import requests

from . import jobdesc
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
GENERIC_SKILLS = "Could not read the application page - open the listing."

# Hosts that mean we landed on a real employer-side application page rather
# than back on an aggregator.
ATS_HINTS = ("greenhouse.io", "lever.co", "myworkdayjobs.com", "workday", "icims.com",
             "smartrecruiters.com", "ashbyhq.com", "jobvite.com", "taleo.net",
             "successfactors", "oraclecloud.com", "brassring.com", "eightfold.ai")


def resolve_portal(postings: Sequence[Posting], timeout: int = 20) -> None:
    """Follow each listing URL to the employer's own application page.

    Aggregator links redirect to the real applicant tracking system. When the
    chain cannot be followed (network blocked, JS-only interstitial, dead link)
    the listing URL is kept so the row always has somewhere to click.
    """
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; internship-radar/1.0)"})

    for p in postings:
        if not p.listing_url:
            continue
        try:
            resp = session.get(p.listing_url, timeout=timeout, allow_redirects=True)
            final = resp.url
        except requests.RequestException as exc:
            log.debug("portal resolve failed for %s: %s", p.job_id, exc)
            p.portal_url = p.listing_url
            continue

        host = urlparse(final).netloc.lower()
        if final != p.listing_url and not any(a in host for a in ("jobright",)):
            p.portal_url = final
            if not any(h in host for h in ATS_HINTS):
                log.debug("%s resolved to non-ATS host %s", p.job_id, host)
        else:
            p.portal_url = p.listing_url


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
