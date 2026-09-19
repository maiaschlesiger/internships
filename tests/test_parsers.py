"""Regression tests. Every case here corresponds to a bug found against live data."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from radar.classify import rule_verdict  # noqa: E402
from radar.models import Posting  # noqa: E402
from radar.sources import internlist  # noqa: E402
from radar.sources.jobright import parse_readme  # noqa: E402

CFG = yaml.safe_load((Path(__file__).resolve().parent.parent / "config.yaml").read_text())

# Real rows from jobright-ai/2026-Product-Management-Internship.
FIXTURE = """
| Company | Job Title | Location | Work Model | Date Posted |
| ----- | --------- |  --------- | ---- | ------- |
| **[General Motors](https://www.gm.com)** | **[2027 Summer Intern - Digital Product: Product Management (MBA)](https://jobright.ai/jobs/info/6aad7a803dbb1f8967cedb27?utm_campaign=1047&utm_source=git)** | Warren, MI, United States | Hybrid | Sep 18 |
| **[Gemini](https://gemini.com)** | **[Product Management Intern (Winter 2027)](https://jobright.ai/jobs/info/6aada1736956574eac8b6cc7?utm_campaign=1047&utm_source=git)** | New York, NY, United States | Hybrid | Sep 18 |
| ↳ | **[Product Management Intern (Winter 2027)](https://jobright.ai/jobs/info/6aad9e626956574eac8b6c20?utm_campaign=1047&utm_source=git)** | New York, NY, United States | Hybrid | Sep 18 |
| **[SAP](https://www.sap.com)** | **[SAP iXp Intern - Product Management, Event Technology & Digital Platforms [Newtown Square, PA]](https://jobright.ai/jobs/info/6a9af54513883870605953ef?utm_campaign=1047&utm_source=git)** | Newtown Square, Pennsylvania, United States | Hybrid | Sep 18 |
| **[Publicis Montréal](https://www.publicismontreal.ca)** | **[Product Manager Intern (Class of 2028)](https://jobright.ai/jobs/info/6aadb25d3d96632d741af5df?utm_campaign=1047&utm_source=git)** | Chicago, IL, United States | On Site | Sep 18 |
"""


def _p(title: str, company: str = "Acme") -> Posting:
    return Posting(job_id="x", title=title, company=company, source="t")


class TestJobrightParser(unittest.TestCase):
    def setUp(self):
        self.rows = parse_readme(FIXTURE, "test")

    def test_header_and_separator_rows_are_skipped(self):
        self.assertEqual(len(self.rows), 5)

    def test_brackets_in_link_label_do_not_drop_the_row(self):
        """A non-greedy [^]]* pattern silently lost this row entirely."""
        sap = [r for r in self.rows if r.company == "SAP"]
        self.assertEqual(len(sap), 1)
        self.assertIn("Newtown Square, PA", sap[0].title)
        self.assertEqual(sap[0].job_id, "6a9af54513883870605953ef")

    def test_continuation_marker_inherits_previous_company(self):
        gemini = [r for r in self.rows if "Winter 2027" in r.title]
        self.assertEqual(len(gemini), 2)
        self.assertTrue(all(r.company == "Gemini" for r in gemini))

    def test_job_ids_are_unique_and_well_formed(self):
        ids = [r.job_id for r in self.rows]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(len(i) == 24 for i in ids))


class TestClassifierRules(unittest.TestCase):
    def test_mba_only_roles_are_rejected(self):
        v = rule_verdict(_p("2027 Summer Intern - Digital Product: Product Management (MBA)"), CFG)
        self.assertFalse(v.include)
        self.assertIn("MBA", v.reason)

    def test_mba_substring_does_not_false_positive(self):
        """Word-boundary guard: 'mba' must not match inside another word."""
        v = rule_verdict(_p("Product Management Intern, Mumbai"), CFG)
        self.assertNotIn("MBA", v.reason)

    def test_wrong_term_is_rejected(self):
        self.assertFalse(rule_verdict(_p("Product Management Intern (Winter 2027)"), CFG).include)
        self.assertFalse(rule_verdict(_p("Design Intern (Fall 2026)"), CFG).include)

    def test_architecture_is_rejected(self):
        self.assertFalse(rule_verdict(_p("Student Architectural Internship / Summer 2027"), CFG).include)
        self.assertFalse(rule_verdict(_p("Landscape Architect Intern - Summer 2027"), CFG).include)

    def test_employer_industry_does_not_reject_a_relevant_role(self):
        """Matching the company name rejected analytics roles at construction firms."""
        v = rule_verdict(_p("Intern/Co Op - Business Analytics-Intelligence",
                            company="Turner Construction"), CFG)
        self.assertNotIn("excluded field", v.reason)
        self.assertTrue(v.include)

    def test_unmatched_category_is_dropped_even_when_no_term_is_named(self):
        """unspecified_action is a TERM policy; it must not rescue an off-topic role.

        Conflating the two let "ESG-Intern" and "R&D Intern - Biostatistics"
        through on a keyword-only run purely because they named no term.
        """
        for title in ("ESG-Intern", "R&D Intern - Biostatistics",
                      "2027 Future Talent Program - Statistical Programmer - Intern"):
            with self.subTest(title=title):
                self.assertFalse(rule_verdict(_p(title), CFG).include)

    def test_clear_match_is_accepted_on_rules_alone(self):
        v = rule_verdict(_p("Associate Product Manager (APM) Intern - Summer 2027"), CFG)
        self.assertTrue(v.include)
        self.assertEqual(v.confidence, "rule")
        self.assertEqual(v.category, "Product Management")

    def test_class_of_2028_counts_as_the_target_term(self):
        v = rule_verdict(_p("Product Manager Intern (Class of 2028)"), CFG)
        self.assertTrue(v.include)
        self.assertEqual(v.term, "Summer 2027")

    def test_unspecified_term_is_flagged_not_dropped(self):
        v = rule_verdict(_p("Product Management Intern"), CFG)
        self.assertEqual(v.confidence, "flagged")
        self.assertEqual(v.term, "Unspecified")


class TestInternListHelpers(unittest.TestCase):
    def test_ids_are_stable_across_processes(self):
        """hash() is salted per run; unstable ids would re-append every row hourly."""
        self.assertEqual(
            internlist.stable_id("pm", "https://example.com/a"),
            "internlist:pm:" + __import__("hashlib").sha1(b"https://example.com/a").hexdigest()[:16],
        )

    def test_relative_ages(self):
        now = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(internlist.parse_relative_age("3 hours ago", now).hour, 9)
        self.assertEqual(internlist.parse_relative_age("45 minutes ago", now).minute, 15)
        self.assertEqual(internlist.parse_relative_age("2 days ago", now).day, 17)
        self.assertIsNone(internlist.parse_relative_age("no date here", now))

    def test_malformed_html_returns_empty_not_raises(self):
        self.assertEqual(internlist._from_html("<table><tr><td>broken", "pm"), [])


if __name__ == "__main__":
    unittest.main()
