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
from radar import dedupe, enrich, jobdesc  # noqa: E402
from radar.sources import ghlist  # noqa: E402

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

    def test_reuses_jobright_native_id_so_sources_dedup(self):
        """intern-list is jobright's own front end; the same role appears in both."""
        native = "6aad7a803dbb1f8967cedb27"
        self.assertEqual(
            internlist.stable_id("pm", f"https://jobright.ai/jobs/info/{native}?utm=1"),
            native,
        )

    def test_unknown_feed_returns_empty_not_raises(self):
        self.assertEqual(internlist.fetch("not-a-feed"), [])

    def test_maps_listing_payload_regardless_of_nesting(self):
        payload = {"result": {"data": {"jobList": [
            {"jobTitle": "Product Management Intern", "companyName": "Acme",
             "jobLocation": "New York, NY", "jobId": "6aad7a803dbb1f8967cedb27",
             "applyLink": "https://acme.com/apply", "publishTimeDesc": "3 hours ago"}
        ]}}}
        rows = internlist.postings_from_payload(payload, "pm")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].company, "Acme")
        self.assertEqual(rows[0].job_id, "6aad7a803dbb1f8967cedb27")
        self.assertIsNotNone(rows[0].posted_at)

    def test_payload_without_listings_returns_empty(self):
        self.assertEqual(internlist.postings_from_payload({"totalCount": 30921}, "pm"), [])


class TestJobDescription(unittest.TestCase):
    def test_strips_script_style_and_chrome(self):
        html = ("<html><head><style>.a{color:red}</style></head><body>"
                "<nav>Home About</nav><script>var x=1;</script>"
                "<h2>Responsibilities</h2><li>Own the product roadmap</li>"
                "<li>Analyse results in SQL</li><footer>(c) 2026</footer></body></html>")
        text = jobdesc.html_to_text(html)
        self.assertIn("roadmap", text)
        self.assertIn("SQL", text)
        self.assertNotIn("var x", text)
        self.assertNotIn("color:red", text)
        self.assertNotIn("Home About", text)

    def test_recovers_description_from_js_only_page(self):
        """Workday/Ashby-style pages ship an empty DOM plus a JSON payload."""
        html = (r'<html><body></body><script>{"jobDescription":'
                r'"\u003cp\u003eBuild product specs with engineers on the roadmap. '
                r'Requires SQL and strong written communication. Figma preferred '
                r'for this internship role.\u003c/p\u003e"}</script></html>')
        text = jobdesc._from_embedded_json(html)
        self.assertIn("product specs", text)
        self.assertIn("SQL", text)
        self.assertNotIn("<p>", text, "HTML entities should be decoded and stripped")

    def test_embedded_threshold_does_not_discard_short_real_descriptions(self):
        """Two thresholds were both set at 200, silently dropping valid text."""
        body = "Own the roadmap and ship features. " * 4  # ~140 chars
        html = '<html><script>{"description":"%s"}</script></html>' % body
        self.assertTrue(jobdesc._from_embedded_json(html))

    def test_malformed_payload_returns_empty_not_raises(self):
        self.assertEqual(jobdesc._from_embedded_json(r'{"description":"\uZZZZ'), "")
        self.assertEqual(jobdesc.html_to_text("<div><p>unclosed"), "unclosed")


