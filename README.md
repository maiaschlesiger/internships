# internship-radar

Hourly scrape of internship listings into a Notion database, filtered to
**Summer 2027** roles in product management, product design, consulting and
lightly-technical adjacent work.

## Setup

Four steps. Only the first two are required.

### 1. Notion integration

1. Go to <https://www.notion.so/my-integrations> and create a new **internal**
   integration. Copy the secret (starts with `secret_` or `ntn_`).
2. In Notion, create or pick the page the database should live under. Open it,
   click the `...` menu, **Connections → Connect to →** your integration.
   *An integration can only see what has been explicitly shared with it — this
   step is the most common cause of a 404 on a page that obviously exists.*
3. Copy that page's id from its URL: the 32 hex characters after the last `/`
   and before `?`.

### 2. Create the database

```bash
pip install -r requirements.txt
export NOTION_TOKEN=secret_...
export NOTION_PARENT_PAGE_ID=<32-hex-page-id>
python -m radar.main --create-database
```

This prints a `NOTION_DATABASE_ID`. Add both values as repository secrets under
**Settings → Secrets and variables → Actions**:

| Secret | Required | Effect if missing |
|---|---|---|
| `NOTION_TOKEN` | yes | nothing runs |
| `NOTION_DATABASE_ID` | yes | nothing runs |
| `ANTHROPIC_API_KEY` | recommended | ambiguous listings stay on keyword rules; every row gets the same generic resume keywords |
| `APOLLO_API_KEY` | optional | Recruiter Contact column stays empty |

### Rebuilding from scratch

To empty the database and repopulate it — after changing the filters, say —
run the workflow manually with both **clear_first** and **bootstrap** ticked.

`clear_first` archives every row. Notion keeps archived pages in the workspace
trash, so this is recoverable for a while, and the database itself, its columns
and the Hours Since Posted formula are untouched. The cron cannot set it; it is
reachable only from a manual dispatch.

Locally the same thing is `python -m radar.main --clear-database --bootstrap`.

### 3. First run (bootstrap)

Run the workflow manually from the **Actions** tab with `bootstrap: true`.

Bootstrap reconstructs each listing's real posting time by walking the source
repos' commit history. Without it, the first run would stamp every historical
listing as "just now" and the 24-hour filter would let through days of backlog.
**Only do this once.** Every run after it is a plain hourly run.

### Recruiter contacts

Most of this column fills for free. Employers routinely print a hiring address
in the posting itself — a university recruiting inbox, an accommodations
contact, sometimes a named recruiter — and the application page is already being
fetched, so those are taken directly. Applicant-tracking vendors' own addresses,
automated senders and legal inboxes are discarded, a hiring-function local part
wins over a generic one, and nothing is ever guessed or constructed.

Apollo is then asked only about listings that came back with nothing, capped by
`apollo_max_lookups` in `config.yaml` because it bills credits per reveal.

There is no free database of recruiter emails. That data is the product Apollo,
ZoomInfo and Hunter sell; the open-source projects in this space chain together
other services' free tiers rather than holding data of their own. Scraping what
employers publish is the free option, and it is a real one.

### 4. Apollo (optional)

Generate a key in Apollo under **Settings → Integrations → API**.

> Connecting the Apollo *connector* on claude.ai is not a substitute. A
> connector authenticates an interactive chat session; it cannot authenticate an
> unattended scheduled job. The same is true of Notion — the cron needs the
> integration token above, not a connector.

## Columns

| Column | Notes |
|---|---|
| Title / Company / Location | from the source table |
| Application Portal | the listing URL followed through to the employer's own ATS page where the redirect can be resolved |
| Resume Keywords | multi-select, **inferred from the role title** — see caveat below |
| Skill Requirements | same caveat |
| Posted | best available — see below |
| Hours Since Posted | formula, `dateBetween(now(), prop("Posted"), "hours")` — recalculates whenever you open the database |
| Recruiter Contact | the address printed in the posting; Apollo only fills gaps |
| Applied | select: Not applied / Applying / Applied / Interviewing / Offer / Rejected |
| My Resume PDF | empty files property — drag your tailored PDF onto the row |
| Term / Category / Source / Job ID | Job ID is the dedup key; don't delete that column |

