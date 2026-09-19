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

### 3. First run (bootstrap)

Run the workflow manually from the **Actions** tab with `bootstrap: true`.

Bootstrap reconstructs each listing's real posting time by walking the source
repos' commit history. Without it, the first run would stamp every historical
listing as "just now" and the 24-hour filter would let through days of backlog.
**Only do this once.** Every run after it is a plain hourly run.

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
| Posted | real timestamp, not the source's day-granularity date |
| Hours Since Posted | formula, `dateBetween(now(), prop("Posted"), "hours")` — recalculates whenever you open the database |
| Recruiter Contact | Apollo, if a key is set |
| Applied | select: Not applied / Applying / Applied / Interviewing / Offer / Rejected |
| My Resume PDF | empty files property — drag your tailored PDF onto the row |
| Term / Category / Source / Job ID | Job ID is the dedup key; don't delete that column |

### Where keywords and skills come from

Each new listing's application page is fetched and the posting text extracted,
so keywords and skill requirements come from the employer's own wording — which
is what a resume screener matches against. Greenhouse, Lever and SmartRecruiters
serve usable HTML; Workday is asked for JSON instead; JS-only pages are mined for
an embedded payload.

When a page can't be read (dead link, login wall, no embedded payload), the row
falls back to inferring from the role title and the Skill Requirements cell says
so. Set `fetch_descriptions: false` in `config.yaml` to skip the fetch entirely.

Without `ANTHROPIC_API_KEY` the description is fetched but never summarised, and
every row gets the same generic keyword list.

### Keeping the hourly schedule alive

GitHub **disables scheduled workflows in a repository with no pushes for 60
days**, and delays `schedule` runs under load — an hourly cron can drift or skip.
If this goes quiet, check the Actions tab: re-enabling the workflow is one click.

## Sources

Four jobright repos (product management, design, business analyst, data
analysis) are scraped and cross-deduplicated. They publish a markdown table on
the `master` branch — not `main`, which returns a silent 404.

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