class TestGithubLists(unittest.TestCase):
    MD = (
        "| Company | Role | Location | Application/Link | Date Posted |\n"
        "| ------- | ---- | -------- | ---------------- | ----------- |\n"
        '| Vertiv | Product Management Intern \U0001F6C2 | Westerville, OH | '
        '<a href="https://egup.fa.us2.oraclecloud.com/job/20278933?utm_source=github-vansh">'
        '<img src="x.png"></a> | Aug 21 |\n'
        "| \u21b3 | Design Intern | Delaware, OH | "
        '<a href="https://example.com/job/2">a</a> | 0d |\n'
        "| Acme | Closed Role \U0001F512 | NY | <a href=\"https://example.com/3\">a</a> | 1d |\n"
        "| Acme | Grad Only Role \U0001F393 | NY | <a href=\"https://example.com/4\">a</a> | 1d |\n"
    )
    CFG = ghlist.REPOS["vanshb03/Summer2027-Internships"]

    def setUp(self):
        self.rows = ghlist.parse(self.MD, self.CFG, "test")

    def test_closed_and_advanced_degree_rows_are_dropped(self):
        titles = [r.title for r in self.rows]
        self.assertNotIn("Closed Role", titles)
        self.assertNotIn("Grad Only Role", titles)
        self.assertEqual(len(self.rows), 2)

    def test_continuation_inherits_company(self):
        self.assertEqual([r.company for r in self.rows], ["Vertiv", "Vertiv"])

    def test_convention_emoji_stripped_from_title(self):
        self.assertEqual(self.rows[0].title, "Product Management Intern")

    def test_apply_url_is_the_real_ats_with_tracking_removed(self):
        self.assertEqual(self.rows[0].portal_url,
                         "https://egup.fa.us2.oraclecloud.com/job/20278933")

    def test_relative_and_month_day_ages(self):
        now = datetime(2026, 9, 19, 12, tzinfo=timezone.utc)
        self.assertEqual(ghlist.parse_age("3d", now).day, 16)
        self.assertEqual(ghlist.parse_age("Aug 21", now).month, 8)
        self.assertIsNone(ghlist.parse_age("", now))

    def test_month_day_in_the_future_rolls_back_a_year(self):
        """A bare 'Dec 20' seen in September means last December, not next."""
        now = datetime(2026, 9, 19, tzinfo=timezone.utc)
        self.assertEqual(ghlist.parse_age("Dec 20", now).year, 2025)

    def test_html_table_format(self):
        html = ("<tr><td><a href='https://simplify.jobs/c/Waymo'>Waymo</a></td>"
                "<td>Product Manager Intern</td><td>Mountain View, CA</td>"
                "<td><a href='https://careers.withwaymo.com/jobs?gh_jid=821&ref=Simplify'>Apply</a></td>"
                "<td>0d</td></tr>")
        rows = ghlist.parse(html, ghlist.REPOS["SimplifyJobs/Summer2027-Internships"], "t")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].company, "Waymo")
        self.assertEqual(rows[0].portal_url, "https://careers.withwaymo.com/jobs?gh_jid=821")


class TestDedupe(unittest.TestCase):
    def _p(self, company, title, source, url="", loc="New York, NY", **kw):
        return Posting(job_id=f"{source}:{title}", title=title, company=company,
                       source=source, portal_url=url, location=loc, **kw)

    def test_same_ats_url_from_two_lists_collapses(self):
        url = "https://job-boards.greenhouse.io/acme/jobs/123"
        rows = dedupe.collapse([
            self._p("Acme", "Product Manager Intern", "listA", url + "?utm_source=a"),
            self._p("Acme", "Product Manager Intern", "listB", url + "?ref=b"),
        ])
        self.assertEqual(len(rows), 1)
        self.assertIn("listA", rows[0].source)
        self.assertIn("listB", rows[0].source)

    def test_redirector_urls_still_collapse_on_content(self):
        """dreamworkhq links to itself, so URL matching cannot catch these."""
        rows = dedupe.collapse([
            self._p("Acme", "Product Manager Intern - Summer 2027", "listA",
                    "https://www.dreamworkhq.com/job/abc"),
            self._p("Acme", "Product Manager Intern", "listB",
                    "https://jobright.ai/jobs/info/xyz"),
        ])
        self.assertEqual(len(rows), 1)

    def test_different_jobs_at_one_company_are_kept_apart(self):
        rows = dedupe.collapse([
            self._p("Acme", "Product Manager Intern", "a"),
            self._p("Acme", "Product Design Intern", "a"),
        ])
        self.assertEqual(len(rows), 2)

    def test_merge_keeps_the_more_precise_timestamp_and_real_portal(self):
        vague = self._p("Acme", "PM Intern", "listA", "https://jobright.ai/x",
                        posted_at=datetime(2026, 9, 18, tzinfo=timezone.utc),
                        posted_precision="day")
        precise = self._p("Acme", "PM Intern", "listB",
                          "https://job-boards.greenhouse.io/acme/jobs/9",
                          posted_at=datetime(2026, 9, 18, 14, 30, tzinfo=timezone.utc),
                          posted_precision="scraped")
        rows = dedupe.collapse([vague, precise])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].posted_precision, "scraped")
        self.assertEqual(rows[0].posted_at.hour, 14)
        self.assertIn("greenhouse", rows[0].portal_url)


