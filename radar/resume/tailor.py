"""Produce a job-specific variant of the resume content model.

Five zones may be rewritten, and nothing else: the tagline, relevant coursework,
experience bullets, leadership bullets, and the skills lines. Section order, the
set of roles, the number of bullets under each role, dates, employers and the
recognition line are fixed.

Those limits are enforced after the model answers, not merely requested of it --
``validate`` rejects a variant that changes the shape, introduces a course or
skill that is not on the allowed list, or writes a bullet long enough to push the
page to two. A rejected variant falls back to the base resume rather than
producing something misshapen.

On truthfulness: the model reweights and rewords what the base resume already
claims. It is instructed not to invent employers, projects, metrics, tools or
responsibilities, because a resume that overstates gets found out in the
interview it wins.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-5"
# A bullet much longer than the original reflows the page; much shorter leaves a
# visible gap. Keep every rewrite close to the length it replaces.
LENGTH_TOLERANCE = 0.18

PROMPT = """You are tailoring one candidate's resume to one internship posting.

THE CANDIDATE (everything true about her is here; you know nothing else):
%(base)s

THE POSTING:
Title: %(title)s
Company: %(company)s
Category: %(category)s
Requirements / description:
%(description)s

Resume keywords this posting suggests: %(keywords)s

WHAT YOU MAY CHANGE — nothing else exists to change:
1. "tagline" — exactly 3 short positioning phrases, uppercase, each 1-3 words.
2. "coursework" — 3 courses, chosen ONLY from courses_available.
3. "experience" bullets — same roles in the same order, same number of bullets
   for each role. Reword only.
4. "leadership" bullets — same rule.
5. "skills" — for Design, Technical and "AI & Research" you may reorder items and
   drop items, and you may add items that appear in skills_available for that
   line. Never invent one. Return "Recognition" exactly as given.

HARD RULES:
- Every claim must be supported by the base resume. Do not invent employers,
  projects, tools, technologies, metrics, team sizes or responsibilities. If the
  posting wants something she has not done, do not pretend she has.
- Keep each rewritten bullet within roughly %(tol)d%% of the length of the bullet
  it replaces. The resume must stay on one page.
- Work the posting's own vocabulary in where it is genuinely accurate. A keyword
  that would be a lie is not worth having.
- Keep her voice: plain, concrete, specific. No "leveraged", "spearheaded",
  "utilized", "passionate", or filler adjectives.
- Preserve names of real things exactly: WANDA, Little Wanda, PPAC, MedExplain,
  Figma, Weill Cornell Medical School, DoD / ORISE, Hack4Impact.

Return ONLY a JSON object, no prose:
{"tagline": ["...","...","..."],
 "coursework": ["...","...","..."],
 "experience": [{"org":"<exact org name>","bullets":["...","..."]}, ...],
 "leadership": [{"org":"<exact org name>","bullets":["..."]}, ...],
 "skills": {"Design":[...], "Technical":[...], "AI & Research":[...], "Recognition":[...]}}
