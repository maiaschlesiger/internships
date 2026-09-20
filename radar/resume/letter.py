"""Render a cover letter on the same letterhead as the resume.

Shares the resume's header block and fonts so a submitted pair reads as one
document set rather than two unrelated files.
"""

from __future__ import annotations

import html
import shutil
import tempfile
import subprocess
from pathlib import Path
from typing import List

from .render import find_chrome, FONT_DIR

TEMPLATE = Path(__file__).with_name("letter_template.html")


def render_letter(data: dict, paragraphs: List[str], meta: List[str],
                  signoff: str, out_pdf: Path) -> Path:
    chrome = find_chrome()
    if not chrome:
        raise RuntimeError("no Chromium/Chrome found for PDF rendering")
    esc = lambda t: html.escape(str(t), quote=False)

    body = "".join(
        f'<p class="{"first" if i == 0 else ""}">{esc(p)}</p>'
        for i, p in enumerate(paragraphs)
    )
    doc = TEMPLATE.read_text(encoding="utf-8")
    for key, value in {
        "__NAME__": esc(data["name"]),
        "__TAGLINE__": "&nbsp; &middot; &nbsp;".join(esc(t) for t in data["tagline"]),
        "__CONTACT__": " / ".join(esc(c) for c in data["contact"]),
        "__META__": "".join(f"<div>{esc(m)}</div>" for m in meta),
        "__BODY__": body,
        "__SIGNOFF__": esc(signoff),
    }.items():
        doc = doc.replace(key, value)

    out_pdf = Path(out_pdf)
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        if FONT_DIR.exists():
            shutil.copytree(FONT_DIR, work / "fonts")
        page = work / "letter.html"
        page.write_text(doc, encoding="utf-8")
        subprocess.run(
            [chrome, "--headless", "--disable-gpu", "--no-sandbox",
             "--no-pdf-header-footer", "--virtual-time-budget=4000",
             f"--print-to-pdf={out_pdf}", page.as_uri()],
            check=True, capture_output=True, timeout=120)
    return out_pdf