class TestRequirementsExtraction(unittest.TestCase):
    """The Skill Requirements column must carry the employer's own words."""

    POSTING = ("About Acme\nWe build things.\n"
               "Responsibilities\nOwn the roadmap\n"
               "Basic Qualifications\n"
               "Currently pursuing a Bachelor's degree in Computer Science\n"
               "Experience with SQL and data analysis\n"
               "Preferred Qualifications\nFamiliarity with Figma\n"
               "Benefits\nFree lunch and unlimited PTO\n"
               "Equal Opportunity\nAcme is an equal opportunity employer.\n")

    def test_pulls_the_qualifications_section(self):
        out = jobdesc.extract_requirements(self.POSTING)
        self.assertIn("Bachelor", out)
        self.assertIn("SQL", out)

    def test_continues_through_a_second_requirements_heading(self):
        self.assertIn("Figma", jobdesc.extract_requirements(self.POSTING))

    def test_stops_before_benefits_and_eeo_boilerplate(self):
        out = jobdesc.extract_requirements(self.POSTING)
        self.assertNotIn("Free lunch", out)
        self.assertNotIn("equal opportunity", out.lower())

    def test_strips_bullet_markers(self):
        out = jobdesc.extract_requirements(
            "Requirements\n- Pursuing a BS\n\u2022 Proficiency in Excel\n1. Strong communication\n")
        self.assertTrue(out.startswith("Pursuing a BS"))
        self.assertNotIn("\u2022", out)
        self.assertIn("Excel", out)

    def test_no_section_returns_empty_so_the_caller_can_say_so(self):
        self.assertEqual(jobdesc.extract_requirements("Prose with no headings at all."), "")
        self.assertEqual(jobdesc.extract_requirements(""), "")

    def test_truncates_long_sections(self):
        long = "Qualifications\n" + "\n".join(
            f"Requirement {i} with supporting detail" for i in range(60))
        out = jobdesc.extract_requirements(long, max_chars=300)
        self.assertLessEqual(len(out), 310)
        self.assertTrue(out.endswith("..."))


class TestNotesExtraction(unittest.TestCase):
    POSTING = ("This is a 12-week program based in New York.\n"
               "The hourly pay range is $45.00 - $55.00 per hour.\n"
               "Applications close October 31, 2026.\n"
               "Minimum GPA of 3.2 required.\n"
               "Candidates must be authorized to work in the United States; "
               "we are unable to sponsor visas.\n"
               "High performers may receive a return offer.\n"
               "Relocation assistance is available.\n")

    def test_finds_pay_deadline_duration_and_gpa(self):
        out = jobdesc.extract_notes(self.POSTING)
        self.assertIn("$45.00", out)
        self.assertIn("October 31, 2026", out)
        self.assertIn("12-week", out)
        self.assertIn("3.2", out)

    def test_flags_sponsorship_and_perks(self):
        out = jobdesc.extract_notes(self.POSTING)
        self.assertIn("No visa sponsorship", out)
        self.assertIn("Relocation or housing support", out)
        self.assertIn("Return offer possible", out)

    def test_merges_list_flags_without_duplicating(self):
        out = jobdesc.extract_notes(self.POSTING, extra=["No visa sponsorship"])
        self.assertEqual(out.count("No visa sponsorship"), 1)

    def test_flags_alone_survive_with_no_description(self):
        self.assertEqual(jobdesc.extract_notes("", extra=["US citizenship required"]),
                         "US citizenship required")

    def test_posting_with_nothing_notable_returns_empty(self):
        self.assertEqual(jobdesc.extract_notes("We are a company that does things."), "")
        self.assertEqual(jobdesc.extract_notes(""), "")

    def test_output_is_capped(self):
        noisy = self.POSTING * 6
        self.assertLessEqual(len(jobdesc.extract_notes(noisy, max_chars=120)), 120)


class TestListEligibilityFlags(unittest.TestCase):
    """The lists' sponsorship glyphs were stripped and lost; keep them as notes."""

    def test_no_sponsorship_glyph_becomes_a_note(self):
        md = ("| Company | Role | Location | Link | Date Posted |\n"
              "| --- | --- | --- | --- | --- |\n"
              "| Acme | PM Intern \U0001F6C2 | NY | <a href=\"https://x.com/1\">a</a> | 0d |\n")
        rows = ghlist.parse(md, ghlist.REPOS["vanshb03/Summer2027-Internships"], "t")
        self.assertEqual(rows[0].notes, "No visa sponsorship")
        self.assertNotIn("\U0001F6C2", rows[0].title)

    def test_citizenship_glyph_becomes_a_note(self):
        md = ("| Company | Role | Location | Link | Date Posted |\n"
              "| --- | --- | --- | --- | --- |\n"
              "| Acme | PM Intern \U0001F1FA\U0001F1F8 | NY | <a href=\"https://x.com/1\">a</a> | 0d |\n")
        rows = ghlist.parse(md, ghlist.REPOS["vanshb03/Summer2027-Internships"], "t")
        self.assertEqual(rows[0].notes, "US citizenship required")

    def test_unflagged_row_has_no_notes(self):
        md = ("| Company | Role | Location | Link | Date Posted |\n"
              "| --- | --- | --- | --- | --- |\n"
              "| Acme | PM Intern | NY | <a href=\"https://x.com/1\">a</a> | 0d |\n")
        rows = ghlist.parse(md, ghlist.REPOS["vanshb03/Summer2027-Internships"], "t")
        self.assertEqual(rows[0].notes, "")


