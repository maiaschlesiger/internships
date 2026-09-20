"""Collapse the same job posting appearing on more than one source.

Six lists cover overlapping ground, so one role routinely shows up several
times. Matching on the listing id alone is not enough: each list mints its own
id, and some link through their own redirector rather than the employer's
applicant tracking system.

Two passes, cheapest first:

1. the canonical application URL, which is the same page whoever links to it;
2. a fingerprint of company, normalised title and city, which catches the rest.

When duplicates merge, the surviving row keeps the earliest posting time and the
most precise one available, and prefers a real employer URL over a redirect.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Dict, List, Sequence

from .models import Posting
from .sources.ghlist import canonical_url, normalize_title

log = logging.getLogger(__name__)

# Most precise first -- decides which row's timestamp survives a merge.
PRECISION_RANK = {"scraped": 0, "commit": 1, "first_seen": 2, "exact": 1, "day": 3, "unknown": 4}

REDIRECTORS = ("jobright.ai", "dreamworkhq.com", "simplify.jobs", "intern-list.com")


def _city(location: str) -> str:
    """First component of a location, lowercased. 'New York, NY, USA' -> 'new york'."""
    return re.split(r"[,/|]", location or "", 1)[0].strip().lower()


def fingerprint(p: Posting) -> str:
    basis = f"{p.company.strip().lower()}|{normalize_title(p.title)}|{_city(p.location)}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:20]


def _is_real_portal(url: str) -> bool:
    return bool(url) and not any(host in url.lower() for host in REDIRECTORS)


def _merge(keep: Posting, other: Posting) -> Posting:
    """Fold ``other`` into ``keep``, preferring better data from either."""
    if other.posted_at and (
        not keep.posted_at
        or PRECISION_RANK.get(other.posted_precision, 9) < PRECISION_RANK.get(keep.posted_precision, 9)
        or (other.posted_precision == keep.posted_precision and other.posted_at < keep.posted_at)
    ):
        keep.posted_at = other.posted_at
        keep.posted_precision = other.posted_precision

    if not _is_real_portal(keep.portal_url) and _is_real_portal(other.portal_url):
        keep.portal_url = other.portal_url
    for field in ("location", "work_model", "company_url", "recruiter"):
        if not getattr(keep, field) and getattr(other, field):
            setattr(keep, field, getattr(other, field))
    if other.source not in keep.source:
        # " + ", not ", ": Source is a Notion select, and a select option may
        # not contain a comma. A job found in three lists is exactly the kind
        # worth surfacing, so this must not be what stops it being written.
        keep.source = f"{keep.source} + {other.source}"
    return keep


def collapse(postings: Sequence[Posting]) -> List[Posting]:
    """Return one Posting per distinct job."""
    by_key: Dict[str, Posting] = {}
    order: List[str] = []

    for p in postings:
        url_key = canonical_url(p.portal_url or p.listing_url)
        key = f"url:{url_key}" if _is_real_portal(url_key) else f"fp:{fingerprint(p)}"
        if key in by_key:
            _merge(by_key[key], p)
        else:
            by_key[key] = p
            order.append(key)

    # A row keyed by URL and another keyed by fingerprint can still be the same
    # job, so fold fingerprints together in a second pass.
    final: Dict[str, Posting] = {}
    final_order: List[str] = []
    for key in order:
        p = by_key[key]
        fp = fingerprint(p)
        if fp in final:
            _merge(final[fp], p)
        else:
            final[fp] = p
            final_order.append(fp)

    out = [final[fp] for fp in final_order]
    if len(out) != len(postings):
        log.info("dedupe: %d listings -> %d distinct jobs", len(postings), len(out))
    return out
