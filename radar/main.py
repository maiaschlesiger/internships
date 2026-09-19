"""Entry point for the hourly run.

Pipeline order is deliberate and worth preserving: dedup and the recency window
run *before* classification and enrichment, so a steady-state run only spends
API calls on listings it has genuinely never seen. A bootstrap run over four
repos touches ~400 listings; an hourly run typically touches a handful.
"""

from __future__ import annotations

import argparse
import logging
import re
import tempfile
import os
import time
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

import yaml

from . import apollo, classify, dedupe, enrich, jobdesc
from .models import Posting
from .resume import render as resume_render
from .resume import tailor as resume_tailor
from .notion_sink import Notion
from .sources import ghlist, internlist, jobright

log = logging.getLogger("radar")


def collect(cfg: dict, bootstrap: bool) -> List[Posting]:
    """Scrape every configured source. A failing source never sinks the run."""
    postings: List[Posting] = []

    sources = cfg.get("sources") or {}
    for repo in sources.get("jobright_repos", []):
        try:
            found = jobright.fetch(repo)
        except Exception as exc:  # noqa: BLE001
            log.error("source %s failed: %s", repo, exc)
            continue
        history = {}
        if bootstrap:
            try:
                history = jobright.first_seen_from_history(repo)
            except Exception as exc:  # noqa: BLE001
                log.warning("history walk for %s failed (%s); stamping as now", repo, exc)
        jobright.stamp(found, history)
        postings.extend(found)

    for repo in sources.get("github_lists", []):
        try:
            found = ghlist.fetch(repo)
        except Exception as exc:  # noqa: BLE001
            log.error("source %s failed: %s", repo, exc)
            continue
        postings.extend(found)

    for feed in sources.get("internlist_feeds", []):
        try:
            found = internlist.fetch(feed)
        except Exception as exc:  # noqa: BLE001
            log.error("intern-list %s failed: %s", feed, exc)
            continue
        # This source reports its own timestamps; fill gaps with "just seen".
        now = datetime.now(timezone.utc)
        for p in found:
            if p.posted_at is None:
                p.posted_at, p.posted_precision = now, "first_seen"
        postings.extend(found)

    # Six overlapping lists advertise the same jobs, each with its own ids and
    # sometimes its own redirect URLs, so collapse on the posting itself.
    by_id = list({p.job_id: p for p in postings}.values())
    unique = dedupe.collapse(by_id)
    log.info("collected %d listings -> %d distinct jobs", len(postings), len(unique))
    return unique


def apply_scraped_dates(postings, pages) -> None:
    """Prefer the employer's own posting timestamp where the page published one.

    Only upgrades: a page that gives a bare calendar date does not replace a
    commit-derived estimate that is already accurate to about an hour.
    """
    upgraded = 0
    for p in postings:
        data = pages.get(p.job_id)
        if not data or not data.posted_at:
            continue
        better = jobdesc_rank(data.posted_precision) < jobdesc_rank(p.posted_precision)
        if better or p.posted_at is None:
            p.posted_at = data.posted_at
            p.posted_precision = data.posted_precision
            upgraded += 1
    if upgraded:
        log.info("posting time taken from the employer's page for %d listings", upgraded)


def tailor_resumes(client, database_id: str, cfg: dict) -> int:
    """Attach a job-specific resume to each row that is marked as being applied to.

    Only rows the user has flagged are touched: tailoring costs a model call and
    a render per posting, and a resume for a job she will not apply to is waste.
    A posting whose variant fails validation still gets a resume -- the base one,
    unmodified -- because an untailored resume is far better than none.
    """
    base_path = Path(__file__).resolve().parent.parent / "resume" / "base.yaml"
    if not base_path.exists():
        log.error("no resume/base.yaml; nothing to tailor")
        return 2
    base = yaml.safe_load(base_path.read_text())

    rows = client.rows_awaiting_resume(database_id,
                                       status=cfg.get("resume_trigger_status", "Applying"))
    if not rows:
        log.info("no rows are waiting for a resume")
        return 0

    # The stored Skill Requirements are the posting's own words, so they make a
    # good tailoring brief without re-fetching the page. Re-fetch only when the
    # row has nothing useful stored.
    needs_text = [r for r in rows if len(r.get("skills", "")) < 80 and r.get("url")]
    fetched = {}
    if needs_text:
        stubs = [Posting(job_id=r["page_id"], title=r["title"], company=r["company"],
                         source="resume", portal_url=r["url"]) for r in needs_text]
        fetched = jobdesc.fetch_all(stubs, max_chars=cfg.get("description_max_chars", 6000),
                                    workers=cfg.get("description_workers", 6))

    out_dir = Path(tempfile.mkdtemp(prefix="resumes-"))
    written = 0
    for row in rows:
        page = fetched.get(row["page_id"])
        posting = {
            "title": row["title"], "company": row["company"],
            "category": row["category"], "keywords": row.get("keywords") or [],
            "description": (page.text if page and page.text else row.get("skills", "")),
        }
        content, note = resume_tailor.tailor(base, posting)
        safe = re.sub(r"[^A-Za-z0-9]+", "-", f"{row['company']}-{row['title']}").strip("-")[:70]
        pdf = out_dir / f"Maia-Schlesiger-{safe or 'Resume'}.pdf"
        try:
            resume_render.render(content, pdf)
            client.attach_file(row["page_id"], pdf)
            written += 1
            log.info("%s — %s (%s)", row["company"][:28], row["title"][:44], note)
        except Exception as exc:  # noqa: BLE001 - one failure must not lose the rest
            log.error("resume failed for %r at %r: %s", row["title"][:40], row["company"], exc)
        time.sleep(0.35)

    log.info("attached %d/%d resumes", written, len(rows))
    return 0


