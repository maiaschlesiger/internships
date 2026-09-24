"""Read hiring.cafe's listings out of the payload its page already carries.

hiring.cafe aggregates company career pages and applicant tracking systems
directly rather than reposting other boards, so it reaches employers the
community lists never do. Its pages are Next.js and ship the whole result set
in a ``__NEXT_DATA__`` island -- no rendering, no model, no guessing.

It answers requests from a datacenter with 403, so ``fetch`` will usually fail
from CI and succeed from a laptop. ``parse`` works on a page saved from a
browser either way, which is what ``--ingest`` uses: the listings are read from
a page a person opened themselves.

Per listing this is the best-described source here. ``apply_url`` points at the
employer's own ATS rather than at hiring.cafe, ``estimated_publish_date`` is a
real timestamp, and ``requirements_summary`` fills Skill Requirements without
the application page being fetched at all.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import List, Optional

import requests

from ..models import Posting

log = logging.getLogger(__name__)

PAYLOAD = re.compile(
    r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def _publish_date(v5: dict) -> tuple:
    """Prefer the epoch millis; fall back to the ISO string."""
    millis = v5.get("estimated_publish_date_millis")
    if isinstance(millis, (int, float)) and millis > 0:
        return datetime.fromtimestamp(millis / 1000, tz=timezone.utc), "exact"
    stamp = v5.get("estimated_publish_date")
    if stamp:
        try:
            return datetime.fromisoformat(stamp.replace("Z", "+00:00")), "exact"
        except ValueError:
            pass
    return None, ""


def _notes(v5: dict) -> str:
    """Facts the row has nowhere else to put, taken only where stated."""
    bits = []
    lo = v5.get("hourly_min_compensation")
    hi = v5.get("hourly_max_compensation")
    if v5.get("is_compensation_transparent") and (lo or hi):
        cur = v5.get("listed_compensation_currency") or "USD"
        if lo and hi and lo != hi:
            bits.append(f"Pay: {lo}-{hi} {cur}/hr")
        else:
            bits.append(f"Pay: {lo or hi} {cur}/hr")
    if v5.get("seniority_level"):
        bits.append(f"Level: {v5['seniority_level']}")
    fields = v5.get("bachelors_degree_fields_of_study")
    if fields:
        bits.append("Degree: " + ", ".join(fields[:4]))
    if v5.get("workplace_type"):
        bits.append(v5["workplace_type"])
    return " | ".join(bits)[:1800]


def parse(html: str, source_name: str = "hiringcafe") -> List[Posting]:
    """Turn a saved or fetched hiring.cafe page into Postings."""
    match = PAYLOAD.search(html or "")
    if not match:
        log.warning("%s: no __NEXT_DATA__ payload in the page", source_name)
        return []
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        log.warning("%s: payload did not parse (%s)", source_name, exc.msg)
        return []

    props = (data.get("props") or {}).get("pageProps") or {}
    hits = props.get("ssrHits") or []
    total = props.get("ssrTotalCount")
    if total and len(hits) < total:
        log.info("%s: page holds %d of %d results; save a later page for the rest",
                 source_name, len(hits), total)

    out: List[Posting] = []
    for hit in hits:
        if not isinstance(hit, dict) or hit.get("is_expired"):
            continue
        v5 = hit.get("v5_processed_job_data") or {}
        info = hit.get("job_information") or {}
        title = (info.get("title") or info.get("job_title_raw") or "").strip()
        company = (v5.get("company_name") or "").strip()
        if not title or not company:
            continue

        when, precision = _publish_date(v5)
        apply_url = (hit.get("apply_url") or "").strip()
        commitment = v5.get("commitment") or []
        out.append(Posting(
            job_id=f"hc-{hit.get('id') or hit.get('objectID') or apply_url}"[:120],
            title=title,
            company=company,
            company_url=(v5.get("company_website") or "").strip(),
            # Both point at the employer: hiring.cafe does not interpose itself.
            listing_url=apply_url,
            portal_url=apply_url,
            location=(v5.get("formatted_workplace_location") or "").strip(),
            work_model=(v5.get("workplace_type") or "").strip(),
            posted_at=when,
            posted_precision=precision,
            source=source_name,
            skills=[s for s in [(v5.get("requirements_summary") or "").strip()] if s],
            notes=_notes(v5),
            term="",
            term_confidence="",
        ))
        if commitment and "Internship" not in commitment:
            out[-1].notes = (out[-1].notes + " | " + ", ".join(commitment)).strip(" |")

    log.info("%s: %d listings", source_name, len(out))
    return out


def fetch(url: str, timeout: int = 30,
          session: Optional[requests.Session] = None) -> List[Posting]:
    """Fetch and parse. Returns [] when the host declines the request."""
    session = session or requests.Session()
    try:
        resp = session.get(url, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.info("hiringcafe: not fetchable from here (%s); "
                 "save the page and use --ingest", exc)
        return []
    return parse(resp.text)
