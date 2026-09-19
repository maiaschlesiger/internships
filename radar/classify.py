"""Two-stage relevance filter: cheap keyword rules, then Claude on the leftovers.

Most listings are decided by rules alone -- "Architecture Intern" is never
relevant and "Product Management Intern (Summer 2027)" always is. Only genuinely
ambiguous titles cost an API call, which keeps the hourly run close to free
while still handling the fuzzy part of the brief ("can even be a little
technical, but definitely not something like an architecture intern").
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from .models import Posting

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-5"


@dataclass
class Verdict:
    include: bool
    term: str
    category: str
    confidence: str  # "rule" | "model" | "flagged"
    reason: str


def _hay(p: Posting) -> str:
    """Matching surface for the rules: the title only.

    The company name is deliberately excluded. Matching it too rejected
    "Intern/Co Op - Business Analytics-Intelligence" because the employer was a
    construction firm -- but a product or analytics role is still relevant
    whoever posts it. Only the role itself decides the field.
    """
    return p.title.lower()


def _matches(haystack: str, patterns: Sequence[str]) -> Optional[str]:
    for pat in patterns:
        if pat.lower() in haystack:
            return pat
    return None


def rule_verdict(p: Posting, cfg: dict) -> Verdict:
    """Decide by keyword rules alone. confidence == "flagged" means unresolved."""
    hay = _hay(p)

    hit = _matches(hay, cfg.get("reject_categories", []))
    if hit:
        return Verdict(False, "", "", "rule", f"excluded field: {hit!r}")

    # Degree gate. Guard "mba" against false hits inside longer words.
    for pat in cfg.get("reject_degree_patterns", []):
        if pat == "mba":
            if re.search(r"\bmba\b", hay):
                return Verdict(False, "", "", "rule", "graduate-only (MBA)")
        elif pat in hay:
            return Verdict(False, "", "", "rule", f"graduate-only ({pat})")

    term_cfg = cfg["target_term"]
    wrong_term = _matches(hay, term_cfg.get("reject_patterns", []))
    right_term = _matches(hay, term_cfg.get("accept_patterns", []))
    # An explicit target-term match wins over a reject pattern, so a title like
    # "Summer 2027 (Class of 2027 & 2028)" is not thrown away by "class of 2027".
    if wrong_term and not right_term:
        return Verdict(False, "", "", "rule", f"wrong term: {wrong_term!r}")

    term = term_cfg["label"] if right_term else "Unspecified"

    category = ""
    for cat in cfg.get("categories", []):
        if _matches(hay, cat["patterns"]):
            category = cat["name"]
            break

    if category and right_term:
        return Verdict(True, term, category, "rule", "clear match")

    # Something is unresolved -- hand it to the model.
    missing = []
    if not category:
        missing.append("category")
    if not right_term:
        missing.append("term")
    return Verdict(
        bool(category) or term_cfg.get("unspecified_action") == "include_flagged",
        term,
        category or "Uncertain",
        "flagged",
        "unresolved: " + ", ".join(missing),
    )


PROMPT = """You are filtering internship listings for a Cornell CS undergraduate \
graduating in 2028, who is looking for a SUMMER 2027 internship.

She wants: product management, product design (UX/UI/product), consulting or \
strategy, and roles that are somewhat technical but still product- or \
design-oriented (e.g. technical PM, product analyst, solutions engineering).

She does NOT want: roles in unrelated fields (architecture, civil/mechanical \
engineering, nursing, accounting, sales, HR, supply chain), and not roles \
restricted to MBA, master's or PhD candidates.

Term rule: include a listing if it is for Summer 2027, or if it names no term at \
all (an unlabelled internship req posted in September 2026 is almost certainly \
for summer 2027). Exclude it if it names a different term (Winter 2027, Fall \
2026, Summer 2026, etc.).

For each listing below, respond with one JSON object per listing.

Return ONLY a JSON array, no prose. Each element:
{"id": "<the id given>", "include": true|false, "category": "Product Management"\
|"Product Design"|"Consulting / Strategy"|"Technical / Adjacent"|"Other", \
"term": "Summer 2027"|"Unspecified"|"<other term>", "reason": "<8 words max>"}

Listings:
%s
"""


def model_adjudicate(postings: Sequence[Posting], api_key: str) -> Dict[str, dict]:
    """Ask Claude about the ambiguous ones. Returns job_id -> verdict dict."""
    if not postings:
        return {}
    try:
        import anthropic
    except ImportError:
        log.warning("anthropic SDK missing; keeping keyword verdicts")
        return {}

    listing_lines = "\n".join(
        f'- id={p.job_id} | title={p.title!r} | company={p.company!r}' for p in postings
    )
    client = anthropic.Anthropic(api_key=api_key)
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=4000,
            messages=[{"role": "user", "content": PROMPT % listing_lines}],
        )
        text = resp.content[0].text.strip()
    except Exception as exc:  # noqa: BLE001 - never let the model break the run
        log.warning("model adjudication failed (%s); keeping keyword verdicts", exc)
        return {}

    # Tolerate the model wrapping JSON in a fence.
    match = re.search(r"\[.*\]", text, re.S)
    if not match:
        log.warning("model returned no JSON array; keeping keyword verdicts")
        return {}
    try:
        rows = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        log.warning("model JSON did not parse (%s); keeping keyword verdicts", exc)
        return {}

    return {row["id"]: row for row in rows if isinstance(row, dict) and "id" in row}


def classify(postings: Sequence[Posting], cfg: dict) -> List[Posting]:
    """Return only the postings that pass, with term/category filled in."""
    verdicts = {p.job_id: rule_verdict(p, cfg) for p in postings}

    decided = [p for p in postings if verdicts[p.job_id].confidence == "rule"]
    rejected_by_rule = [p for p in decided if not verdicts[p.job_id].include]
    uncertain = [p for p in postings if verdicts[p.job_id].confidence == "flagged"]
    log.info(
        "rules: %d accepted, %d rejected, %d uncertain",
        len(decided) - len(rejected_by_rule), len(rejected_by_rule), len(uncertain),
    )

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if uncertain and cfg.get("use_model_for_uncertain") and api_key:
        for job_id, row in model_adjudicate(uncertain, api_key).items():
            if job_id not in verdicts:
                continue
            verdicts[job_id] = Verdict(
                include=bool(row.get("include")),
                term=row.get("term") or "Unspecified",
                category=row.get("category") or "Uncertain",
                confidence="model",
                reason=str(row.get("reason", ""))[:80],
            )
    elif uncertain:
        log.info("model pass skipped (no key or disabled); %d kept on rules", len(uncertain))

    kept: List[Posting] = []
    for p in postings:
        v = verdicts[p.job_id]
        if not v.include:
            log.debug("drop %s (%s): %s", p.title[:40], p.company, v.reason)
            continue
        p.term = v.term
        p.term_confidence = v.confidence
        p.category = v.category
        kept.append(p)

    log.info("classified: %d/%d kept", len(kept), len(postings))
    return kept
