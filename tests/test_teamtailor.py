"""Teamtailor career sites: detection, the two-feed board read, and posting pages.

Fixtures are trimmed from live responses captured 2026-09-24/25 (Wagepoint,
Oatly, Deepki, SATS, Uniflex). Descriptions are cut to a sentence; every field
the code reads is kept in its live shape — salary bounds as strings, an empty
currency, a regional host, the U+202F space an employer pasted into a role.
"""

import json
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import escape

import pytest

from fetchaller.content import teamtailor as tt
from fetchaller.content.html_preflight import inspect_html_preflight
from fetchaller.tools.fetch import _fetch_url_impl

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_MARKERS = (
    '<script src="https://assets-aws.teamtailor-cdn.com/assets/careersite-be8c4bda.js" type="module"></script>'
    '<a href="https://app.teamtailor.com/companies/oClrHUa4fnE@eu/dashboard">Log in as employee</a>'
)


def _json_item(uuid, numeric, title, *, host="careers.oatly.com", published="2026-09-18T03:16:27+02:00",
               salary=None, valid_through=None):
    posting = {
        "@context": "http://schema.org/",
        "@type": "JobPosting",
        "title": title,
        "identifier": {"@type": "PropertyValue", "name": "Oatly AB", "value": numeric},
        "datePosted": published,
        "description": "<p>Long description.</p>",
        # The office on file, not where the job is (see the location trap).
        "jobLocation": [{"@type": "Place", "address": {"streetAddress": "70 Shawville Blvd SE",
                                                       "addressLocality": "Calgary"}}],
    }
    if salary is not None:
        posting["baseSalary"] = salary
    if valid_through is not None:
        posting["validThrough"] = valid_through
    return {
        "id": uuid,
        "title": title,
        "url": f"https://{host}/jobs/{numeric}-slug",
        "date_published": published,
        "content_html": "<p>Long description.</p>",
        "_jobposting": posting,
    }


def _feed(items, next_url=None, host="careers.oatly.com"):
    data = {
        "version": "https://jsonfeed.org/version/1.1",
        "title": "Oatly AB",
        "home_page_url": f"https://{host}/jobs",
        "feed_url": f"https://{host}/jobs.json",
        "items": items,
    }
    if next_url:
        data["next_url"] = next_url
    return data


def _rss_item(uuid, numeric, title, *, remote="none", department="Sales & Commercial", role="",
              locations=("United States - Remote",), pub="Thu, 18 Sep 2026 03:16:27 +0200",
              host="careers.oatly.com"):
    locs = "".join(
        f"<tt:location><tt:name>{escape(name)}</tt:name><tt:address>70 Shawville Blvd SE</tt:address>"
        f"<tt:city>Calgary</tt:city><tt:country>Canada</tt:country></tt:location>"
        for name in locations
    )
    return (
        f"<item><title>{escape(title)}</title><description>&lt;p&gt;Long description.&lt;/p&gt;</description>"
        f"<pubDate>{pub}</pubDate><link>https://{host}/jobs/{numeric}-slug</link>"
        f"<remoteStatus>{remote}</remoteStatus><guid>{uuid}</guid>"
        f"<tt:locations>{locs}</tt:locations>"
        f"<tt:department>{escape(department)}</tt:department><tt:role>{escape(role)}</tt:role></item>"
    )


def _rss(items_xml, host="careers.oatly.com"):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0" xmlns:tt="https://teamtailor.com/locations"><channel>'
        f"<title>Oatly AB</title><description/><link>https://{host}/jobs</link>"
        + "".join(items_xml)
        + "</channel></rss>"
    ).encode()


class _Resp:
    def __init__(self, url, status=200, *, body=b"", content_type="text/html; charset=utf-8", data=None):
        self.url = url
        self.status_code = status
        if data is not None:
            body = json.dumps(data).encode()
            content_type = "application/feed+json; charset=utf-8"
        self.content = body
        self.text = body.decode("utf-8")
        self.headers = {"content-type": content_type}

    def json(self):
        return json.loads(self.text)


