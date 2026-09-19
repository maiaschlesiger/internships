#!/usr/bin/env python3
"""Capture intern-list.com's real markup so the parser can be written from evidence.

Run this from an environment with network access to intern-list.com (a GitHub
runner works; see the probe-internlist workflow job). It writes the raw HTML and
a short structural summary to ./probe-output/ for inspection.
"""

from __future__ import annotations

import pathlib
import re
import sys

import requests

AGE_RE = re.compile(r"\\d+\\s*(?:hour|day|minute)s?\\s*ago", re.I)

OUT = pathlib.Path("probe-output")
FEEDS = ["pm", "cd"]


def main() -> int:
    OUT.mkdir(exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (compatible; internship-radar-probe/1.0)"

    for feed in FEEDS:
        url = f"https://www.intern-list.com/?k={feed}"
        try:
            resp = session.get(url, timeout=30)
        except requests.RequestException as exc:
            print(f"{feed}: request failed: {exc}", file=sys.stderr)
            continue

        html = resp.text
        (OUT / f"{feed}.html").write_text(html, encoding="utf-8")

        summary = [
            f"url: {url}",
            f"status: {resp.status_code}",
            f"content-type: {resp.headers.get('Content-Type', '')}",
            f"bytes: {len(html)}",
            f"<table> count: {html.count('<table')}",
            f"<tr> count: {html.count('<tr')}",
            f"__NEXT_DATA__ present: {'__NEXT_DATA__' in html}",
            f"relative ages found: {len(AGE_RE.findall(html))}",
            "",
            "-- candidate class names --",
        ]
        classes = sorted({c for c in re.findall(r'class="([^"]{1,60})"', html)})[:60]
        summary.extend(classes)
        (OUT / f"{feed}-summary.txt").write_text("\n".join(summary), encoding="utf-8")
        print(f"{feed}: {resp.status_code}, {len(html)} bytes -> probe-output/{feed}.html")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
