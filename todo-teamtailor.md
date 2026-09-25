# TODO: Teamtailor ATS parser

> **Status: implemented in 3.8.0** (`src/fetchaller/content/teamtailor.py`,
> `tests/test_teamtailor.py`, docs in `docs/site-apis.md`). Re-measured live on
> 2026-09-25; four claims below were wrong or incomplete and the code follows
> the measurement, not this file:
>
> 1. **"RSS ignores `?page`"** is true only *unfiltered*. With any board filter
>    (`query`, `department`, `location`, `country`, `remote_status_id`,
>    `employment_type`, `language`) both feeds page at **20**, ignore
>    `per_page`, emit **no** `next_url`, and honour `page=N`. Following
>    `next_url` alone stops at 20 (SATS `department=Gym`: 20+20+20+10).
> 2. **"Follow `next_url` until it is absent"** / "default page size not
>    measured": unfiltered JSON pages at **100** (`per_page` is capped there).
>    RSS also stops at 100 by default and cannot page, so the merge below
>    silently loses department/location/remote for everything past 100 —
>    unless RSS is asked for `per_page=<JSON total>`, which it honours
>    (Uniflex: 254 of 254).
> 3. **`remoteStatus`** has five values, not three: `fully`, `hybrid`,
>    `onsite`, `temporary`, `none`. `none` is the *default*: the board filter
>    labels it "No Remote Work", but posting pages show nothing for it (they do
>    show "Onsite") and Oatly leaves it on "United States - Remote" postings.
> 4. **Salary** also arrives as a single `value` and with an empty `currency`
>    (Deepki); the JSON and RSS posted dates can disagree by months (Oatly
>    republishes); a locale prefix (`/fr-CA/jobs.json`) is a separate,
>    possibly empty per-language listing; an empty map-bounds filter makes the
>    feeds answer 422; the subdomain HTML 301s to the customer's domain while
>    the feeds answer on the subdomain.

Add structured support for Teamtailor career boards, matching the existing
Workday / Ashby / BambooHR / JazzHR / Dayforce parsers. Evidence below was
verified live on 2026-09-24 against four tenants.

## Current behavior

- **HTML list page** (`https://<host>/jobs`) goes through the generic
  markdown converter. It returns only the first 20 jobs, with no salary and a
  lot of cookie-consent and filter-facet text. It does not follow the
  `Show N more` link (`/jobs/show_more?page=N`, a Turbo Stream,
  `text/vnd.turbo-stream.html`), so any job after the 20th is missed.
- **`https://<host>/jobs.json`** is passed through as raw JSON. Every item
  includes the full description HTML (Wagepoint: 11 jobs = 496 KB), so at the
  default `maxTokens` only 1 of 11 items comes back (reported in
  `_fetchaller_truncated`).

## Detection

- Host is `<slug>.teamtailor.com`, OR a custom domain (`careers.oatly.com`,
  `careers.deepki.com`, `careers.steel-eye.com`).
- Custom-domain probe: `GET https://<host>/jobs.json` answers
  `Content-Type: application/feed+json` with
  `"version": "https://jsonfeed.org/version/1.1"`.
- HTML markers: `teamtailor-cdn.com` asset URLs, an
  `app.teamtailor.com/companies/<id>` employee-login link, and the footer link
  text `Applicant tracking system by Teamtailor`.

## Data sources

All public and unauthenticated. No single source carries every field, so the
parser has to merge them.

1. **`https://<host>/jobs.json`** (JSON Feed 1.1)
   - Has: `id` (UUID), `title`, `url`, `date_published`, `content_html`,
     `_jobposting.identifier.value` (numeric job id),
     `_jobposting.baseSalary` = `{currency, value: {minValue, maxValue, unitText}}`.
   - `minValue` / `maxValue` are **strings**. `baseSalary` is absent when the
     employer does not publish a band.
   - Missing: department, location name, remote status.
   - Pagination: honours `?per_page=N` and emits a top-level `next_url`.
     Follow `next_url` until it is absent. Default page size not measured
     (23 returned in one call on the largest tenant found).
   - Filters work: `?query=ux`, `?department=Product`. A filter that matches
     nothing returns `items: []`, not an error.
2. **`https://<host>/jobs.rss`**
   - Has: `guid` (same UUID as the JSON `id`), `<remoteStatus>`
     (`fully` | `hybrid` | `none`), `<tt:department>`, `<tt:role>`,
     `<tt:locations><tt:location><tt:name>` (e.g. `Canada - REMOTE`) plus
     `tt:address` / `tt:city` / `tt:zip` / `tt:country`.
     Namespace: `xmlns:tt="https://teamtailor.com/locations"`.
   - Missing: salary.
   - **Ignores `?page`**: `?per_page=10&page=3` returns the first 10 again.
     Fetch it once with no paging params.
3. **Job page** `https://<host>/jobs/<id>-<slug>`
   - JSON-LD `JobPosting` with everything in the JSON feed, plus
     `jobLocationType` (`TELECOMMUTE`), `employmentType` (`FULL_TIME`) and
     `applicantLocationRequirements` (`{"@type": "Country", "name": "Canada"}`).
   - The rendered page also shows labelled `Department`, `Location`,
     `Remote status` and `Yearly salary` values.

Not usable:

- No per-job JSON: `/jobs/<id>.json` 301s, `/jobs/<id>-<slug>.json` 406s.
- `api.teamtailor.com/v1` is the authenticated customer API (406 without a key).

## Merge

Join JSON `id` to RSS `guid` (11/11 matched on Wagepoint). Confirm both feeds
return the same job count. RSS returned all 23 Oatly jobs in one call, but
nobody has checked whether RSS stops at some size on very large boards.

## Trap: the JSON-LD address is the company office

`jobLocation.address` in both the JSON feed and the job page is the
**company's office**, not the job's location. Wagepoint's fully remote
`Canada - REMOTE` job carries `70 Shawville Blvd SE, Calgary`. Take location
from RSS `tt:name` and remote status from RSS `remoteStatus`, never from the
address.

## Output

- **List mode:** one row per job with title, numeric id, url, department,
  location name(s), remote status, posted date, and salary
  (`currency min-max / unit`, or `not published`). Leave descriptions out so
  large boards are not truncated.
- **Single job URL:** full description plus the same fields, including
  `employmentType` and `applicantLocationRequirements`.

## Test fixtures (live 2026-09-24)

| Host | Jobs | What it tests |
| --- | --- | --- |
| `wagepoint.teamtailor.com` | 11, all with salary | Job `8384421` "Staff UX Designer": Product, `Canada - REMOTE`, `fully`, posted 2026-09-15, CAD 125000-145000 / YEAR. Bilingual (EN + FR) descriptions. |
| `careers.oatly.com` | 23 | Custom domain; HTML list stops at 20, so this tests completeness. `per_page=10` gives pages of 10, 10, 3. |
| `careers.deepki.com` | 13, 3 with salary | Missing-band case. |
| `careers.steel-eye.com` | 4, none with salary | Custom domain only; do not assume a `*.teamtailor.com` subdomain exists. |

Job counts will drift; assert on structure and on the fields of a job that is
still live, not on exact totals.
