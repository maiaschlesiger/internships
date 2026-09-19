"""Entry point for the hourly run.

Pipeline order is deliberate and worth preserving: dedup and the recency window
run *before* classification and enrichment, so a steady-state run only spends
API calls on listings it has genuinely never seen. A bootstrap run over four
repos touches ~400 listings; an hourly run typically touches a handful.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

import yaml

from . import apollo, classify, dedupe, enrich, jobdesc
from .models import Posting
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

    if args.clear_database:
        if args.dry_run:
            log.error("--clear-database and --dry-run are contradictory; doing nothing")
            return 2
        Notion(token).clear(database_id)

    postings = collect(cfg, bootstrap=args.bootstrap)

    # Dedup against what is already in Notion before spending anything.
    if not args.dry_run:
        client = Notion(token)
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

    enrich.enrich(postings, cfg, pages)
    apollo.find_recruiters(postings)

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
    return 0


if __name__ == "__main__":
    sys.exit(main())
