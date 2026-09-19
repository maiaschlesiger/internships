"""Recruiter lookup via the Apollo.io REST API.

Deliberately feature-flagged: with no APOLLO_API_KEY set this is a no-op and the
Recruiter column stays empty, which is the expected state if you connected the
Apollo *connector* rather than issuing an API key. A connector authenticates a
chat session; it cannot authenticate an unattended scheduled run.

Apollo bills credits per revealed email, so this only ever looks up companies
that survived the relevance filter, and caches within a run.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Dict, Optional, Sequence

import requests

from .models import Posting

log = logging.getLogger(__name__)

BASE = "https://api.apollo.io/api/v1"
RECRUITER_TITLES = [
    "university recruiter", "campus recruiter", "early career recruiter",
    "technical recruiter", "recruiter", "talent acquisition",
]


def _search(session: requests.Session, company: str) -> Optional[dict]:
    try:
        resp = session.post(
            f"{BASE}/mixed_people/search",
            json={
                "q_organization_name": company,
                "person_titles": RECRUITER_TITLES,
                "page": 1,
                "per_page": 5,
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        log.warning("apollo search failed for %s: %s", company, exc)
        return None

    if resp.status_code in (401, 403):
        raise PermissionError("Apollo rejected the API key (401/403)")
    if resp.status_code == 429:
        log.warning("apollo rate limited on %s", company)
        return None
    if not resp.ok:
        log.warning("apollo %s -> %s", company, resp.status_code)
        return None

    people = resp.json().get("people") or []
    return people[0] if people else None


def find_recruiters(postings: Sequence[Posting]) -> None:
    """Attach a recruiter email per posting, in place. No key -> no-op."""
    api_key = os.environ.get("APOLLO_API_KEY", "")
    if not api_key:
        log.info("APOLLO_API_KEY unset; skipping recruiter lookup")
        return

    session = requests.Session()
    session.headers.update({
        "x-api-key": api_key,
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
    })

    cache: Dict[str, str] = {}
    for p in postings:
        key = p.company.strip().lower()
        if not key:
            continue
        if key in cache:
            p.recruiter = cache[key]
            continue
        try:
            person = _search(session, p.company)
        except PermissionError as exc:
            log.error("%s -- aborting recruiter lookup for this run", exc)
            return

        email = ""
        if person:
            candidate = person.get("email") or ""
            # Apollo returns this placeholder when the address is behind a credit.
            if candidate and "email_not_unlocked" not in candidate:
                email = candidate
            elif person.get("name"):
                log.debug("%s: found %s but email locked", p.company, person["name"])
        cache[key] = email
        p.recruiter = email
        time.sleep(0.5)

    found = sum(1 for p in postings if p.recruiter)
    log.info("recruiter emails resolved for %d/%d listings", found, len(postings))