def backfill(client, database_id: str, cfg: dict, limit: int = 0) -> int:
    """Fill gaps in rows that already exist, without disturbing anything else."""
    rows = client.rows_to_backfill(database_id, limit=limit)
    if not rows:
        log.info("nothing to backfill")
        return 0

    # Reuse the normal page fetcher by wrapping each row as a Posting.
    stubs = [Posting(job_id=r["page_id"], title=r["title"], company=r["company"],
                     source="backfill", portal_url=r["url"]) for r in rows]
    pages = jobdesc.fetch_all(
        stubs,
        max_chars=cfg.get("description_max_chars", 6000),
        workers=cfg.get("description_workers", 6),
    )

    updated = 0
    for row in rows:
        data = pages.get(row["page_id"])
        if not data:
            continue
        skills = data.requirements if row["needs_skills"] else ""
        recruiter = data.contact_email if row["needs_recruiter"] else ""
        notes = data.notes if row.get("needs_notes") else ""
        # Existing rows do not store the original listing URL -- only the
        # resolved Application Portal -- so that is what their title links to.
        # Rows written from here on link to the source listing instead.
        title = row["title"] if row.get("needs_title_link") else ""
        title_url = row["url"] if row.get("needs_title_link") else ""
        if not skills and not recruiter and not notes and not title_url:
            continue
        try:
            client.update_row(row["page_id"], skills=skills, recruiter=recruiter,
                              notes=notes, title=title, title_url=title_url)
            updated += 1
        except Exception as exc:  # noqa: BLE001 - one bad row must not lose the rest
            log.error("could not update %r: %s", row["title"][:40], exc)
        time.sleep(0.35)

    log.info("backfilled %d/%d rows", updated, len(rows))
    return 0


def apply_scraped_emails(postings, pages) -> None:
    """Use the contact address the employer printed in the posting."""
    filled = 0
    for p in postings:
        data = pages.get(p.job_id)
        if data and data.contact_email and not p.recruiter:
            p.recruiter = data.contact_email
            filled += 1
    if filled:
        log.info("contact email taken from the posting for %d listings", filled)


def apply_scraped_notes(postings, pages) -> None:
    """Merge notes found on the page with any the source list already supplied."""
    for p in postings:
        data = pages.get(p.job_id)
        if not data or not data.notes:
            continue
        if p.notes:
            extra = [n for n in data.notes.split(" \u00b7 ") if n not in p.notes]
            if extra:
                p.notes = p.notes + " \u00b7 " + " \u00b7 ".join(extra)
        else:
            p.notes = data.notes


def jobdesc_rank(precision: str) -> int:
    return dedupe.PRECISION_RANK.get(precision, 9)


