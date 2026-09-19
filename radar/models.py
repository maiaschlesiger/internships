"""Core record type shared by every source, filter and sink."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


@dataclass
class Posting:
    """One internship listing, progressively enriched as it moves down the pipeline.

    ``job_id`` is the dedup key and must be stable across runs for a given
    listing. Sources are responsible for choosing something durable (jobright
    uses the 24-hex id embedded in its listing URL).
    """

    job_id: str
    title: str
    company: str
    source: str

    company_url: str = ""
    listing_url: str = ""
    # Resolved employer-side application page. Falls back to listing_url when
    # the redirect chain cannot be followed (see enrich.resolve_portal).
    portal_url: str = ""
    location: str = ""
    work_model: str = ""

    # Always tz-aware UTC. ``posted_precision`` records how much to trust it:
    #   "commit"     - derived from the source repo's commit history (~70 min)
    #   "first_seen" - the first run that observed this id (<= cron interval)
    #   "day"        - only a calendar date was available
    posted_at: Optional[datetime] = None
    posted_precision: str = "unknown"

    term: str = ""
    term_confidence: str = ""
    category: str = ""

    resume_keywords: List[str] = field(default_factory=list)
    skills: List[str] = field(default_factory=list)
    recruiter: str = ""
    # Short labelled facts worth knowing before applying: pay, deadline,
    # sponsorship, programme length. Never inferred -- see jobdesc.extract_notes.
    notes: str = ""

    def keywords_cell(self) -> str:
        return ", ".join(self.resume_keywords)

    def skills_cell(self) -> str:
        return ", ".join(self.skills)
