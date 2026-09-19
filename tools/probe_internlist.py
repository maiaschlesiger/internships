#!/usr/bin/env python3
"""Capture what the intern-list feeds actually return, so the parser is written
from evidence rather than inference.

intern-list.com renders nothing itself -- its listings live in an iframe served
by jobright.ai. This probes those embed URLs (and the Airtable fallbacks) and
writes both the raw responses and a structural summary to ./probe-output/.

Run it from somewhere with network access to jobright.ai; the probe-internlist
job in the workflow does exactly that and uploads the result as an artifact.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from radar.sources.internlist import AIRTABLE, EMBED, FEEDS  # noqa: E402

OUT = pathlib.Path("probe-output")
AGE_RE = re.compile(r"\d+\s*(?:hour|day|minute)s?\s*ago", re.I)
JSON_KEY_RE = re.compile(r'"(\w{3,30})"\s*:')


def probe(name: str, url: str, session: requests.Session) -> list:
    lines = [f"== {name}", f"url: {url}"]
    try:
        resp = session.get(url, timeout=30,
                           headers={"Accept": "application/json, text/html"})
    except requests.RequestException as exc:
        lines.append(f"FAILED: {exc}")
        return lines

    body = resp.text
    (OUT / f"{name}.txt").write_text(body, encoding="utf-8")

    lines += [
        f"status: {resp.status_code}",
        f"content-type: {resp.headers.get('Content-Type', '')}",
        f"bytes: {len(body)}",
        f"looks like JSON: {body.lstrip()[:1] in '{['}",
        f"__NEXT_DATA__ present: {'__NEXT_DATA__' in body}",
        f"relative ages found: {len(AGE_RE.findall(body))}",
        f"24-hex job ids found: {len(set(re.findall(r'[0-9a-f]{24}', body)))}",
    ]

    # The most useful signal for writing a mapper: which field names exist.
    keys = sorted(set(JSON_KEY_RE.findall(body)))
    interesting = [k for k in keys if re.search(
        r"title|company|location|url|link|time|date|post|job|remote|work", k, re.I)]
    lines.append(f"candidate field names ({len(interesting)}): {', '.join(interesting[:60])}")

    try:
        payload = resp.json()
        lines.append("top-level keys: " + ", ".join(list(payload)[:20]
                     if isinstance(payload, dict) else [f"list[{len(payload)}]"]))
        (OUT / f"{name}.pretty.json").write_text(
            json.dumps(payload, indent=2)[:400_000], encoding="utf-8")
    except (json.JSONDecodeError, ValueError):
        pass
    return lines


def main() -> int:
    OUT.mkdir(exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (compatible; internship-radar-probe/1.0)"

    summary: list = []
    # The landing page itself, for re-deriving the feed map if the site changes.
    summary += probe("landing-pm", "https://www.intern-list.com/?k=pm", session) + [""]

    for feed, path in FEEDS.items():
        summary += probe(f"embed-{feed}", EMBED.format(path=path), session) + [""]

    for feed, ids in AIRTABLE.items():
        summary += probe(f"airtable-{feed}",
                         f"https://airtable.com/embed/{ids}?viewControls=on", session) + [""]

    (OUT / "SUMMARY.txt").write_text("\n".join(summary), encoding="utf-8")
    print("\n".join(summary))
    print(f"\nwrote {len(list(OUT.iterdir()))} files to {OUT}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