class _Session:
    """Answers from a route table keyed by (path, frozenset(query items))."""

    def __init__(self, routes):
        self.routes = routes
        self.seen: list[str] = []

    async def get(self, url, **kwargs):
        self.seen.append(url)
        parsed = urlparse(url)
        key = (parsed.path, frozenset((k, v[0]) for k, v in parse_qs(parsed.query).items()))
        answer = self.routes.get(key)
        if answer is None:
            return _Resp(url, 404, body=b"{}", content_type="application/json")
        kind, payload = answer
        if kind == "json":
            return _Resp(url, data=payload)
        return _Resp(url, body=payload, content_type="application/rss+xml; charset=utf-8")


def _q(**kwargs):
    return frozenset(kwargs.items())


@pytest.fixture
def inline_rss(monkeypatch):
    """Parse RSS in-process; the isolation itself is covered by the pipeline test."""

    async def _inline(function, *args, timeout=20.0):
        return function(*args)

    monkeypatch.setattr(tt, "run_isolated", _inline)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


class TestDetection:
    @pytest.mark.parametrize("url", [
        "https://wagepoint.teamtailor.com/jobs",
        "https://wagepoint.teamtailor.com/jobs/",
        "https://wagepoint.teamtailor.com/fr-CA/jobs",
        "https://meteor-1691488970.na.teamtailor.com/jobs",
        "https://career.teamtailor.com/jobs",  # Teamtailor's own board is a tenant
        "https://sats.teamtailor.com/jobs?department=Gym",
    ])
    def test_board_on_a_teamtailor_host(self, url):
        assert tt.is_teamtailor_board_url(url)
        assert tt.teamtailor_job_id(url) is None

    @pytest.mark.parametrize("url", [
        "https://www.teamtailor.com/jobs",
        "https://app.teamtailor.com/jobs",
        "https://careers.oatly.com/jobs",  # custom domain: only the page can tell
        "https://wagepoint.teamtailor.com/",
        "https://wagepoint.teamtailor.com/jobs/8384421-staff-ux-designer",
        "https://wagepoint.teamtailor.com/departments/product",
        "https://teamtailor.com.evil.example/jobs",
        "ftp://wagepoint.teamtailor.com/jobs",
    ])
    def test_not_a_board_url(self, url):
        assert not tt.is_teamtailor_board_url(url)

    def test_custom_domain_board_path_is_recognised_for_the_page_check(self):
        assert tt.is_teamtailor_board_path("https://careers.oatly.com/jobs")
        assert tt.is_teamtailor_board_path("https://careers.oatly.com/en-GB/jobs/")
        assert not tt.is_teamtailor_board_path("https://careers.oatly.com/jobs/8427035-lab")

    @pytest.mark.parametrize("url,expected", [
        ("https://careers.oatly.com/jobs/8427035-laboratory-technician-at-oatly", "8427035"),
        ("https://careers.oatly.com/jobs/8427035", "8427035"),
        ("https://wagepoint.teamtailor.com/fr-CA/jobs/8384421-staff-ux-designer", "8384421"),
        ("https://careers.oatly.com/jobs/abc", None),
        ("https://careers.oatly.com/jobs/8427035-x/apply", None),
    ])
    def test_job_id(self, url, expected):
        assert tt.teamtailor_job_id(url) == expected

    def test_page_markers(self):
        assert tt.is_teamtailor_html(f"<html>{_MARKERS}</html>")
        footer = ('<img src="https://images.teamtailor-cdn.com/x.png">'
                  '<a aria-label="Applicant tracking system by Teamtailor"></a>')
        assert tt.is_teamtailor_html(footer)
        # A CDN image alone is a blog embedding a picture, not a career site.
        assert not tt.is_teamtailor_html('<img src="https://images.teamtailor-cdn.com/x.png">')
        assert not tt.is_teamtailor_html('<a href="https://app.teamtailor.com/companies/x">x</a>')


