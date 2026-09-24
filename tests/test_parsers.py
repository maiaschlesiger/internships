"""Regression tests. Every case here corresponds to a bug found against live data."""

from __future__ import annotations

import sys
import unittest
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402
import yaml  # noqa: E402

from radar.classify import rule_verdict  # noqa: E402
from radar.models import Posting  # noqa: E402
from radar.sources import internlist, modelsite  # noqa: E402
from radar.sources.jobright import parse_readme  # noqa: E402
from radar import dedupe, enrich, jobdesc, notion_sink  # noqa: E402
from radar.sources import ghlist  # noqa: E402
from radar.resume import tailor as rtailor  # noqa: E402

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


class TestInternListPayload(unittest.TestCase):
    """The embed serves __NEXT_DATA__ with listings at props.pageProps.initialJobs."""

    PAYLOAD = {"props": {"pageProps": {"initialJobs": [{
        "id": "6aada2723dbb1f8967ceeb25",
        "title": "Category Management Intern",
        "company": "Gordon Food Service",
        "location": "Wyoming, MI",
        "applyUrl": "https://jobright.ai/jobs/info/6aada2723dbb1f8967ceeb25?utm_source=1099",
        "postedDate": 1789746689000,
        "workModel": "On Site",
        "salary": "$25-$25/hr",
        "graduateTime": "2027-August / 2028-July",
        "h1bSponsored": "No",
        "companySize": "10000+",
        "qualifications": "1. Pursuing a Bachelor's degree 2. Proficiency in Excel",
    }]}}}

    def setUp(self):
        self.row = internlist.postings_from_payload(self.PAYLOAD, "pm")[0]

    def test_uses_jobrights_own_id_so_it_dedups_with_the_repos(self):
        self.assertEqual(self.row.job_id, "6aada2723dbb1f8967ceeb25")

    def test_epoch_millis_become_an_exact_timestamp(self):
        """This feed is the only source publishing a real time of day."""
        self.assertEqual(self.row.posted_precision, "scraped")
        self.assertEqual(self.row.posted_at.year, 2026)
        self.assertIsNotNone(self.row.posted_at.tzinfo)

    def test_qualifications_are_flattened_into_skills(self):
        self.assertIn("Bachelor", self.row.skills[0])
        self.assertIn("Excel", self.row.skills[0])
        self.assertNotIn("1.", self.row.skills[0])

    def test_notes_carry_pay_graduation_window_and_sponsorship(self):
        self.assertIn("$25-$25/hr", self.row.notes)
        self.assertIn("2028-July", self.row.notes)
        self.assertIn("No visa sponsorship", self.row.notes)

    def test_applyurl_is_kept_as_the_listing_for_redirect_resolution(self):
        """It points at jobright, so resolve_portal must follow it."""
        self.assertIn("jobright.ai", self.row.listing_url)

    def test_payload_without_jobs_returns_empty(self):
        self.assertEqual(internlist.postings_from_payload({"props": {}}, "pm"), [])
        self.assertEqual(internlist.postings_from_payload({"totalCount": 30921}, "pm"), [])


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

    def test_source_qualifications_survive_the_enrich_fallback(self):
        p = Posting(job_id="1", title="PM Intern", company="Acme", source="s",
                    category="Product Management", skills=["Pursuing a BS; Excel"])
        enrich.apply_fallback(p, requirements="")
        self.assertEqual(p.skills, ["Pursuing a BS; Excel"])


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


