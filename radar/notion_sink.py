"""Notion database sink: schema creation, dedup, and row append.

Auth is an internal integration token (``NOTION_TOKEN``). Create one at
notion.so/my-integrations, then share the target page/database with it -- an
integration can only see what has been explicitly shared, which is the most
common reason this module 404s on a page that plainly exists.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Iterable, List, Optional, Sequence, Set

import requests

from .models import Posting

log = logging.getLogger(__name__)

API = "https://api.notion.com/v1"
VERSION = "2022-06-28"

# Property names are the sheet columns you asked for. Changing a name here
# without changing it in Notion creates a duplicate column, so treat these as
# the single source of truth.
P_TITLE = "Title"
P_COMPANY = "Company"
P_CATEGORY = "Category"
P_TERM = "Term"
P_LOCATION = "Location"
P_PORTAL = "Application Portal"
P_KEYWORDS = "Resume Keywords"
P_SKILLS = "Skill Requirements"
P_POSTED = "Posted"
P_AGE = "Hours Since Posted"
P_RECRUITER = "Recruiter Contact"
P_APPLIED = "Applied"
P_RESUME = "My Resume PDF"
P_SOURCE = "Source"
P_JOB_ID = "Job ID"

APPLIED_OPTIONS = [
    {"name": "Not applied", "color": "default"},
    {"name": "Applying", "color": "yellow"},
    {"name": "Applied", "color": "blue"},
    {"name": "Interviewing", "color": "purple"},
    {"name": "Offer", "color": "green"},
    {"name": "Rejected", "color": "red"},
]

SCHEMA = {
    P_TITLE: {"title": {}},
    P_COMPANY: {"rich_text": {}},
    P_CATEGORY: {"select": {}},
    P_TERM: {"select": {}},
    P_LOCATION: {"rich_text": {}},
    P_PORTAL: {"url": {}},
    P_KEYWORDS: {"multi_select": {}},
    P_SKILLS: {"rich_text": {}},
    P_POSTED: {"date": {}},
    # Notion re-evaluates now() when the page is viewed, which is what makes
    # this column self-updating without any scheduled write.
    P_AGE: {"formula": {"expression": f'dateBetween(now(), prop("{P_POSTED}"), "hours")'}},
    P_RECRUITER: {"email": {}},
    P_APPLIED: {"select": {"options": APPLIED_OPTIONS}},
    P_RESUME: {"files": {}},
    P_SOURCE: {"select": {}},
    P_JOB_ID: {"rich_text": {}},
}


class Notion:
    def __init__(self, token: str):
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "Notion-Version": VERSION,
            "Content-Type": "application/json",
        })

    def _call(self, method: str, path: str, **kw) -> dict:
        """One request with retry on Notion's 429 and transient 5xx."""
        for attempt in range(5):
            resp = self.s.request(method, f"{API}{path}", timeout=30, **kw)
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = float(resp.headers.get("Retry-After", 2 ** attempt))
                log.warning("notion %s -> %s, retrying in %.0fs", path, resp.status_code, wait)
                time.sleep(wait)
                continue
            if not resp.ok:
                raise RuntimeError(f"Notion {method} {path} -> {resp.status_code}: {resp.text[:400]}")
            return resp.json()
        raise RuntimeError(f"Notion {method} {path} kept failing after retries")

    def create_database(self, parent_page_id: str, title: str) -> str:
        payload = {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [{"type": "text", "text": {"content": title}}],
            "properties": SCHEMA,
        }
        db = self._call("POST", "/databases", json=payload)
        log.info("created database %s (%s)", title, db["id"])
        return db["id"]

    def existing_job_ids(self, database_id: str) -> Set[str]:
        """Every Job ID already in the database -- the dedup key."""
        ids: Set[str] = set()
        cursor: Optional[str] = None
        while True:
            body = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            page = self._call("POST", f"/databases/{database_id}/query", json=body)
            for row in page.get("results", []):
                prop = row.get("properties", {}).get(P_JOB_ID, {})
                for chunk in prop.get("rich_text", []):
                    text = chunk.get("plain_text", "").strip()
                    if text:
                        ids.add(text)
            if not page.get("has_more"):
                break
            cursor = page.get("next_cursor")
        log.info("database already holds %d listings", len(ids))
        return ids

    def add(self, database_id: str, p: Posting) -> None:
        def rt(value: str) -> dict:
            return {"rich_text": [{"type": "text", "text": {"content": value[:2000]}}]} if value else {"rich_text": []}

        props: Dict[str, dict] = {
            P_TITLE: {"title": [{"type": "text", "text": {"content": p.title[:2000]}}]},
            P_COMPANY: rt(p.company),
            P_LOCATION: rt(p.location),
            P_SKILLS: rt(p.skills_cell()),
            P_JOB_ID: rt(p.job_id),
            # Notion rejects multi_select values containing a comma.
            P_KEYWORDS: {"multi_select": [
                {"name": k.replace(",", " ")[:100]} for k in p.resume_keywords[:25]
            ]},
            P_APPLIED: {"select": {"name": "Not applied"}},
        }
        if p.category:
            props[P_CATEGORY] = {"select": {"name": p.category[:100]}}
        if p.term:
            props[P_TERM] = {"select": {"name": p.term[:100]}}
        if p.source:
            props[P_SOURCE] = {"select": {"name": p.source[:100]}}
        if p.portal_url or p.listing_url:
            props[P_PORTAL] = {"url": p.portal_url or p.listing_url}
        if p.posted_at:
            props[P_POSTED] = {"date": {"start": p.posted_at.isoformat()}}
        if p.recruiter:
            props[P_RECRUITER] = {"email": p.recruiter}

        self._call("POST", "/pages", json={
            "parent": {"database_id": database_id},
            "properties": props,
        })

    def add_all(self, database_id: str, postings: Sequence[Posting]) -> int:
        """Append postings one by one, surviving individual failures."""
        written = 0
        for p in postings:
            try:
                self.add(database_id, p)
                written += 1
            except Exception as exc:  # noqa: BLE001 - one bad row must not lose the rest
                log.error("failed to write %r (%s): %s", p.title[:50], p.job_id, exc)
            time.sleep(0.35)  # stay under Notion's ~3 req/s ceiling
        log.info("wrote %d/%d new listings", written, len(postings))
        return written