class TestParams:
    def test_filters_are_forwarded_and_named(self):
        got = tt._classify_params(
            "https://careers.oatly.com/jobs?department=People&split_view=true&query=&location="
            "&foo=bar&page=3&country=United%20States"
        )
        assert got["forward"] == [("department", "People"), ("foo", "bar"), ("country", "United States")]
        assert got["applied"] == [("department", "People"), ("country", "United States")]
        assert got["unknown"] == [("foo", "bar")]
        assert got["paging"] == [("page", "3")]


# ---------------------------------------------------------------------------
# Salary
# ---------------------------------------------------------------------------


class TestSalary:
    def test_range_with_currency(self):
        base = {"@type": "MonetaryAmount", "currency": "CAD",
                "value": {"@type": "QuantitativeValue", "unitText": "YEAR", "minValue": "125000", "maxValue": "145000"}}
        assert tt.format_salary(base) == "CAD 125,000–145,000 / year"

    def test_single_value_with_empty_currency_says_so(self):
        base = {"currency": "", "value": {"unitText": "YEAR", "value": "40000"}}
        assert tt.format_salary(base) == "40,000 / year (currency not stated)"

    def test_implausible_band_is_quoted_not_corrected(self):
        # Deepki publishes USD 145-165 a year; the page shows "$145 - $165".
        base = {"currency": "USD", "value": {"unitText": "YEAR", "minValue": "145", "maxValue": "165"}}
        assert tt.format_salary(base) == "USD 145–165 / year"

    def test_equal_bounds_hourly_and_decimals(self):
        base = {"currency": "SEK", "value": {"unitText": "HOUR", "minValue": "152.5", "maxValue": "152.50"}}
        assert tt.format_salary(base) == "SEK 152.5 / hour"

    def test_one_sided_and_absent(self):
        assert tt.format_salary({"currency": "EUR", "value": {"unitText": "MONTH", "minValue": "3000"}}) == \
            "EUR from 3,000 / month"
        assert tt.format_salary(None) is None
        assert tt.format_salary({"currency": "EUR", "value": {"unitText": "YEAR"}}) is None


# ---------------------------------------------------------------------------
# RSS
# ---------------------------------------------------------------------------


class TestRss:
    def test_fields_the_json_feed_lacks(self):
        xml = _rss([
            _rss_item("u-1", 8191515, "Global Partnerships Lead", remote="hybrid", department="Sales",
                      locations=("London Office", "Paris Office")),
            _rss_item("u-2", 8288018, "Software Development Engineer - Mobile", remote="fully",
                      department="Engineering", role="Software Development Engineer - Mobile"),
        ])
        got = tt.parse_teamtailor_rss(xml)
        assert [g["guid"] for g in got] == ["u-1", "u-2"]
        assert got[0]["locations"] == ["London Office", "Paris Office"]
        assert got[0]["remoteStatus"] == "hybrid"
        assert got[0]["department"] == "Sales"
        assert got[1]["role"] == "Software Development Engineer - Mobile"

    def test_not_a_feed(self):
        assert tt.parse_teamtailor_rss(b"<html><body>no</body></html>") is None
        assert tt.parse_teamtailor_rss(b"not xml") is None


# ---------------------------------------------------------------------------
# Board fetch: paging modes
# ---------------------------------------------------------------------------