class TestTitleLink(unittest.TestCase):
    """The title links to where the listing was found, not where it resolved to."""

    def _props(self, p):
        import radar.notion_sink as ns
        captured = {}
        client = ns.Notion.__new__(ns.Notion)
        client._call = lambda method, path, **kw: captured.update(kw.get("json", {})) or {}
        ns.Notion.add(client, "db", p)
        return captured["properties"]

    def test_title_links_to_the_source_listing_not_the_resolved_portal(self):
        p = Posting(job_id="1", title="PM Intern", company="Acme", source="s",
                    listing_url="https://jobright.ai/jobs/info/abc",
                    portal_url="https://job-boards.greenhouse.io/acme/jobs/1")
        link = self._props(p)["Title"]["title"][0]["text"]["link"]["url"]
        self.assertEqual(link, "https://jobright.ai/jobs/info/abc")

    def test_falls_back_to_the_portal_when_no_listing_url(self):
        p = Posting(job_id="1", title="PM Intern", company="Acme", source="s",
                    portal_url="https://job-boards.greenhouse.io/acme/jobs/1")
        link = self._props(p)["Title"]["title"][0]["text"]["link"]["url"]
        self.assertIn("greenhouse", link)

    def test_no_url_leaves_a_plain_title(self):
        p = Posting(job_id="1", title="PM Intern", company="Acme", source="s")
        self.assertNotIn("link", self._props(p)["Title"]["title"][0]["text"])

    def test_application_portal_still_holds_the_resolved_url(self):
        p = Posting(job_id="1", title="PM Intern", company="Acme", source="s",
                    listing_url="https://jobright.ai/jobs/info/abc",
                    portal_url="https://job-boards.greenhouse.io/acme/jobs/1")
        self.assertEqual(self._props(p)["Application Portal"]["url"],
                         "https://job-boards.greenhouse.io/acme/jobs/1")


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


class TestResumeTailoringGuardrails(unittest.TestCase):
    """The structure rules are enforced after the model answers, not just asked for."""

    @classmethod
    def setUpClass(cls):
        import yaml as _yaml
        path = Path(__file__).resolve().parent.parent / "resume" / "base.yaml"
        cls.base = _yaml.safe_load(path.read_text())

    def _variant(self, **over):
        import copy
        b = self.base
        v = {"tagline": list(b["tagline"]),
             "coursework": list(b["education"]["coursework"]),
             "experience": [{"org": r["org"], "bullets": list(r["bullets"])}
                            for r in b["experience"]],
             "leadership": [{"org": r["org"], "bullets": list(r["bullets"])}
                            for r in b["leadership"]],
             "skills": copy.deepcopy(b["skills"])}
        v.update(over)
        return v

    def test_unchanged_variant_is_accepted(self):
        ok, problems = rtailor.validate(self.base, self._variant())
        self.assertTrue(ok, problems)

    def test_rejects_adding_a_bullet(self):
        v = self._variant()
        v["experience"][0]["bullets"].append("An extra invented achievement.")
        self.assertFalse(rtailor.validate(self.base, v)[0])

    def test_rejects_dropping_a_role(self):
        v = self._variant()
        v["experience"] = v["experience"][:-1]
        self.assertFalse(rtailor.validate(self.base, v)[0])

    def test_rejects_renaming_an_employer(self):
        v = self._variant()
        v["experience"][0]["org"] = "Google"
        self.assertFalse(rtailor.validate(self.base, v)[0])

    def test_rejects_a_course_she_has_not_taken(self):
        v = self._variant(coursework=["Quantum Computing", "Artificial Intelligence",
                                      "Game Development"])
        self.assertFalse(rtailor.validate(self.base, v)[0])

    def test_rejects_a_skill_she_does_not_have(self):
        v = self._variant()
        v["skills"]["Technical"] = v["skills"]["Technical"] + ["Kubernetes"]
        self.assertFalse(rtailor.validate(self.base, v)[0])

    def test_rejects_altering_the_recognition_line(self):
        v = self._variant()
        v["skills"]["Recognition"] = ["Nobel Prize"]
        self.assertFalse(rtailor.validate(self.base, v)[0])

    def test_rejects_bullets_that_would_reflow_the_page(self):
        v = self._variant()
        v["experience"][0]["bullets"][0] = "Led product."
        self.assertFalse(rtailor.validate(self.base, v)[0])
        v2 = self._variant()
        v2["experience"][0]["bullets"][0] = self.base["experience"][0]["bullets"][0] * 2
        self.assertFalse(rtailor.validate(self.base, v2)[0])

    def test_accepts_reordering_and_permitted_additions(self):
        v = self._variant()
        v["skills"]["Design"] = ["Figma", "prototyping", "wireframing", "design systems"]
        v["coursework"] = ["Introduction to Algorithms", "Discrete Structures",
                           "Artificial Intelligence"]
        ok, problems = rtailor.validate(self.base, v)
        self.assertTrue(ok, problems)

    def test_apply_variant_leaves_structure_untouched(self):
        v = self._variant(tagline=["product management", "ux research", "ai"])
        out = rtailor.apply_variant(self.base, v)
        self.assertEqual(len(out["experience"]), len(self.base["experience"]))
        self.assertEqual([r["when"] for r in out["experience"]],
                         [r["when"] for r in self.base["experience"]])
        self.assertEqual(out["tagline"], ["PRODUCT MANAGEMENT", "UX RESEARCH", "AI"])

    def test_no_api_key_falls_back_to_the_base_resume(self):
        import os
        saved = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            content, note = rtailor.tailor(self.base, {"title": "PM Intern"})
            self.assertEqual(content, self.base)
            self.assertIn("not tailored", note)
        finally:
            if saved:
                os.environ["ANTHROPIC_API_KEY"] = saved


