#!/usr/bin/env python3
"""Report the real shape of a job-list page, from a network that can reach it.

Written for apmseason.com, which is unreachable from the environment the parser
is written in. Everything is printed to the job log rather than uploaded, so a
parser can be written from evidence instead of guesswork -- the first
intern-list parser was written blind and had to be replaced.

Prints, per candidate URL: status, content type, size, whether a Next.js
payload is embedded, the field names that look like a listing, and a sample
record. Bounded so the log stays readable.
"""

from __future__ import annotations

import json
import re
import sys

import requests

CANDIDATES = [
    "https://www.apmseason.com/internships",
    "https://www.apmseason.com/jobs/internships",
    "https://www.apmseason.com/list/internships",
    "https://www.apmseason.com/jobs?type=internship",
    "https://www.apmseason.com/",
]

# Keys a listing feed tends to use. Presence tells us where the data lives.
LISTING_HINTS = ("company", "title", "role", "location", "postedat", "posteddate",
                 "datepost", "applyurl", "apply_url", "url", "link", "jobs",
                 "positions", "listings", "internships", "season", "deadline")

UA = {"User-Agent": "Mozilla/5.0 (compatible; internship-radar-probe/1.0)",
      "Accept": "text/html,application/json;q=0.9,*/*;q=0.8"}


def embedded_json(html: str):
    """Return (name, parsed) for the biggest embedded JSON blob, or None."""
    best = None
    for pattern, name in (
        (r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', "__NEXT_DATA__"),
        (r'<script[^>]+type="application/json"[^>]*>(.*?)</script>', "application/json"),
        (r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', "__next_f (RSC)"),
        (r'window\.__NUXT__\s*=\s*(\{.*?\});', "__NUXT__"),
    ):
        for blob in re.findall(pattern, html, re.S):
            if best is None or len(blob) > len(best[1]):
                best = (name, blob)
    if not best:
        return None
    name, blob = best
    try:
        return name, json.loads(blob)
    except json.JSONDecodeError:
        return name, blob[:400]  # unparsed: show a sample so the shape is visible


def walk_for_listings(node, depth=0, path="$"):
    """Find lists of dicts that look like job listings."""
    out = []
    if depth > 8:
        return out
    if isinstance(node, list) and node and isinstance(node[0], dict):
        keys = {re.sub(r"[^a-z]", "", k.lower()) for k in node[0]}
        if sum(1 for h in LISTING_HINTS if h in keys) >= 2:
            out.append((path, len(node), node[0]))
    if isinstance(node, dict):
        for k, v in node.items():
            out += walk_for_listings(v, depth + 1, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node[:6]):
            out += walk_for_listings(v, depth + 1, f"{path}[{i}]")
    return out


def probe(url: str, session: requests.Session) -> None:
    print(f"\n{'='*72}\n{url}")
    try:
        resp = session.get(url, timeout=30, allow_redirects=True)
    except requests.RequestException as exc:
        print(f"  FAILED: {exc}")
        return
    ctype = resp.headers.get("Content-Type", "")
    print(f"  status {resp.status_code} | {ctype} | {len(resp.content)} bytes")
    if resp.url != url:
        print(f"  redirected to {resp.url}")
    if not resp.ok:
        return

    body = resp.text
    if "json" in ctype:
        try:
            payload = resp.json()
            print("  JSON response. Listing-shaped arrays:")
            for path, count, sample in walk_for_listings(payload)[:4]:
                print(f"    {path}  ({count} items)")
                print(f"      keys: {sorted(sample)}")
                print(f"      sample: {json.dumps(sample)[:600]}")
            return
        except json.JSONDecodeError:
            pass

    found = embedded_json(body)
    if found:
        name, payload = found
        print(f"  embedded payload: {name}")
        if isinstance(payload, str):
            print(f"    (did not parse) sample: {payload[:300]}")
        else:
            hits = walk_for_listings(payload)
            if hits:
                for path, count, sample in hits[:4]:
                    print(f"    {path}  ({count} items)")
                    print(f"      keys: {sorted(sample)}")
                    print(f"      sample: {json.dumps(sample)[:600]}")
            else:
                print(f"    no listing-shaped array found; top keys: "
                      f"{sorted(payload)[:20] if isinstance(payload, dict) else type(payload)}")
    else:
        print("  no embedded JSON payload")

    # Fall back to describing the HTML, so a DOM parser could be written.
    rows = len(re.findall(r"<tr\b", body, re.I))
    tables = len(re.findall(r"<table\b", body, re.I))
    links = re.findall(r'href="(https?://[^"]+)"', body)
    external = [l for l in links if "apmseason" not in l]
    print(f"  html: {tables} tables, {rows} <tr>, {len(links)} links "
          f"({len(external)} external)")
    for sample in external[:8]:
        print(f"    -> {sample[:110]}")
    fields = sorted({m.lower() for m in re.findall(r'"([A-Za-z_]{3,24})"\s*:', body)
                     if any(h in m.lower() for h in LISTING_HINTS)})
    if fields:
        print(f"  candidate field names: {fields[:25]}")


def main() -> int:
    urls = sys.argv[1:] or CANDIDATES
    session = requests.Session()
    session.headers.update(UA)
    for url in urls:
        probe(url, session)
    print(f"\n{'='*72}\ndone")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
