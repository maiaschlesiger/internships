"""Turn the resume content model into a PDF.

The layout lives in template.html and is never generated -- only text is
substituted, so a tailored variant cannot alter the structure even if the model
returns something unexpected. Rendering is headless Chromium, which is present
both on GitHub runners (via Playwright's bundled build) and on a developer
machine with Chrome installed.
"""

from __future__ import annotations

import html
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

log = logging.getLogger(__name__)

TEMPLATE = Path(__file__).with_name("template.html")
FONT_DIR = Path(__file__).resolve().parent.parent.parent / "resume" / "fonts"

# Playwright's bundled Chromium first, then anything on PATH.
CHROME_CANDIDATES = (
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
    "/opt/pw-browsers/chromium_headless_shell-1194/chrome-linux/headless_shell",
    "chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)


def find_chrome() -> Optional[str]:
    for candidate in CHROME_CANDIDATES:
        if os.path.sep in candidate:
            if Path(candidate).exists():
                return candidate
        else:
            found = shutil.which(candidate)
            if found:
                return found
    # Playwright layout varies by version; search for any bundled build.
    root = Path("/opt/pw-browsers")
    if root.exists():
        for name in ("chrome", "headless_shell"):
            for path in root.glob(f"*/chrome-linux/{name}"):
                return str(path)
    return None


def _esc(text: str) -> str:
    return html.escape(str(text), quote=False)


def build_html(data: dict) -> str:
    doc = TEMPLATE.read_text(encoding="utf-8")

    entries: List[str] = []
    for role in data["experience"]:
        bullets = "".join(f"<li>&bull;&nbsp; {_esc(b)}</li>" for b in role["bullets"])
        entries.append(
            f'<div class="entry"><div class="head">'
            f'<span>{_esc(role["org"])} &middot; {_esc(role["role"])}</span>'
            f'<span class="meta">{_esc(role["where"])} &nbsp;&middot;&nbsp; {_esc(role["when"])}</span>'
            f'</div><ul>{bullets}</ul></div>'
        )

    leads: List[str] = []
    for role in data["leadership"]:
        bullets = "".join(f"<li>&bull;&nbsp; {_esc(b)}</li>" for b in role["bullets"])
        leads.append(
            f'<div class="entry"><div class="head">'
            f'<span>{_esc(role["org"])} &middot; {_esc(role["role"])}</span>'
            f'</div><ul>{bullets}</ul></div>'
        )

    skills = "".join(
        f'<div class="skill"><b>{_esc(label)}</b>&nbsp; {_esc(", ".join(items))}</div>'
        for label, items in data["skills"].items()
    )

    edu = data["education"]
    replacements = {
        "__NAME__": _esc(data["name"]),
        "__TAGLINE__": "&nbsp; &middot; &nbsp;".join(_esc(t) for t in data["tagline"]),
        "__CONTACT__": " / ".join(_esc(c) for c in data["contact"]),
        "__SCHOOL__": _esc(edu["school"]),
        "__DEGREE__": _esc(edu["degree"]),
        "__GRAD__": _esc(edu["grad"]),
        "__COURSEWORK__": _esc(", ".join(edu["coursework"])) + ".",
        "__EXPERIENCE__": "".join(entries),
        "__LEADERSHIP__": "".join(leads),
        "__SKILLS__": skills,
    }
    for key, value in replacements.items():
        doc = doc.replace(key, value)
    return doc


def render(data: dict, out_pdf: Path) -> Path:
    """Render the content model to ``out_pdf``. Raises if Chromium is missing."""
    chrome = find_chrome()
    if not chrome:
        raise RuntimeError(
            "no Chromium/Chrome found for PDF rendering; set PLAYWRIGHT_BROWSERS_PATH "
            "or install Chrome")

    out_pdf = Path(out_pdf)
    out_pdf.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        # Fonts are referenced relatively, so they must sit beside the HTML.
        if FONT_DIR.exists():
            shutil.copytree(FONT_DIR, work / "fonts")
            log.debug("using fonts from %s", FONT_DIR)
        else:
            log.warning("resume/fonts/ is empty -- falling back to a substitute face. "
                        "Add HelveticaNeue*.ttf there for output identical to the original.")
        page = work / "resume.html"
        page.write_text(build_html(data), encoding="utf-8")

        subprocess.run(
            [chrome, "--headless", "--disable-gpu", "--no-sandbox",
             "--no-pdf-header-footer", "--run-all-compositor-stages-before-draw",
             "--virtual-time-budget=4000",
             f"--print-to-pdf={out_pdf}", page.as_uri()],
            check=True, capture_output=True, timeout=120,
        )
    if not out_pdf.exists() or out_pdf.stat().st_size < 1000:
        raise RuntimeError(f"Chromium produced no usable PDF at {out_pdf}")
    return out_pdf