JOBRIGHT_NEXT_DATA = """<!DOCTYPE html><html><head><title>PM Intern | Jobright.ai</title></head>
<body><div id="__next">...</div>
<script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"job":{
"id":"6a5908d763a8f619507bfd68","title":"Product Management Intern (Summer 2027)",
"company":"Databricks","applyUrl":"https://jobright.ai/jobs/info/6a5908d763a8f619507bfd68?utm_source=1099",
"originalUrl":"https://www.databricks.com/company/careers/product/pm-intern-summer-2027-6883068002",
"socialShare":"https://twitter.com/intent/tweet?url=x"}}}}</script></body></html>"""

JOBRIGHT_BUTTON = """<html><body>
<a href="/jobs/info/abc">Back</a>
<a class="btn" href="https://boards.greenhouse.io/acme/jobs/44110?src=jobright">
  <span>Original Job Post</span></a>
<a href="https://www.facebook.com/sharer?u=x">Share</a>
</body></html>"""

JOBRIGHT_ATS_ONLY = """<html><body><div>Apply below</div>
<a href="https://usaa.wd1.myworkdayjobs.com/en-US/usaajobs/job/San-Antonio/PM_R0111">Apply</a>
</body></html>"""

JOBRIGHT_SELF_REFERENTIAL = """<html><body>
<script>{"applyUrl":"https://jobright.ai/jobs/info/6aab75e44be87a72913a46c8?utm_source=1099"}</script>
<a href="https://jobright.ai/pricing">Upgrade</a></body></html>"""


class _FakeResponse:
    def __init__(self, url, text="", ok=True):
        self.url, self.text, self.ok = url, text, ok


class _FakeSession:
    """Serves a canned response per URL and records what was requested."""

    def __init__(self, pages):
        self.pages, self.asked = pages, []
        self.headers = {}

    def get(self, url, **kwargs):
        self.asked.append(url)
        return self.pages.get(url) or _FakeResponse(url, "", ok=False)


class TestOriginalPostLink(unittest.TestCase):
    """jobright serves its own page; the employer's posting is behind a button."""

    def test_embedded_original_url_beats_a_self_referential_apply_url(self):
        self.assertEqual(
            enrich.original_post_link(JOBRIGHT_NEXT_DATA),
            "https://www.databricks.com/company/careers/product/"
            "pm-intern-summer-2027-6883068002")

    def test_the_original_job_post_button_is_read(self):
        self.assertEqual(enrich.original_post_link(JOBRIGHT_BUTTON),
                         "https://boards.greenhouse.io/acme/jobs/44110?src=jobright")

    def test_an_ats_link_is_the_last_resort(self):
        self.assertEqual(
            enrich.original_post_link(JOBRIGHT_ATS_ONLY),
            "https://usaa.wd1.myworkdayjobs.com/en-US/usaajobs/job/San-Antonio/PM_R0111")

    def test_a_page_linking_only_to_itself_yields_nothing(self):
        # Must be "", so the caller keeps the listing URL rather than
        # substituting something wrong.
        self.assertEqual(enrich.original_post_link(JOBRIGHT_SELF_REFERENTIAL), "")

    def test_share_links_are_never_mistaken_for_the_posting(self):
        self.assertNotIn("twitter", enrich.original_post_link(JOBRIGHT_NEXT_DATA))
        self.assertNotIn("facebook", enrich.original_post_link(JOBRIGHT_BUTTON))

    def test_json_escaped_urls_are_unescaped(self):
        html = r'{"originalJobUrl":"https:\/\/jobs.lever.co\/acme\/1?a=1\u0026b=2"}'
        self.assertEqual(enrich.original_post_link(html),
                         "https://jobs.lever.co/acme/1?a=1&b=2")

    def test_empty_and_barren_pages_are_safe(self):
        self.assertEqual(enrich.original_post_link(""), "")
        self.assertEqual(enrich.original_post_link("<html><body>hi</body></html>"), "")