### Where the posting time comes from

Every list publishes day granularity at best ("Aug 21", "3d"), which is useless
for a 24-hour window. Three estimates are used, most precise first:

1. **`scraped`** — the employer's own `datePosted` from the schema.org
   JobPosting block on the application page, *when it includes a time of day*.
2. **`commit`** — for the jobright repos, the commit that first introduced the
   listing. They commit roughly hourly, so this is good to about 70 minutes.
3. **`first_seen`** — the first run that observed the listing, accurate to the
   cron interval.

A bare calendar date from a page never replaces a better estimate. Be aware that
**most applicant tracking systems do not publish the hour a job went live** —
`datePosted` is usually just a date. "What hour it was posted" is frequently not
a fact that exists publicly, so the `commit` estimate is often the most precise
figure available.

### Where keywords and skills come from

Each new listing's application page is fetched and the posting text extracted,
so keywords and skill requirements come from the employer's own wording — which
is what a resume screener matches against. Greenhouse, Lever and SmartRecruiters
serve usable HTML; Workday is asked for JSON instead; JS-only pages are mined for
an embedded payload.

When a page can't be read (dead link, login wall, no embedded payload), the row
falls back to inferring from the role title and the Skill Requirements cell says
so. Set `fetch_descriptions: false` in `config.yaml` to skip the fetch entirely.

Skill Requirements never contains a guess from the job title. With
`ANTHROPIC_API_KEY` set, the model summarises the posting. Without it, the
requirements/qualifications section is lifted out of the page verbatim —
the employer's own words, with benefits and EEO boilerplate stripped. Only a
posting whose page could not be read at all gets a placeholder, and it says so.

Resume Keywords still fall back to a per-category list without a key.

### Keeping the hourly schedule alive

GitHub **disables scheduled workflows in a repository with no pushes for 60
days**, and delays `schedule` runs under load — an hourly cron can drift or skip.
If this goes quiet, check the Actions tab: re-enabling the workflow is one click.

## Sources

Seven lists, all cross-deduplicated against each other:

| Source | Format | Notes |
|---|---|---|
| 4 × jobright-ai repos | markdown table, `master` branch | `main` returns a silent 404 |
| vanshb03/Summer2027-Internships | markdown table, `main` | links straight to the employer ATS |
| SimplifyJobs/Summer2027-Internships | **HTML** `<tr>` table, `master` | ~1,700 rows |
| dreamworkhq/Tech-Internships-2027 | markdown table, `main` | links via its own redirector |

The community lists mark eligibility with emoji, per their own legends. Rows
marked 🔒 (application closed) or 🎓 (Master's/PhD/MBA required) are dropped at
parse time rather than being left for the relevance filter.

Software engineering roles are excluded — see `reject_categories` in
`config.yaml`. This is about the role, not the technology: a *technical product*
or *product analyst* role still counts, and "Software Product Management Intern"
is correctly kept.

### Deduplication

The same job routinely appears on several lists. `radar/dedupe.py` collapses
them in two passes: by canonical application URL with tracking parameters
stripped, then by a fingerprint of company, normalised title and city — which is
what catches lists that link through their own redirector instead of the
employer. A merged row keeps the most precise timestamp and prefers a real
employer URL over a redirect.

`intern-list.com` support is **written but unverified**. That host was blocked by
network policy in the environment this was built in, so its real markup was never
observed and the parser is inference, not evidence. To finish it: run the
workflow manually and download the `internlist-probe` artifact, which captures
the live HTML, then tighten the selectors in `radar/sources/internlist.py`. Until
then that source logs a warning and returns nothing; it cannot break the run.

## Local use

```bash
python -m radar.main --dry-run          # full pipeline, prints instead of writing
python -m radar.main --dry-run -v       # with debug logging
python -m unittest discover -s tests    # 15 regression tests
```

## Tuning

Edit `config.yaml`, not the code. The classifier reads it at run time:
`reject_categories` (fields to exclude outright), `categories` (what counts),
`target_term` accept/reject patterns, and `window_hours`.

Rules are matched against the **role title only**. Matching the company name too
caused "Business Analytics-Intelligence" at a construction firm to be rejected
for `construction`.