class TestContactEmailExtraction(unittest.TestCase):
    def test_prefers_a_hiring_inbox_over_a_generic_one(self):
        html = "<p>press@acme.com</p><p>universityrecruiting@acme.com</p>"
        self.assertEqual(jobdesc.extract_contact_emails(html),
                         "universityrecruiting@acme.com")

    def test_reads_a_mailto_link(self):
        self.assertEqual(
            jobdesc.extract_contact_emails('<a href="mailto:Campus@Acme.com">apply</a>'),
            "campus@acme.com")

    def test_discards_the_ats_vendors_own_addresses(self):
        html = "<p>noreply@greenhouse.io support@lever.co</p><p>campus@acme.com</p>"
        self.assertEqual(jobdesc.extract_contact_emails(html), "campus@acme.com")

    def test_discards_legal_and_automated_inboxes(self):
        self.assertEqual(
            jobdesc.extract_contact_emails("<p>privacy@acme.com legal@acme.com</p>"), "")

    def test_ignores_asset_filenames_that_look_like_addresses(self):
        self.assertEqual(jobdesc.extract_contact_emails('<img src="logo@2x.png">'), "")

    def test_falls_back_to_a_plausible_personal_address(self):
        self.assertEqual(
            jobdesc.extract_contact_emails("<p>Contact jane.doe@acme.com</p>"),
            "jane.doe@acme.com")

    def test_nothing_found_returns_empty(self):
        self.assertEqual(jobdesc.extract_contact_emails("<p>no address here</p>"), "")
        self.assertEqual(jobdesc.extract_contact_emails(""), "")


class TestSkillsFallback(unittest.TestCase):
    def test_scraped_requirements_are_used_verbatim(self):
        p = Posting(job_id="1", title="PM Intern", company="Acme", source="s",
                    category="Product Management")
        enrich.apply_fallback(p, requirements="Pursuing a BS; SQL; Figma")
        self.assertEqual(p.skills, ["Pursuing a BS; SQL; Figma"])

    def test_generic_note_only_when_the_page_could_not_be_read(self):
        p = Posting(job_id="1", title="PM Intern", company="Acme", source="s",
                    category="Product Management")
        enrich.apply_fallback(p, requirements="")
        self.assertEqual(p.skills, ["See posting"])


class TestStoredRowDedup(unittest.TestCase):
    """A job already in the database must not be re-added from another list."""

    def test_fingerprint_matches_between_a_stored_row_and_a_fresh_scrape(self):
        stored = Posting(job_id="6aad7a803dbb1f8967cedb27",
                         title="Product Management Intern", company="Vertiv",
                         source="jobright", location="Westerville, OH")
        # Same job, later seen on a different list: different id, different
        # location suffix, term named in the title.
        scraped = Posting(job_id="gh:abc123", title="Product Management Intern",
                          company="Vertiv", source="SimplifyJobs",
                          location="Westerville, OH, United States")
        self.assertNotEqual(stored.job_id, scraped.job_id)
        self.assertEqual(dedupe.fingerprint(stored), dedupe.fingerprint(scraped))

    def test_different_roles_do_not_collide(self):
        a = Posting(job_id="1", title="Product Management Intern", company="Vertiv",
                    source="x", location="Westerville, OH")
        b = Posting(job_id="2", title="Product Design Intern", company="Vertiv",
                    source="x", location="Westerville, OH")
        self.assertNotEqual(dedupe.fingerprint(a), dedupe.fingerprint(b))


class TestPostedAtScraping(unittest.TestCase):
    def test_schema_org_date_with_time_is_marked_scraped(self):
        html = '<script type="application/ld+json">{"@type":"JobPosting",' \
               '"datePosted":"2026-09-18T14:30:00Z"}</script>'
        when, precision = jobdesc.extract_posted_at(html)
        self.assertEqual(precision, "scraped")
        self.assertEqual(when.hour, 14)

    def test_bare_calendar_date_is_marked_day_not_scraped(self):
        """Most boards publish only a date; that must not outrank a better estimate."""
        html = '{"datePosted":"2026-09-18"}'
        when, precision = jobdesc.extract_posted_at(html)
        self.assertEqual(precision, "day")
        self.assertEqual(when.day, 18)

    def test_implausible_dates_are_ignored(self):
        self.assertEqual(jobdesc.extract_posted_at('{"datePosted":"1998-01-01"}')[0], None)

    def test_no_date_returns_none(self):
        self.assertEqual(jobdesc.extract_posted_at("<html>nothing</html>"),
                         (None, "unknown"))


if __name__ == "__main__":
    unittest.main()