"""


def _base_summary(base: dict) -> str:
    lines = [f'Tagline now: {" / ".join(base["tagline"])}',
             f'Courses available: {", ".join(base["courses_available"])}',
             "", "EXPERIENCE:"]
    for role in base["experience"]:
        lines.append(f'- {role["org"]} | {role["role"]} | {role["when"]}')
        for b in role["bullets"]:
            lines.append(f'    * ({len(b)} chars) {b}')
    lines.append("")
    lines.append("LEADERSHIP:")
    for role in base["leadership"]:
        lines.append(f'- {role["org"]} | {role["role"]}')
        for b in role["bullets"]:
            lines.append(f'    * ({len(b)} chars) {b}')
    lines.append("")
    lines.append("SKILLS (current):")
    for label, items in base["skills"].items():
        lines.append(f'- {label}: {", ".join(items)}')
    lines.append("")
    lines.append("SKILLS AVAILABLE (the only additions permitted):")
    for label, items in base.get("skills_available", {}).items():
        lines.append(f'- {label}: {", ".join(items)}')
    return "\n".join(lines)


def validate(base: dict, variant: dict) -> Tuple[bool, List[str]]:
    """Check a variant keeps the resume's shape and claims. Returns (ok, problems)."""
    problems: List[str] = []

    tagline = variant.get("tagline") or []
    if len(tagline) != 3:
        problems.append(f"tagline has {len(tagline)} phrases, expected 3")

    allowed_courses = {c.lower() for c in base.get("courses_available", [])}
    courses = variant.get("coursework") or []
    if len(courses) != len(base["education"]["coursework"]):
        problems.append(f"coursework has {len(courses)} items, "
                        f"expected {len(base['education']['coursework'])}")
    for course in courses:
        if course.lower() not in allowed_courses:
            problems.append(f"course not in courses_available: {course!r}")

    for key in ("experience", "leadership"):
        base_roles = base[key]
        new_roles = variant.get(key) or []
        if len(new_roles) != len(base_roles):
            problems.append(f"{key} has {len(new_roles)} roles, expected {len(base_roles)}")
            continue
        for old, new in zip(base_roles, new_roles):
            if new.get("org", "").strip().lower() != old["org"].strip().lower():
                problems.append(f"{key} role renamed: {old['org']!r} -> {new.get('org')!r}")
            new_bullets = new.get("bullets") or []
            if len(new_bullets) != len(old["bullets"]):
                problems.append(f"{old['org']}: {len(new_bullets)} bullets, "
                                f"expected {len(old['bullets'])}")
                continue
            for ob, nb in zip(old["bullets"], new_bullets):
                if not nb.strip():
                    problems.append(f"{old['org']}: empty bullet")
                elif abs(len(nb) - len(ob)) / max(len(ob), 1) > LENGTH_TOLERANCE:
                    problems.append(
                        f"{old['org']}: bullet length {len(nb)} vs {len(ob)} "
                        f"(outside {int(LENGTH_TOLERANCE*100)}%)")

    skills = variant.get("skills") or {}
    if set(skills) != set(base["skills"]):
        problems.append(f"skills lines changed: {sorted(skills)} vs {sorted(base['skills'])}")
    else:
        available = base.get("skills_available", {})
        for label, items in skills.items():
            if label == "Recognition":
                if items != base["skills"]["Recognition"]:
                    problems.append("Recognition line was altered")
                continue
            allowed = {s.lower() for s in available.get(label, base["skills"][label])}
            allowed |= {s.lower() for s in base["skills"][label]}
            for item in items:
                if item.lower() not in allowed:
                    problems.append(f"{label}: {item!r} is not in skills_available")
            if not items:
                problems.append(f"{label}: line is empty")

    return (not problems), problems


def apply_variant(base: dict, variant: dict) -> dict:
    """Merge a validated variant onto the base, leaving structure untouched."""
    out = copy.deepcopy(base)
    out["tagline"] = [t.upper() for t in variant["tagline"]]
    out["education"]["coursework"] = variant["coursework"]
    for key in ("experience", "leadership"):
        for role, new in zip(out[key], variant[key]):
            role["bullets"] = new["bullets"]
    out["skills"] = {label: variant["skills"][label] for label in base["skills"]}
    return out


def tailor(base: dict, posting: dict, api_key: Optional[str] = None) -> Tuple[dict, str]:
    """Return ``(content_model, note)``. Falls back to the base resume on any problem."""
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return base, "not tailored (ANTHROPIC_API_KEY unset)"
    try:
        import anthropic
    except ImportError:
        return base, "not tailored (anthropic SDK missing)"

    prompt = PROMPT % {
        "base": _base_summary(base),
        "title": posting.get("title", ""),
        "company": posting.get("company", ""),
        "category": posting.get("category", ""),
        "description": (posting.get("description") or posting.get("skills") or "")[:6000],
        "keywords": ", ".join(posting.get("keywords") or []),
        "tol": int(LENGTH_TOLERANCE * 100),
    }
    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(model=MODEL, max_tokens=4000,
                                      messages=[{"role": "user", "content": prompt}])
        text = resp.content[0].text
    except Exception as exc:  # noqa: BLE001
        log.warning("tailoring call failed (%s)", exc)
        return base, f"not tailored (API error: {type(exc).__name__})"

    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return base, "not tailored (no JSON returned)"
    try:
        variant = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        return base, f"not tailored (bad JSON: {exc.msg})"

    ok, problems = validate(base, variant)
    if not ok:
        log.warning("variant rejected for %s at %s: %s",
                    posting.get("title"), posting.get("company"), "; ".join(problems[:4]))
        return base, "not tailored (variant failed validation: " + problems[0] + ")"

    return apply_variant(base, variant), "tailored"