class TestBoardFetch:
    async def test_unfiltered_follows_next_url_and_sizes_rss_to_json(self, inline_rss):
        host = "uniflex.teamtailor.com"
        page1 = [_json_item(f"u-{i}", 1000 + i, f"Job {i}", host=host) for i in range(100)]
        page2 = [_json_item(f"u-{i}", 1000 + i, f"Job {i}", host=host) for i in range(100, 154)]
        rss = _rss([_rss_item(f"u-{i}", 1000 + i, f"Job {i}", host=host) for i in range(154)], host=host)
        session = _Session({
            ("/jobs.json", _q()): ("json", _feed(page1, next_url=f"https://{host}/jobs.json?page=2&per_page=100", host=host)),
            ("/jobs.json", _q(page="2", per_page="100")): ("json", _feed(page2, host=host)),
            # RSS stops at 100 by default and ignores page; per_page past 100 works.
            ("/jobs.rss", _q(per_page="154")): ("rss", rss),
        })
        board = await tt.fetch_teamtailor_board(f"https://{host}/jobs", session)

        assert len(board["items"]) == 154
        assert len(board["rss"]) == 154
        assert (board["json_pages"], board["rss_pages"]) == (2, 1)
        assert board["json_stop"] is None and board["rss_stop"] is None
        assert session.seen[-1] == f"https://{host}/jobs.rss?per_page=154"

    async def test_filtered_walks_page_n_because_next_url_never_comes(self, inline_rss):
        host = "sats.teamtailor.com"
        items = [_json_item(f"g-{i}", 2000 + i, f"Gym {i}", host=host) for i in range(45)]
        rss_items = [_rss_item(f"g-{i}", 2000 + i, f"Gym {i}", host=host, department="Gym") for i in range(45)]
        routes = {}
        for page, (lo, hi) in enumerate([(0, 20), (20, 40), (40, 45)], start=1):
            query = _q(department="Gym") if page == 1 else _q(department="Gym", page=str(page))
            routes[("/jobs.json", query)] = ("json", _feed(items[lo:hi], host=host))
            routes[("/jobs.rss", query)] = ("rss", _rss(rss_items[lo:hi], host=host))
        session = _Session(routes)

        board = await tt.fetch_teamtailor_board(
            f"https://{host}/jobs?department=Gym&split_view=true&query=", session
        )

        assert len(board["items"]) == 45
        assert len(board["rss"]) == 45
        # A short page ends the walk: no request for page 4.
        assert (board["json_pages"], board["rss_pages"]) == (3, 3)
        assert not any("page=4" in u for u in session.seen)

    async def test_unfiltered_board_of_exactly_twenty_costs_no_extra_page(self, inline_rss):
        items = [_json_item(f"u-{i}", 3000 + i, f"Job {i}") for i in range(20)]
        session = _Session({
            ("/jobs.json", _q()): ("json", _feed(items)),
            ("/jobs.rss", _q()): ("rss", _rss([_rss_item(f"u-{i}", 3000 + i, f"Job {i}") for i in range(20)])),
        })
        board = await tt.fetch_teamtailor_board("https://careers.oatly.com/jobs", session)
        assert len(board["items"]) == 20
        assert len(session.seen) == 2

    async def test_next_url_to_another_host_is_not_followed(self, inline_rss):
        session = _Session({
            ("/jobs.json", _q()): ("json", _feed([_json_item("u-1", 1, "One")],
                                                 next_url="https://attacker.example/jobs.json?page=2")),
            ("/jobs.rss", _q()): ("rss", _rss([_rss_item("u-1", 1, "One")])),
        })
        board = await tt.fetch_teamtailor_board("https://careers.oatly.com/jobs", session)
        assert len(board["items"]) == 1
        assert not any("attacker" in u for u in session.seen)

    async def test_no_json_feed_means_not_a_board(self, inline_rss):
        session = _Session({})
        assert await tt.fetch_teamtailor_board("https://careers.oatly.com/jobs", session) is None

    async def test_locale_prefix_reads_that_languages_feeds(self, inline_rss):
        host = "wagepoint.teamtailor.com"
        session = _Session({
            ("/fr-CA/jobs.json", _q()): ("json", _feed([], host=host)),
            ("/fr-CA/jobs.rss", _q()): ("rss", _rss([], host=host)),
        })
        board = await tt.fetch_teamtailor_board(f"https://{host}/fr-CA/jobs", session)
        out = tt.render_teamtailor_board(board)
        assert "(0 open positions)" in out
        assert "No postings are listed in the `fr-CA` language" in out
        assert f"https://{host}/jobs." in out


# ---------------------------------------------------------------------------
# Board render
# ---------------------------------------------------------------------------


def _board(items, rss_items, **extra):
    board = {
        "title": "Oatly AB",
        "board_url": "https://careers.oatly.com/jobs",
        "feed_base": "https://careers.oatly.com",
        "locale": "",
        "params": {"forward": [], "applied": [], "unknown": [], "paging": []},
        "items": [tt._slim_json_item(i) for i in items],
        "json_pages": 1,
        "json_stop": None,
        "rss": {r["guid"]: r for r in rss_items},
        "rss_order": [r["guid"] for r in rss_items],
        "rss_pages": 1,
        "rss_stop": None,
    }
    board.update(extra)
    return board