class TestFollowToOriginal(unittest.TestCase):
    def test_reads_the_page_when_the_aggregator_does_not_redirect(self):
        listing = "https://jobright.ai/jobs/info/6a5908d763a8f619507bfd68"
        session = _FakeSession({listing: _FakeResponse(listing, JOBRIGHT_NEXT_DATA)})
        self.assertTrue(
            enrich.follow_to_original(listing, session).startswith("https://www.databricks.com"))

    def test_keeps_a_redirect_that_already_landed_on_the_employer(self):
        listing = "https://simplify.jobs/p/xyz"
        landed = "https://boards.greenhouse.io/acme/jobs/9"
        session = _FakeSession({listing: _FakeResponse(landed, "<html></html>")})
        self.assertEqual(enrich.follow_to_original(listing, session), landed)

    def test_returns_the_listing_when_there_is_nothing_better(self):
        listing = "https://jobright.ai/jobs/info/6aab75e44be87a72913a46c8"
        session = _FakeSession({listing: _FakeResponse(listing, JOBRIGHT_SELF_REFERENTIAL)})
        self.assertEqual(enrich.follow_to_original(listing, session), listing)

    def test_a_dead_link_returns_the_input_rather_than_raising(self):
        class _Boom:
            def get(self, url, **kwargs):
                raise requests.RequestException("no route")

        url = "https://jobright.ai/jobs/info/dead"
        self.assertEqual(enrich.follow_to_original(url, _Boom()), url)


class TestResolvePortalUsesTheOriginal(unittest.TestCase):
    def test_a_jobright_listing_ends_up_pointing_at_the_employer(self):
        listing = "https://jobright.ai/jobs/info/6a5908d763a8f619507bfd68"
        p = Posting(job_id="x", title="PM Intern", company="Databricks",
                    source="jobright", listing_url=listing)
        session = _FakeSession({listing: _FakeResponse(listing, JOBRIGHT_NEXT_DATA)})
        with mock.patch.object(enrich.requests, "Session", return_value=session):
            enrich.resolve_portal([p], workers=1)
        self.assertTrue(p.portal_url.startswith("https://www.databricks.com"))
        self.assertEqual(p.listing_url, listing)  # the Title link is untouched

    def test_an_unresolvable_listing_keeps_its_link(self):
        listing = "https://jobright.ai/jobs/info/6aab75e44be87a72913a46c8"
        p = Posting(job_id="y", title="APM Intern", company="Visa",
                    source="jobright", listing_url=listing)
        session = _FakeSession({listing: _FakeResponse(listing, JOBRIGHT_SELF_REFERENTIAL)})
        with mock.patch.object(enrich.requests, "Session", return_value=session):
            enrich.resolve_portal([p], workers=1)
        self.assertEqual(p.portal_url, listing)



class TestSelectOptionSafety(unittest.TestCase):
    """Notion 400s on a select option containing a comma, failing the page write."""

    def test_a_merged_source_list_loses_its_commas(self):
        merged = ("intern-list:pm, dreamworkhq/Tech-Internships-2027, "
                  "SimplifyJobs/Summer2027-Internships")
        out = notion_sink._select(merged)
        self.assertNotIn(",", out)
        self.assertIn("intern-list:pm", out)
        self.assertIn("SimplifyJobs", out)

    def test_an_ordinary_value_is_untouched(self):
        self.assertEqual(notion_sink._select("Product Management"), "Product Management")

    def test_the_option_stays_within_notion_s_length_limit(self):
        self.assertLessEqual(len(notion_sink._select("x" * 400)), 100)

    def test_empty_and_none_are_safe(self):
        self.assertEqual(notion_sink._select(""), "")
        self.assertEqual(notion_sink._select(None), "")

    def test_merging_two_sources_produces_no_comma(self):
        a = Posting(job_id="1", title="PM Intern", company="Acme", source="jobright",
                    location="Austin, TX")
        b = Posting(job_id="1", title="PM Intern", company="Acme",
                    source="SimplifyJobs/Summer2027-Internships", location="Austin, TX")
        merged = dedupe.collapse([a, b])[0]
        self.assertNotIn(",", merged.source)
        self.assertIn("jobright", merged.source)
        self.assertIn("SimplifyJobs", merged.source)



