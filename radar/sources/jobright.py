"""Scraper for the jobright-ai internship repos.

The listings live in a markdown table in README.md on the ``master`` branch
(not ``main`` -- getting that wrong yields a silent 404). Each row looks like:

    | **[Company](https://company.com)** | **[Title](https://jobright.ai/jobs/info/<id>?utm=..)** | City, ST | Hybrid | Sep 18 |

A company cell of ``↳`` means "same company as the row above", so rows must be
parsed in order with the last seen company carried forward.

The ``Date Posted`` column only has day granularity, which is useless for a
"posted in the last 24 hours" filter. We get real precision two ways:

* steady state -- the repo commits roughly hourly, so an id appearing for the
  first time on a given run was added within one cron interval;
* bootstrap -- walk the repo's own commit history once and record the commit
  that first introduced each id.
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import requests

from ..models import Posting

log = logging.getLogger(__name__)

RAW_README = "https://raw.githubusercontent.com/{repo}/master/README.md"
CLONE_URL = "https://github.com/{repo}.git"

# A table row: five pipe-delimited cells. Non-greedy so trailing pipes in link
# titles do not swallow the row.
ROW_RE = re.compile(r"^\|(.+?)\|(.+?)\|(.*?)\|(.*?)\|(.*?)\|\s*$")
# Link labels routinely contain square brackets of their own, e.g.
# "**[SAP iXp Intern - Product Management [Newtown Square, PA]](https://...)**",
# which no non-greedy [^\]]* pattern can survive. Split on the last "](" instead.
JOB_ID_RE = re.compile(r"jobs/info/([0-9a-f]{24})")
CONTINUATION = "↳"  # the ↳ glyph marking a same-company row


def _split_link(cell: str) -> tuple:
    """Return ``(label, url)`` for a markdown link cell.

    Tolerates brackets inside the label by anchoring on the final "](" rather
    than scanning for the first balanced pair.
    """
    cell = cell.strip()
    sep = cell.rfind("](")
    if sep == -1:
        return cell, ""
    open_bracket = cell.find("[")
    close_paren = cell.find(")", sep + 2)
    if open_bracket == -1 or open_bracket > sep or close_paren == -1:
        return cell, ""
    return cell[open_bracket + 1:sep], cell[sep + 2:close_paren]


def _clean(text: str) -> str:
    return text.replace("**", "").replace("*", "").strip()


def _cell_text(cell: str) -> str:
    """Strip markdown emphasis/links down to display text."""
    label, _ = _split_link(cell)
    return _clean(label)


def _cell_url(cell: str) -> str:
    _, url = _split_link(cell)
    return url.strip()


def parse_readme(markdown: str, source_name: str) -> List[Posting]:
    """Parse the README table into Postings, resolving ↳ continuation rows."""
    postings: List[Posting] = []
    last_company = ""
    last_company_url = ""

    for line in markdown.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        m = ROW_RE.match(line.strip())
        if not m:
            continue

        company_cell, title_cell, location, work_model, _date_cell = m.groups()

        title = _cell_text(title_cell)
        title_url = _cell_url(title_cell)

        # Header and separator rows.
        if title.lower() in {"job title", "---------", ""} or set(title) <= {"-", " "}:
            continue

        job_id_match = JOB_ID_RE.search(title_url)
        if not job_id_match:
            # Without a stable id we cannot dedup across runs; skipping is
            # safer than emitting a row that duplicates every hour.
            log.debug("skipping row with no job id: %s", title)
            continue

        company_text = _cell_text(company_cell)
        if company_text.strip() == CONTINUATION or not company_text:
            company = last_company
            company_url = last_company_url
        else:
            company = company_text
            company_url = _cell_url(company_cell)
            last_company, last_company_url = company, company_url

        postings.append(
            Posting(
                job_id=job_id_match.group(1),
                title=title,
                company=company,
                company_url=company_url,
                listing_url=title_url,
                location=_cell_text(location),
                work_model=_cell_text(work_model),
                source=source_name,
            )
        )

    return postings


def fetch(repo: str, session: Optional[requests.Session] = None) -> List[Posting]:
    """Fetch and parse the current README for ``repo`` (``owner/name``)."""
    session = session or requests.Session()
    url = RAW_README.format(repo=repo)
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    postings = parse_readme(resp.text, source_name=repo)
    log.info("%s: parsed %d listings", repo, len(postings))
    return postings


def first_seen_from_history(repo: str, days: int = 10) -> Dict[str, datetime]:
    """Map job_id -> UTC time of the commit that first introduced it.

    Used to bootstrap the very first run, where "we just saw it" would wrongly
    stamp every historical listing with the current time. Walks commits oldest
    to newest in a throwaway clone and records each id's debut.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    first_seen: Dict[str, datetime] = {}

    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "src"
        subprocess.run(
            ["git", "clone", "--filter=blob:none", "--no-checkout",
             CLONE_URL.format(repo=repo), str(dest)],
            check=True, capture_output=True, timeout=600,
        )
        log_out = subprocess.run(
            ["git", "-C", str(dest), "log", "--reverse", "--format=%H %cI",
             f"--since={cutoff.isoformat()}", "master", "--", "README.md"],
            check=True, capture_output=True, text=True, timeout=300,
        ).stdout

        for line in log_out.splitlines():
            if not line.strip():
                continue
            sha, _, iso = line.partition(" ")
            when = datetime.fromisoformat(iso.strip()).astimezone(timezone.utc)
            try:
                blob = subprocess.run(
                    ["git", "-C", str(dest), "show", f"{sha}:README.md"],
                    check=True, capture_output=True, text=True, timeout=60,
                ).stdout
            except subprocess.CalledProcessError:
                continue
            for job_id in JOB_ID_RE.findall(blob):
                first_seen.setdefault(job_id, when)

    log.info("%s: reconstructed first-seen for %d ids", repo, len(first_seen))
    return first_seen


def stamp(
    postings: Iterable[Posting],
    history: Optional[Dict[str, datetime]] = None,
    now: Optional[datetime] = None,
) -> None:
    """Attach posted_at/posted_precision in place."""
    now = now or datetime.now(timezone.utc)
    history = history or {}
    for p in postings:
        if p.job_id in history:
            p.posted_at = history[p.job_id]
            p.posted_precision = "commit"
        else:
            p.posted_at = now
            p.posted_precision = "first_seen"