class TestBoardRender:
    def test_join_location_remote_and_salary(self):
        cad = {"currency": "CAD", "value": {"unitText": "YEAR", "minValue": "125000", "maxValue": "145000"}}
        items = [
            _json_item("a", 8384421, "Staff UX Designer", salary=cad),
            _json_item("b", 8427035, "Laboratory Technician at Oatly", valid_through="2026-10-06T23:59:59+02:00"),
            _json_item("c", 8399088, "National Account Manager, Albertsons"),
        ]
        rss = tt.parse_teamtailor_rss(_rss([
            _rss_item("a", 8384421, "Staff UX Designer", remote="fully", department="Product",
                      locations=("Canada - REMOTE",)),
            _rss_item("b", 8427035, "Laboratory Technician at Oatly", remote="onsite",
                      department="Quality & Regulatory Affairs", locations=("Landskrona",),
                      role="Quality Control / Laboratory"),
            _rss_item("c", 8399088, "National Account Manager, Albertsons"),
        ]))
        out = tt.render_teamtailor_board(_board(items, rss))

        assert "# Oatly AB — Teamtailor job board (3 open positions)" in out
        assert ("- **Staff UX Designer** (8384421) — Canada - REMOTE · remote: fully · "
                "CAD 125,000–145,000 / year · posted 2026-09-18") in out
        assert ("Landskrona · remote: onsite · salary not published · posted 2026-09-18 · closes 2026-10-06 · "
                "role: Quality Control / Laboratory") in out
        # `none` is the default, not a statement that the job is on-site.
        assert "United States - Remote · remote: none (not set)" in out
        assert "No Remote Work" in out
        # The office street address on the JSON-LD is never the job's location.
        assert "Shawville" not in out and "Calgary" not in out
        assert "**salary published**: 1 of 3 postings in jobs.json" in out
        assert "**departments**: Product (1) · Quality & Regulatory Affairs (1) · Sales & Commercial (1)" in out

    def test_missing_from_one_feed_is_unknown_not_unpublished(self):
        items = [_json_item("a", 1, "In both"), _json_item("b", 2, "JSON only")]
        rss = tt.parse_teamtailor_rss(_rss([_rss_item("a", 1, "In both"), _rss_item("z", 26, "RSS only")]))
        out = tt.render_teamtailor_board(_board(items, rss))

        assert "(3 open positions)" in out
        assert "**JSON only** (2) — location and remote status unknown (not in jobs.rss)" in out
        assert "## Department unknown (not in jobs.rss) (1)" in out
        assert "**RSS only** (26) — United States - Remote · remote: none (not set) · salary unknown (not in jobs.json)" in out
        assert "1 posting is in jobs.json but not jobs.rss" in out
        assert "1 posting is in jobs.rss but not jobs.json" in out

    def test_republished_date_shows_both(self):
        items = [_json_item("a", 7768057, "Außendienst", published="2026-09-23T00:00:00+02:00")]
        rss = tt.parse_teamtailor_rss(_rss([_rss_item("a", 7768057, "Außendienst",
                                                      pub="Wed, 20 May 2026 16:14:35 +0200")]))
        out = tt.render_teamtailor_board(_board(items, rss))
        assert "posted 2026-09-23 (RSS: 2026-05-20)" in out

    def test_incomplete_read_is_said(self):
        out = tt.render_teamtailor_board(_board(
            [_json_item("a", 1, "One")], [],
            json_stop="jobs.json page 2 failed (HTTP 500); postings after it are missing",
            rss_stop="jobs.rss page 1 failed (HTTP 503)",
        ))
        assert "**incomplete**: jobs.json page 2 failed (HTTP 500); postings after it are missing." in out
        assert "**jobs.rss**: jobs.rss page 1 failed (HTTP 503); department, location and remote status" in out

    def test_filters_are_reported(self):
        params = tt._classify_params("https://careers.oatly.com/jobs?department=People&foo=bar&page=2")
        out = tt.render_teamtailor_board(_board([], [], params=params))
        assert "**filters applied by the board**: department=People" in out
        assert "**forwarded, not a known Teamtailor filter**: foo=bar" in out
        assert "**ignored**: page=2 (every page is read)" in out
        assert "No postings match these filters." in out