class _RecordingNotion(notion_sink.Notion):
    """A Notion client that records calls instead of making them."""

    def __init__(self):  # noqa: D107 - deliberately skips the real __init__
        self.calls = []

    def _call(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs.get("json")))
        return {"results": [], "has_more": False}


class TestBackfillLeavesExistingCellsAlone(unittest.TestCase):
    """A refresh may fill a gap. It may never overwrite what is already there."""

    def test_update_row_patches_only_the_fields_it_is_given(self):
        client = _RecordingNotion()
        client.update_row("page-1", skills="Python, SQL")
        (_, _, body), = client.calls
        self.assertEqual(list(body["properties"]), [notion_sink.P_SKILLS])

    def test_update_row_sends_nothing_when_there_is_nothing_to_fill(self):
        client = _RecordingNotion()
        client.update_row("page-1")
        self.assertEqual(client.calls, [])

    def test_update_row_never_touches_the_user_s_own_columns(self):
        client = _RecordingNotion()
        client.update_row("page-1", skills="s", recruiter="a@b.com", notes="n",
                          title="T", title_url="https://example.com/job")
        (_, _, body), = client.calls
        for owned in (notion_sink.P_APPLIED, notion_sink.P_RESUME,
                      notion_sink.P_POSTED, notion_sink.P_PORTAL):
            self.assertNotIn(owned, body["properties"])

    def test_a_row_with_real_requirements_is_not_queued_for_backfill(self):
        row = {"Skill Requirements": "Strong SQL and a product mindset."}
        self.assertFalse(_needs_skills(row["Skill Requirements"]))

    def test_a_placeholder_row_is_queued_for_backfill(self):
        for placeholder in ("See posting", "Could not read the application page",
                            "Inferred from title", "See listing"):
            self.assertTrue(_needs_skills(placeholder), placeholder)

    def test_an_empty_row_is_queued_for_backfill(self):
        self.assertTrue(_needs_skills(""))


def _needs_skills(skills: str) -> bool:
    """The rule rows_to_backfill applies, kept here so the tests pin it."""
    return (not skills) or skills.startswith((
        "See posting", "Could not read", "Inferred from",
        "See listing", "Description fetched"))



APM_PAGE = """<html><head><title>Internships | APM Season</title></head><body>
<nav><a href="/about">About</a><a href="/blogs">Blog</a></nav>
<script>window.analytics=1;</script>
<table>
<tr><td>Google</td><td><a href="/jobs/g-apm-27">APM Intern, Summer 2027</a></td>
    <td>Mountain View, CA</td><td>2d ago</td></tr>
<tr><td>Microsoft</td><td><a href="https://careers.microsoft.com/200057344">Product Manager Intern</a></td>
    <td>Redmond, WA</td><td>Sep 23, 2026</td></tr>
</table>
<footer>Subscribe to our newsletter</footer></body></html>"""


class _FakeContent:
    def __init__(self, text): self.text = text


class _FakeModelReply:
    def __init__(self, text): self.content = [_FakeContent(text)]


class _FakeMessages:
    def __init__(self, reply): self.reply, self.prompts = reply, []

    def create(self, **kwargs):
        self.prompts.append(kwargs["messages"][0]["content"])
        return _FakeModelReply(self.reply)


class _FakeClient:
    def __init__(self, reply): self.messages = _FakeMessages(reply)


class _PageSession:
    def __init__(self, html): self.html, self.headers = html, {}

    def get(self, url, **kwargs):
        class _R:
            def __init__(self, text): self.text, self.status_code = text, 200
            def raise_for_status(self): pass
        return _R(self.html)


