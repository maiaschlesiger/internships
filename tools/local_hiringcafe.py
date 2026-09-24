#!/usr/bin/env python3
"""Open hiring.cafe in your own Chrome, then hand the page to the parser.

hiring.cafe declines scripted requests -- from a server and from a laptop
alike -- but serves the page normally to a browser. This drives the Chrome
already installed on this machine, which is the same thing that happens when
you open the tab yourself, and reads the listings out of the page it returns.

It is deliberately plain: no stealth extensions, no proxies, no attempt to look
like something it is not. If Chrome is refused too, that is the site's answer
and this prints it rather than trying again differently.

    python3 tools/local_hiringcafe.py                 # save the HTML only
    python3 tools/local_hiringcafe.py --to-notion     # and write it to Notion

Run it about as often as you would check the site. It is one page load.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from radar.sources.hiringcafe import parse  # noqa: E402

DEFAULT_URL = ("https://hiringcafe.com/?searchState=%7B%22commitmentTypes%22%3A"
               "%5B%22Internship%22%5D%2C%22dateFetchedPastNDays%22%3A7%2C"
               "%22departments%22%3A%5B%22Design%22%2C%22Product+Management%22"
               "%2C%22Marketing%22%5D%7D")

CHROME_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
]


def find_chrome() -> str:
    for path in CHROME_PATHS:
        if Path(path).exists():
            return path
    raise SystemExit("No Chrome found. Install Chrome, or save the page by hand "
                     "and use: python3 -m radar.main --ingest <file>")


def load(url: str, chrome: str, wait_ms: int) -> str:
    """Load the page and return the DOM once its scripts have run."""
    proc = subprocess.run(
        [chrome, "--headless=new", "--disable-gpu",
         f"--virtual-time-budget={wait_ms}", "--dump-dom", url],
        capture_output=True, text=True, timeout=180)
    return proc.stdout or ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--wait-ms", type=int, default=15000,
                    help="How long to let the page's scripts run (default 15s)")
    ap.add_argument("--out", default=str(Path.home() / "Downloads" / "hiringcafe.html"))
    ap.add_argument("--to-notion", action="store_true",
                    help="Write the listings to Notion as well as saving the page")
    args = ap.parse_args()

    chrome = find_chrome()
    print(f"loading in {Path(chrome).name} ...")
    html = load(args.url, chrome, args.wait_ms)

    posts = parse(html)
    if not posts:
        print("\nNo listings found. Either the page did not finish loading, or "
              "Chrome was refused as well.")
        print("If it was refused, open the URL yourself, save the page, and run:")
        print("  python3 -m radar.main --ingest <the saved file>")
        Path(args.out).write_text(html, encoding="utf-8")
        print(f"\n(what came back is saved at {args.out} -- {len(html)} bytes)")
        return 1

    Path(args.out).write_text(html, encoding="utf-8")
    print(f"{len(posts)} listings, page saved to {args.out}")
    for p in posts[:5]:
        stamp = p.posted_at.strftime("%b %d %H:%M") if p.posted_at else "?"
        print(f"  {stamp}  {p.title[:48]:50s} {p.company[:24]}")
    if len(posts) > 5:
        print(f"  ... and {len(posts) - 5} more")

    if args.to_notion:
        if not (os.environ.get("NOTION_TOKEN") and os.environ.get("NOTION_DATABASE_ID")):
            print("\nSet NOTION_TOKEN and NOTION_DATABASE_ID to write to Notion.")
            return 1
        print()
        return subprocess.call([sys.executable, "-m", "radar.main",
                                "--ingest", args.out], cwd=str(ROOT))
    print(f"\nTo write these to Notion:\n  python3 -m radar.main --ingest {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