# ---------------------------------------------------------------------------
# Posting page
# ---------------------------------------------------------------------------


def _job_page(*, badge="Onsite", facts=(("Department", "Quality &amp; Regulatory Affairs"),
                                        ("Location", "Landskrona")), salary=None,
              extra_ld=None):
    posting = {
        "@context": "http://schema.org/",
        "@type": "JobPosting",
        # The page escapes its JSON-LD strings once more than the feed does.
        "title": "Lab &amp; Quality Technician",
        "identifier": {"@type": "PropertyValue", "name": "Oatly AB", "value": "8427035"},
        "datePosted": "2026-09-22T00:00:00+02:00",
        "employmentType": "FULL_TIME",
        "hiringOrganization": {"@type": "Organization", "name": "Oatly AB"},
        "validThrough": "2026-10-06 23:59:59 +0200",
        "description": "&lt;p&gt;You &lt;strong&gt;test&lt;/strong&gt; oats.&lt;/p&gt;",
        "jobLocation": [{"@type": "Place", "address": {"streetAddress": "Företagsvägen 42"}}],
    }
    if salary:
        posting["baseSalary"] = salary
    posting.update(extra_ld or {})
    badge_html = (f'<span class="inline-flex">{badge}<i class="fas fa-wifi"></i></span>' if badge else "")
    dl = "".join(f'<dt class="font-semibold">{k}</dt><dd class="mb-32">\n  {v}\n</dd>' for k, v in facts)
    return f"""<html lang="en-GB"><head>
<script type="application/ld+json">{json.dumps(posting, ensure_ascii=False)}</script>
{_MARKERS}</head><body>
<div class="relative" data-variabletextwrapper="">
  <div class="mb-32 uppercase"><span>Quality &amp; Regulatory Affairs</span><span>·</span><span>Landskrona</span>{badge_html}</div>
  <h1 class="font-company-header">Lab &amp; Quality Technician</h1>
  <h2 class="block mt-8">You work hands-on in the laboratory.</h2>
</div>
<dl class="company-links">{dl}</dl>
<h1>Related jobs</h1>
<div class="text-md mt-4"><span>Sales</span><span class="inline-flex">Hybrid<i class="fas fa-wifi"></i></span></div>
</body></html>"""


_JOB_URL = "https://careers.oatly.com/jobs/8427035-lab-quality-technician"


class TestJobPage:
    def test_onsite_badge_absent_from_the_facts_is_added(self):
        out = tt.render_teamtailor_job(_job_page(), _JOB_URL)
        assert out.startswith("# Lab & Quality Technician\n")
        assert "**Oatly AB** · Teamtailor posting 8427035" in out
        assert "> You work hands-on in the laboratory." in out
        assert "- **Department**: Quality & Regulatory Affairs" in out
        assert "- **Remote status**: Onsite" in out
        # The related-jobs card's "Hybrid" belongs to a different posting.
        assert "Hybrid" not in out
        assert "- **Closes**: 2026-10-06" in out
        assert "- **Salary**: not published" in out
        assert "You **test** oats." in out
        assert "Företagsvägen" not in out

    def test_badge_already_in_the_facts_is_not_repeated(self):
        out = tt.render_teamtailor_job(
            _job_page(badge="Fully Remote",
                      facts=(("Location", "Canada - REMOTE"), ("Remote status", "Fully Remote"))),
            _JOB_URL,
        )
        assert out.count("Fully Remote") == 1

    def test_no_badge_is_not_set_rather_than_onsite(self):
        out = tt.render_teamtailor_job(_job_page(badge=""), _JOB_URL)
        assert "- **Remote status**: not set (Teamtailor's default; the posting shows none)" in out

    def test_structured_fields(self):
        out = tt.render_teamtailor_job(
            _job_page(
                salary={"currency": "CAD", "value": {"unitText": "YEAR", "minValue": "125000", "maxValue": "145000"}},
                extra_ld={"jobLocationType": "TELECOMMUTE",
                          "applicantLocationRequirements": {"@type": "Country", "name": "Canada"}},
            ),
            _JOB_URL,
        )
        assert "- **Salary**: CAD 125,000–145,000 / year" in out
        assert "- **Employment type**: FULL_TIME" in out
        assert "- **Applicants must be in**: Canada" in out
        assert "- **Location type**: TELECOMMUTE" in out

    def test_page_without_a_posting(self):
        assert tt.render_teamtailor_job(f"<html>{_MARKERS}<h1>Hi</h1></html>", _JOB_URL) is None