class TestModelReadSource(unittest.TestCase):
    """A source with no hand-written parser: the model reads the page."""

    def _fetch(self, reply, html=APM_PAGE):
        client = _FakeClient(reply)
        found = modelsite.fetch("https://www.apmseason.com/internships", "key",
                                source_name="apmseason",
                                session=_PageSession(html), client=client)
        return found, client

    def test_listings_become_postings(self):
        reply = """[
          {"title":"APM Intern, Summer 2027","company":"Google",
           "location":"Mountain View, CA","url":"/jobs/g-apm-27",
           "posted":"2d ago","term":"Summer 2027"},
          {"title":"Product Manager Intern","company":"Microsoft",
           "location":"Redmond, WA","url":"https://careers.microsoft.com/200057344",
           "posted":"Sep 23, 2026","term":""}]"""
        found, _ = self._fetch(reply)
        self.assertEqual(len(found), 2)
        self.assertEqual(found[0].company, "Google")
        self.assertEqual(found[0].term, "Summer 2027")

    def test_a_relative_url_is_resolved_against_the_page(self):
        reply = ('[{"title":"APM Intern","company":"Google","location":"",'
                 '"url":"/jobs/g-apm-27","posted":"","term":""}]')
        found, _ = self._fetch(reply)
        self.assertEqual(found[0].listing_url,
                         "https://www.apmseason.com/jobs/g-apm-27")

    def test_an_absolute_url_is_left_alone(self):
        reply = ('[{"title":"PM Intern","company":"Microsoft","location":"",'
                 '"url":"https://careers.microsoft.com/200057344","posted":"","term":""}]')
        found, _ = self._fetch(reply)
        self.assertEqual(found[0].listing_url,
                         "https://careers.microsoft.com/200057344")

    def test_rows_missing_a_company_or_title_are_dropped(self):
        reply = ('[{"title":"","company":"Google","url":"/a"},'
                 ' {"title":"APM Intern","company":"","url":"/b"},'
                 ' {"title":"PM Intern","company":"Stripe","url":"/c"}]')
        found, _ = self._fetch(reply)
        self.assertEqual([p.company for p in found], ["Stripe"])

    def test_the_page_reaches_the_model_with_its_links(self):
        found, client = self._fetch("[]")
        prompt = client.messages.prompts[0]
        self.assertIn("/jobs/g-apm-27", prompt)
        self.assertIn("Google", prompt)
        self.assertNotIn("window.analytics", prompt)  # script stripped

    def test_a_non_json_answer_yields_nothing_rather_than_raising(self):
        found, _ = self._fetch("I could not find any listings on that page.")
        self.assertEqual(found, [])

    def test_an_empty_array_is_not_an_error(self):
        found, _ = self._fetch("[]")
        self.assertEqual(found, [])

    def test_no_api_key_skips_the_source(self):
        self.assertEqual(
            modelsite.fetch("https://www.apmseason.com/internships", ""), [])

    def test_ids_are_stable_across_runs(self):
        reply = ('[{"title":"APM Intern","company":"Google","location":"",'
                 '"url":"/jobs/g-apm-27","posted":"","term":""}]')
        first, _ = self._fetch(reply)
        second, _ = self._fetch(reply)
        self.assertEqual(first[0].job_id, second[0].job_id)

    def test_the_same_job_from_two_sources_collapses(self):
        reply = ('[{"title":"Product Manager Intern","company":"Microsoft",'
                 '"location":"Redmond, WA","url":"https://careers.microsoft.com/1",'
                 '"posted":"","term":""}]')
        found, _ = self._fetch(reply)
        other = Posting(job_id="jr-999", title="Product Manager Intern",
                        company="Microsoft", source="jobright",
                        location="Redmond, WA")
        self.assertEqual(len(dedupe.collapse(found + [other])), 1)



class TestNicheFlag(unittest.TestCase):
    """A listing the big aggregators never carried is the interesting one."""

    def _p(self, source):
        return Posting(job_id="x", title="PM Intern", company="Acme", source=source)

    def test_a_small_board_only_listing_is_niche(self):
        self.assertTrue(self._p("hiringcafe").is_niche())
        self.assertTrue(self._p("apmseason").is_niche())
        self.assertTrue(self._p("hiringcafe + apmseason").is_niche())

    def test_anything_a_mainstream_list_carried_is_not_niche(self):
        for source in ("jobright-ai/2026-Product-Management-Internship",
                       "SimplifyJobs/Summer2027-Internships",
                       "vanshb03/Summer2027-Internships",
                       "dreamworkhq/Tech-Internships-2027",
                       "intern-list:pm"):
            self.assertFalse(self._p(source).is_niche(), source)

    def test_a_merged_listing_stops_being_niche(self):
        # Found on a small board and on jobright: everyone can see it.
        self.assertFalse(
            self._p("apmseason + jobright-ai/2026-Design-Internship").is_niche())

    def test_the_flag_is_written_to_notion(self):
        self.assertIn(notion_sink.P_NICHE, notion_sink.SCHEMA)
        self.assertEqual(notion_sink.SCHEMA[notion_sink.P_NICHE], {"checkbox": {}})



if __name__ == "__main__":
    unittest.main()