def within_window(postings: List[Posting], hours: int) -> List[Posting]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    kept = [p for p in postings if p.posted_at and p.posted_at >= cutoff]
    log.info("recency filter (<%dh): %d/%d kept", hours, len(kept), len(postings))
    return kept


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Scrape internships into Notion.")
    ap.add_argument("--config", default=str(Path(__file__).resolve().parent.parent / "config.yaml"))
    ap.add_argument("--bootstrap", action="store_true",
                    help="First run: reconstruct real posting times from source git history "
                         "instead of stamping everything as 'just now'.")
    ap.add_argument("--dry-run", action="store_true", help="Do everything except write to Notion.")
    ap.add_argument("--tailor-resumes", action="store_true",
                    help="For every row marked Applying that has no resume attached, "
                         "tailor the resume to that posting, render a PDF and upload it "
                         "to the row. Does not scrape for new listings.")
    ap.add_argument("--backfill", action="store_true",
                    help="Re-visit rows already in the database that are missing skill "
                         "requirements or a contact email, and fill just those two "
                         "fields. Applied tags, resume PDFs and every other column are "
                         "left untouched. Does not scrape for new listings.")
    ap.add_argument("--clear-database", action="store_true",
                    help="Archive every row in the database before scraping. The database, "
                         "its columns and formulas survive; rows go to the Notion trash, "
                         "where they can be restored. Combine with --bootstrap for a "
                         "clean rebuild.")
    ap.add_argument("--create-database", action="store_true",
                    help="Create the Notion database under NOTION_PARENT_PAGE_ID and print its id.")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    cfg = yaml.safe_load(Path(args.config).read_text())
    token = os.environ.get("NOTION_TOKEN", "")
    database_id = os.environ.get("NOTION_DATABASE_ID", "")

    if args.create_database:
        parent = os.environ.get("NOTION_PARENT_PAGE_ID", "")
        if not token or not parent:
            log.error("--create-database needs NOTION_TOKEN and NOTION_PARENT_PAGE_ID")
            return 2
        new_id = Notion(token).create_database(parent, "Summer 2027 Internships")
        print(f"\nNOTION_DATABASE_ID={new_id}\n")
        print("Add that as a repository secret, then re-run without --create-database.")
        return 0

    if not args.dry_run and not (token and database_id):
        log.error("NOTION_TOKEN and NOTION_DATABASE_ID must be set (or pass --dry-run)")
        return 2

    if args.tailor_resumes:
        if args.dry_run:
            log.error("--tailor-resumes and --dry-run are contradictory; doing nothing")
            return 2
        client = Notion(token)
        client.ensure_schema(database_id)
        return tailor_resumes(client, database_id, cfg)

    if args.backfill:
        if args.dry_run:
            log.error("--backfill and --dry-run are contradictory; doing nothing")
            return 2
        client = Notion(token)
        client.ensure_schema(database_id)
        return backfill(client, database_id, cfg)

    if args.clear_database:
        if args.dry_run:
            log.error("--clear-database and --dry-run are contradictory; doing nothing")
            return 2
        Notion(token).clear(database_id)

    postings = collect(cfg, bootstrap=args.bootstrap)

    # Dedup against what is already in Notion before spending anything.
    if not args.dry_run:
        client = Notion(token)
        # A database created by an older version lacks newer columns; writing to
        # a property Notion does not know about fails the whole page.
        client.ensure_schema(database_id)
        known_ids, known_prints = client.existing_keys(database_id)
        before = len(postings)
        postings = [p for p in postings
                    if p.job_id not in known_ids
                    and dedupe.fingerprint(p) not in known_prints]
        log.info("%d of %d listings are new", len(postings), before)

    postings = within_window(postings, cfg.get("window_hours", 24))
    if not postings:
        log.info("nothing new in the window; done")
        return 0

    postings = classify.classify(postings, cfg)
    if not postings:
        log.info("nothing survived the relevance filter; done")
        return 0

    enrich.resolve_portal(postings)

    # One fetch of each application page serves both the real posting timestamp
    # and the description the keywords are drawn from.
    pages = {}
    if cfg.get("fetch_descriptions", True):
        pages = jobdesc.fetch_all(
            postings,
            max_chars=cfg.get("description_max_chars", 6000),
            workers=cfg.get("description_workers", 6),
        )
        apply_scraped_dates(postings, pages)
        apply_scraped_emails(postings, pages)
        apply_scraped_notes(postings, pages)

    enrich.enrich(postings, cfg, pages)
    apollo.find_recruiters(postings, max_lookups=cfg.get("apollo_max_lookups", 0))

    if args.dry_run:
        print(f"\n--- dry run: {len(postings)} listings would be written ---")
        for p in postings:
            age = ""
            if p.posted_at:
                hrs = (datetime.now(timezone.utc) - p.posted_at).total_seconds() / 3600
                age = f"{hrs:.1f}h"
            print(f"  [{p.term:12}|{p.category[:22]:22}|{age:>6}] {p.title[:60]}")
            print(f"       {p.company} | {p.location[:40]}")
            print(f"       kw: {', '.join(p.resume_keywords[:6])}")
        return 0

    client.add_all(database_id, postings)

    # Self-heal: give a bounded number of existing incomplete rows another try,
    # so improvements to the scraping reach older rows without a manual pass.
    # Bounded on purpose -- a page that never yields a contact email would
    # otherwise be re-fetched every hour forever.
    per_run = cfg.get("backfill_per_run", 0)
    if per_run:
        backfill(client, database_id, cfg, limit=per_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