class TestPreflight:
    def _run(self, html, url):
        return inspect_html_preflight(html, url, False, False, False, False)

    def test_board_page_on_a_custom_domain(self):
        got = self._run(f"<html>{_MARKERS}<ul id='jobs_list_container'></ul></html>", "https://careers.oatly.com/jobs")
        assert got.teamtailor_board is True
        assert got.teamtailor_job is None

    def test_posting_page_is_rendered_in_the_worker(self):
        got = self._run(_job_page(), _JOB_URL)
        assert got.teamtailor_board is False
        assert got.teamtailor_job.startswith("# Lab & Quality Technician")

    def test_other_sites_are_untouched(self):
        got = self._run("<html><body><h1>Jobs</h1></body></html>", "https://example.com/jobs")
        assert (got.teamtailor_board, got.teamtailor_job) == (False, None)


# ---------------------------------------------------------------------------
# Pipeline: a custom-domain board through _fetch_url_impl
# ---------------------------------------------------------------------------


async def _allow_host(hostname):
    from fetchaller.security.ssrf import HostVerdict

    return HostVerdict(hostname, False, ["93.184.216.34"])


class TestPipeline:
    @staticmethod
    def _install(monkeypatch, json_status=200):
        seen = []
        board_html = f"<html><head>{_MARKERS}</head><body><h1>Current job openings</h1></body></html>".encode()

        async def _fake_get(self, target, **kwargs):
            seen.append(target)
            path = urlparse(target).path
            if path == "/jobs":
                return _Resp(target, body=board_html)
            if path == "/jobs.json":
                if json_status != 200:
                    return _Resp(target, json_status, body=b"{}", content_type="application/json")
                return _Resp(target, data=_feed([_json_item(f"u-{i}", 100 + i, f"Job {i}") for i in range(23)]))
            if path == "/jobs.rss":
                return _Resp(target, body=_rss([_rss_item(f"u-{i}", 100 + i, f"Job {i}") for i in range(23)]),
                             content_type="application/rss+xml; charset=utf-8")
            return _Resp(target, 404)

        monkeypatch.setattr("wafer.AsyncSession.get", _fake_get)
        monkeypatch.setattr("fetchaller.tools.fetch.check_host", _allow_host)
        return seen

    async def test_custom_domain_board_is_read_from_its_feeds(self, monkeypatch):
        seen = self._install(monkeypatch)
        result = await _fetch_url_impl("https://careers.oatly.com/jobs", timeout=20)

        assert seen == [
            "https://careers.oatly.com/jobs",
            "https://careers.oatly.com/jobs.json",
            "https://careers.oatly.com/jobs.rss",
        ]
        assert "# Oatly AB — Teamtailor job board (23 open positions)" in result["content"]
        assert "**Job 22** (122)" in result["content"]

    async def test_unreadable_feed_is_said_over_the_page(self, monkeypatch):
        self._install(monkeypatch, json_status=500)
        result = await _fetch_url_impl("https://careers.oatly.com/jobs", timeout=20)

        assert result["content"].startswith("[Teamtailor job board, but its jobs.json feed could not be read.")
        assert "Current job openings" in result["content"]
