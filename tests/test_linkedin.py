"""Tests for the LinkedIn public guest job search.

Only logged-out endpoints are covered. The fixtures below are trimmed from live
logged-out responses captured 2026-07-29.
"""

import pytest

from fetchaller.linkedin.parse import (
    canonical_job_url,
    parse_job_detail,
    parse_search_fragment,
    strip_tracking,
)
from fetchaller.linkedin.render import render_job_detail, render_search_results
from fetchaller.linkedin.search import _build_params, _validate
from fetchaller.linkedin.url import extract_linkedin_job_id, is_linkedin_host

SEARCH_FRAGMENT = """
<!DOCTYPE html>
<li>
  <div class="base-card relative base-search-card job-search-card"
       data-entity-urn="urn:li:jobPosting:4445926062">
    <a class="base-card__full-link"
       href="https://ca.linkedin.com/jobs/view/software-developer-at-vbk-tech-systems-4445926062?position=1&amp;pageNum=0&amp;refId=abc&amp;trackingId=xyz"></a>
    <h3 class="base-search-card__title">Software Developer</h3>
    <h4 class="base-search-card__subtitle">
      <a href="https://ca.linkedin.com/company/vbktechsystems?trackingId=q">VBK TECH SYSTEMS</a>
    </h4>
    <span class="job-search-card__location">Toronto, Ontario, Canada</span>
    <time class="job-search-card__listdate" datetime="2026-07-27">2 days ago</time>
  </div>
</li>
<li>
  <div class="base-card base-search-card job-search-card"
       data-entity-urn="urn:li:jobPosting:4437865637">
    <a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/4437865637?x=1"></a>
    <h3 class="base-search-card__title">Software Engineer</h3>
    <h4 class="base-search-card__subtitle">CircleCI</h4>
    <span class="job-search-card__location">Toronto, Ontario, Canada</span>
    <time class="job-search-card__listdate--new" datetime="2026-07-29">5 hours ago</time>
  </div>
</li>
"""

EMPTY_FRAGMENT = "<!DOCTYPE html> <!---->"

DETAIL_FRAGMENT = """
<section class="top-card-layout">
  <a data-tracking-control-name="public_jobs_topcard-title"
     href="https://ca.linkedin.com/jobs/view/software-developer-4445926062?refId=z">
    <h2 class="top-card-layout__title">Software Developer</h2>
  </a>
  <a class="topcard__org-name-link" href="https://ca.linkedin.com/company/vbktechsystems?trackingId=q">
    VBK TECH SYSTEMS
  </a>
  <span class="topcard__flavor--bullet">Toronto, Ontario, Canada</span>
  <span class="posted-time-ago__text">2 days ago</span>
  <figcaption class="num-applicants__caption">94 applicants</figcaption>
  <div class="show-more-less-html__markup">Write, modify, integrate and test software code.</div>
  <ul>
    <li class="description__job-criteria-item">
      <h3 class="description__job-criteria-subheader">Seniority level</h3>
      <span class="description__job-criteria-text">Not Applicable</span>
    </li>
    <li class="description__job-criteria-item">
      <h3 class="description__job-criteria-subheader">Employment type</h3>
      <span class="description__job-criteria-text">Full-time</span>
    </li>
  </ul>
</section>
"""


class TestUrlRecognition:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://www.linkedin.com/jobs/view/4445926062", "4445926062"),
            ("https://www.linkedin.com/jobs/view/4445926062/", "4445926062"),
            ("https://ca.linkedin.com/jobs/view/software-developer-at-vbk-4445926062", "4445926062"),
            ("https://uk.linkedin.com/jobs/view/eng-4445926062?refId=x", "4445926062"),
            ("https://www.linkedin.com/jobs/search/?currentJobId=4445926062", "4445926062"),
            ("https://www.linkedin.com/jobs/collections/recommended/?currentJobId=123456", "123456"),
        ],
    )
    def test_job_urls_resolve(self, url, expected):
        assert extract_linkedin_job_id(url) == expected

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.linkedin.com/company/circleci",
            "https://www.linkedin.com/in/someone",
            "https://www.linkedin.com/feed/",
            "https://linkedin.com.evil.example/jobs/view/4445926062",
            "https://notlinkedin.com/jobs/view/4445926062",
            "ftp://www.linkedin.com/jobs/view/4445926062",
            "https://www.linkedin.com/jobs/view/notanumber",
            "not a url",
        ],
    )
    def test_non_job_urls_are_ignored(self, url):
        assert extract_linkedin_job_id(url) is None

    def test_lookalike_host_is_rejected(self):
        """`linkedin.com.evil.example` must not read as LinkedIn."""
        assert is_linkedin_host("linkedin.com.evil.example") is False
        assert is_linkedin_host("ca.linkedin.com") is True
        assert is_linkedin_host("LINKEDIN.COM") is True


class TestSearchParsing:
    def test_cards_are_extracted(self):
        cards = parse_search_fragment(SEARCH_FRAGMENT)
        assert len(cards) == 2
        first = cards[0]
        assert first.job_id == "4445926062"
        assert first.title == "Software Developer"
        assert first.company == "VBK TECH SYSTEMS"
        assert first.location == "Toronto, Ontario, Canada"
        assert first.posted_date == "2026-07-27"
        assert first.posted_label == "2 days ago"

    def test_new_listdate_variant_is_read(self):
        """`--new` postings use a different time class."""
        cards = parse_search_fragment(SEARCH_FRAGMENT)
        assert cards[1].posted_date == "2026-07-29"
        assert cards[1].posted_label == "5 hours ago"

    def test_company_without_a_page_still_parses(self):
        assert parse_search_fragment(SEARCH_FRAGMENT)[1].company == "CircleCI"
        assert parse_search_fragment(SEARCH_FRAGMENT)[1].company_url == ""

    def test_url_is_the_stable_canonical_not_the_tracking_one(self):
        """The returned href carries position/refId/trackingId that change every
        request, and a country subdomain that varies by caller."""
        card = parse_search_fragment(SEARCH_FRAGMENT)[0]
        assert card.url == "https://www.linkedin.com/jobs/view/4445926062"
        assert "trackingId" not in card.url
        assert "refId" not in card.url

    def test_company_url_tracking_is_stripped(self):
        card = parse_search_fragment(SEARCH_FRAGMENT)[0]
        assert card.company_url == "https://ca.linkedin.com/company/vbktechsystems"

    def test_empty_fragment_yields_nothing(self):
        assert parse_search_fragment(EMPTY_FRAGMENT) == []
        assert parse_search_fragment("") == []

    @pytest.mark.parametrize(
        "url,expected",
        [
            (
                "https://ca.linkedin.com/jobs/view/x-1?refId=a&trackingId=b#c",
                "https://ca.linkedin.com/jobs/view/x-1",
            ),
            ("https://www.linkedin.com/company/acme?trackingId=q", "https://www.linkedin.com/company/acme"),
            # Every URL we emit comes from a LinkedIn card. Anything else is
            # markup we misread or content placed to be followed.
            ("https://evil.example/a?b=1", ""),
            ("https://linkedin.com.evil.example/a", ""),
            ("javascript:alert(1)", ""),
            ("", ""),
        ],
    )
    def test_strip_tracking(self, url, expected):
        assert strip_tracking(url) == expected

    def test_canonical_url_needs_an_id(self):
        assert canonical_job_url("") == ""


class TestDetailParsing:
    def test_fields_are_extracted(self):
        detail = parse_job_detail(DETAIL_FRAGMENT, "4445926062")
        assert detail.title == "Software Developer"
        assert detail.company == "VBK TECH SYSTEMS"
        assert detail.location == "Toronto, Ontario, Canada"
        assert detail.applicants == "94 applicants"
        assert "software code" in detail.description

    def test_criteria_are_keyed_by_heading_not_position(self):
        detail = parse_job_detail(DETAIL_FRAGMENT, "4445926062")
        assert detail.criteria["Seniority level"] == "Not Applicable"
        assert detail.criteria["Employment type"] == "Full-time"

    def test_untitled_fragment_is_rejected(self):
        assert parse_job_detail("<section>nothing useful</section>") is None
        assert parse_job_detail("") is None


class TestFilterValidation:
    def test_defaults_are_valid(self):
        assert _validate("any", None, None, None, None, "relevance", 0, 10) is None

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"date_posted": "fortnight"},
            {"workplace": "moon"},
            {"experience": "wizard"},
            {"job_type": "volunteer"},
            {"min_salary": 55_000},
            {"sort": "salary"},
            {"start": -1},
            {"start": 1000},
            {"limit": 0},
            {"limit": 26},
        ],
    )
    def test_bad_values_are_refused(self, kwargs):
        base = dict(
            date_posted="any", workplace=None, experience=None, job_type=None,
            min_salary=None, sort="relevance", start=0, limit=10,
        )
        base.update(kwargs)
        assert _validate(**base) is not None

    def test_start_boundary_matches_the_endpoint(self):
        """999 is the last row LinkedIn answers; 1000 returns HTTP 400."""
        base = dict(
            date_posted="any", workplace=None, experience=None, job_type=None,
            min_salary=None, sort="relevance", limit=10,
        )
        assert _validate(start=999, **base) is None
        assert _validate(start=1000, **base) is not None

    def test_geo_id_replaces_location_rather_than_joining_it(self):
        """geoId overrides conflicting location text, so sending both is a lie
        about what was searched."""
        params = _build_params("eng", "Vancouver", "100025096", "any", None, None, None, None, 0)
        assert params["geoId"] == "100025096"
        assert "location" not in params

    def test_location_is_sent_when_no_geo_id_resolved(self):
        params = _build_params("eng", "Toronto", None, "any", None, None, None, None, 0)
        assert params["location"] == "Toronto"

    def test_filters_map_to_live_confirmed_codes(self):
        params = _build_params(
            "eng", "", "1", "week", "remote", "mid_senior", "contract", 100_000, 20
        )
        assert params["f_TPR"] == "r604800"
        assert params["f_WT"] == "2"
        assert params["f_E"] == "4"
        assert params["f_JT"] == "C"
        assert params["f_SB2"] == "24"
        assert params["start"] == 20

    def test_any_date_sends_no_window(self):
        assert "f_TPR" not in _build_params("eng", "", None, "any", None, None, None, None, 0)


class TestRendering:
    def test_search_results_render(self):
        cards = parse_search_fragment(SEARCH_FRAGMENT)
        out = render_search_results(cards, keywords="software engineer", location="Toronto")

        assert "Software Developer" in out
        assert "VBK TECH SYSTEMS" in out
        assert "https://www.linkedin.com/jobs/view/4445926062" in out
        assert "2 jobs" in out

    def test_salary_absence_is_stated_not_faked(self):
        """No salary element appeared in any sampled guest fragment, so the
        output says so rather than implying these jobs have none listed."""
        out = render_search_results(parse_search_fragment(SEARCH_FRAGMENT))
        assert "not published" in out

    def test_detail_renders_criteria_and_apply_caveat(self):
        out = render_job_detail(parse_job_detail(DETAIL_FRAGMENT, "4445926062"))
        assert "Employment type:** Full-time" in out
        assert "94 applicants" in out
        assert "requires a LinkedIn account" in out

    def test_output_respects_the_token_budget(self):
        detail = parse_job_detail(DETAIL_FRAGMENT, "4445926062")
        detail.description = "x" * 100_000
        out = render_job_detail(detail, max_tokens=500)
        assert len(out) <= 500 * 4

    def test_empty_result_set_renders_without_crashing(self):
        assert "0 jobs" in render_search_results([])


class TestPageFirstOptimization:
    """The JSERP page carries 60 cards; the fragment endpoint carries 10.

    At start=0 one page request covers any allowed limit, instead of up to
    three fragment requests at a 3.2s floor.
    """

    def test_page_url_drops_start(self):
        """The page ignores `start` — every offset returns the same first card —
        so sending it would imply pagination the endpoint does not do."""
        from fetchaller.linkedin.api import build_page_url

        url = build_page_url({"keywords": "engineer", "start": 60, "f_WT": "2"})
        assert "start=" not in url
        assert "keywords=engineer" in url
        assert "f_WT=2" in url
        assert "/jobs/search?" in url

    def test_page_capacity_exceeds_the_result_cap(self):
        """If this stops holding, start=0 needs the fragment loop again."""
        from fetchaller.linkedin.api import PAGE_CARD_CAPACITY
        from fetchaller.linkedin.search import MAX_LIMIT

        assert PAGE_CARD_CAPACITY >= MAX_LIMIT

    async def test_first_page_uses_one_request_not_three(self, monkeypatch):
        from fetchaller.linkedin import api
        from fetchaller.linkedin import search as search_mod

        page_calls, fragment_calls = [], []

        async def fake_page(params, *, session, timeout=None):
            page_calls.append(params)
            return SEARCH_FRAGMENT * 13  # 26 cards, more than the limit

        async def fake_fragment(params, *, session, timeout=None):
            fragment_calls.append(params)
            return SEARCH_FRAGMENT

        async def fake_session(browser_solver=None):
            return object()

        async def fake_geo(location, *, session, timeout=None):
            return "100025096"

        monkeypatch.setattr(api, "fetch_search_page", fake_page)
        monkeypatch.setattr(api, "fetch_search_fragment", fake_fragment)
        monkeypatch.setattr(api, "_get_session", fake_session)
        monkeypatch.setattr(api, "resolve_geo_id", fake_geo)

        result = await search_mod.search_linkedin_jobs("engineer", "Toronto", limit=2)

        assert "content" in result
        assert len(page_calls) == 1
        assert fragment_calls == [], "fragment endpoint used despite a full page"

    async def test_paginated_request_skips_the_page(self, monkeypatch):
        """The page cannot paginate, so start>0 must go straight to fragments."""
        from fetchaller.linkedin import api
        from fetchaller.linkedin import search as search_mod

        page_calls, fragment_calls = [], []

        async def fake_page(params, *, session, timeout=None):
            page_calls.append(params)
            return SEARCH_FRAGMENT

        async def fake_fragment(params, *, session, timeout=None):
            fragment_calls.append(params)
            return SEARCH_FRAGMENT

        async def fake_session(browser_solver=None):
            return object()

        monkeypatch.setattr(api, "fetch_search_page", fake_page)
        monkeypatch.setattr(api, "fetch_search_fragment", fake_fragment)
        monkeypatch.setattr(api, "_get_session", fake_session)

        await search_mod.search_linkedin_jobs("engineer", geo_id="1", start=60, limit=2)

        assert page_calls == []
        assert fragment_calls and fragment_calls[0]["start"] == 60


class TestSortIsNotServerSide:
    """LinkedIn's logged-out endpoints do not honour sortBy.

    Measured 2026-07-29: `sortBy` values R, DD, and RD, plus `f_SORT=DD`, all
    returned byte-identical results from the fragment endpoint, and the JSERP
    page returned an identical job-ID sequence for R and DD. Date order was
    never descending under any of them. `f_TPR` DID change results in the same
    session, so filters reach the backend and sort specifically does not.
    """

    def test_sort_is_not_sent_to_linkedin(self):
        params = _build_params("eng", "", "1", "any", None, None, None, None, 0)
        assert "sortBy" not in params
        assert "f_SORT" not in params

    def test_recent_sorts_locally_by_posting_date(self):
        from fetchaller.linkedin.parse import JobCard
        from fetchaller.linkedin.render import render_search_results

        cards = [
            JobCard(job_id="1", title="Older", posted_date="2026-07-01"),
            JobCard(job_id="2", title="Newest", posted_date="2026-07-29"),
            JobCard(job_id="3", title="Middle", posted_date="2026-07-15"),
        ]
        cards.sort(key=lambda card: card.posted_date or "", reverse=True)
        out = render_search_results(cards)

        assert out.index("Newest") < out.index("Middle") < out.index("Older")


class TestUntrustedTextIsInert:
    """Titles, company names and descriptions are employer-controlled and land
    in markdown headings, bold runs and list items."""

    @pytest.mark.parametrize(
        "hostile",
        [
            "[click here](http://evil.example)",
            "**bold takeover**",
            "# Injected heading",
            "back`tick`",
            "pipe|table|break",
        ],
    )
    def test_markdown_metacharacters_are_escaped(self, hostile):
        from fetchaller.linkedin.render import render_search_results

        html = f'''
        <li><div class="base-search-card" data-entity-urn="urn:li:jobPosting:123456">
          <a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/123456"></a>
          <h3 class="base-search-card__title">{hostile}</h3>
          <h4 class="base-search-card__subtitle">{hostile}</h4>
        </div></li>
        '''
        out = render_search_results(parse_search_fragment(html))

        from fetchaller.linkedin.parse import _MARKDOWN_ESCAPE

        # The text survives as readable characters, but every structural
        # metacharacter is escaped, so none of it renders as active markdown.
        assert hostile.translate(_MARKDOWN_ESCAPE) in out
        assert "](http" not in out
        assert "<img" not in out

    def test_embedded_markup_is_parsed_away_not_emitted(self):
        """A tag inside the title is markup to the parser, so it never reaches
        the text layer at all."""
        html = '''
        <li><div class="base-search-card" data-entity-urn="urn:li:jobPosting:123456">
          <h3 class="base-search-card__title"><img src=x onerror=alert(1)></h3>
        </div></li>
        '''
        card = parse_search_fragment(html)[0]

        assert card.title == ""
        assert "<img" not in card.title
        assert "onerror" not in card.title

    def test_ordinary_titles_stay_readable(self):
        html = '''
        <li><div class="base-search-card" data-entity-urn="urn:li:jobPosting:123456">
          <h3 class="base-search-card__title">Senior Engineer, Platform</h3>
          <h4 class="base-search-card__subtitle">Acme Inc.</h4>
        </div></li>
        '''
        card = parse_search_fragment(html)[0]
        assert card.title == "Senior Engineer, Platform"
        assert card.company == "Acme Inc."


class TestHiringBadges:
    """"Be an early applicant" / "Actively Hiring" are the strongest ordering
    signal a logged-out searcher gets — LinkedIn publishes no applicant count
    or salary on cards."""

    BADGED = '''
    <li><div class="base-search-card" data-entity-urn="urn:li:jobPosting:123456">
      <h3 class="base-search-card__title">Warehouse Associate</h3>
      <h4 class="base-search-card__subtitle">DoorDash</h4>
      <div class="job-posting-benefits">
        <span class="job-posting-benefits__text">Be an early applicant</span>
      </div>
    </div></li>
    '''

    def test_badge_is_extracted(self):
        assert parse_search_fragment(self.BADGED)[0].badges == ("Be an early applicant",)

    def test_badge_reaches_the_output(self):
        from fetchaller.linkedin.render import render_search_results

        assert "Be an early applicant" in render_search_results(parse_search_fragment(self.BADGED))

    def test_absent_badges_render_nothing_extra(self):
        from fetchaller.linkedin.render import render_search_results

        out = render_search_results(parse_search_fragment(SEARCH_FRAGMENT))
        assert "Be an early applicant" not in out


class TestSessionPolicy:
    def test_session_does_not_retry_or_rotate(self):
        """The stated policy is stop rather than rotate; wafer defaults to 3
        retries and 2 rotations, which is six requests under three identities
        against a rate limit."""
        import inspect

        from fetchaller.linkedin import api

        source = inspect.getsource(api._get_session)
        assert "max_retries=0" in source
        assert "max_rotations=0" in source

    @pytest.mark.parametrize("job_id", ["12345", "1" * 21, "", "abc"])
    async def test_job_id_bounds_match_url_parsing(self, job_id):
        """A URL fetch() maps and an ID passed straight to the tool must not
        disagree about what is valid."""
        from fetchaller.linkedin.search import get_linkedin_job

        result = await get_linkedin_job(job_id)
        assert "error" in result and "6-20 digits" in result["error"]


class TestApplicationFilters:
    """`f_AL` and `f_EA` are labelled by LinkedIn's own logged-out filter bar as
    "Easy Apply" and "Under 10 applicants".

    Verified 2026-07-29 against each returned posting's detail fragment:
    f_AL -> 5/5 carried an Easy Apply button and no off-site link (baseline
    0/5); f_EA -> 5/5 read "Be among the first 25 applicants" against a
    baseline of "Over 200 applicants". Unlike `sortBy`, both demonstrably
    change what comes back, which is why they are exposed and sort is not.
    """

    BASE = dict(
        date_posted="any", workplace=None, experience=None,
        job_type=None, min_salary=None, start=0,
    )

    def test_neither_filter_is_sent_by_default(self):
        params = _build_params("eng", "", "1", **self.BASE)
        assert "f_AL" not in params
        assert "f_EA" not in params

    def test_easy_apply_maps_to_f_al(self):
        params = _build_params("eng", "", "1", **self.BASE, easy_apply=True)
        assert params["f_AL"] == "true"
        assert "f_EA" not in params

    def test_low_applicant_filter_maps_to_f_ea(self):
        params = _build_params("eng", "", "1", **self.BASE, under_10_applicants=True)
        assert params["f_EA"] == "true"
        assert "f_AL" not in params

    def test_both_can_combine(self):
        params = _build_params(
            "eng", "", "1", **self.BASE, easy_apply=True, under_10_applicants=True
        )
        assert params["f_AL"] == "true"
        assert params["f_EA"] == "true"

    def test_filters_reach_the_page_url_too(self):
        """start=0 uses the JSERP page; the filters must not be dropped there."""
        from fetchaller.linkedin.api import build_page_url

        url = build_page_url(
            _build_params("eng", "", "1", **self.BASE, easy_apply=True, under_10_applicants=True)
        )
        assert "f_AL=true" in url
        assert "f_EA=true" in url


class TestRefusalStopsTheFlow:
    """A refusal from ANY LinkedIn endpoint must stop the search.

    The GEO typeahead returned None on 429, and the search then fell through to
    a plain-location query — issuing more requests against a host that had just
    told us to stop.
    """

    class _Resp:
        def __init__(self, status):
            self.status_code = status
            self.text = ""

    @pytest.mark.parametrize("status", [401, 403, 429])
    async def test_typeahead_refusal_raises_instead_of_falling_through(
        self, monkeypatch, status
    ):
        from fetchaller.linkedin import api

        calls = []

        async def _fake_get(url, session, timeout):
            calls.append(url)
            return self._Resp(status)

        monkeypatch.setattr(api, "_get", _fake_get)

        with pytest.raises(api.LinkedInBlockedError):
            await api.resolve_geo_id("Toronto", session=object(), timeout=10)
        assert len(calls) == 1

    @pytest.mark.parametrize("status", [500, 503])
    async def test_typeahead_server_error_is_not_a_refusal(self, monkeypatch, status):
        """A 5xx is LinkedIn failing, not refusing; the location falls back."""
        from fetchaller.linkedin import api

        async def _fake_get(url, session, timeout):
            return self._Resp(status)

        monkeypatch.setattr(api, "_get", _fake_get)

        assert await api.resolve_geo_id("Toronto", session=object(), timeout=10) is None

    async def test_a_refusal_surfaces_without_the_exception_text(self, monkeypatch):
        """wafer exceptions embed the request URL; the message must not carry it."""
        from fetchaller.linkedin import api
        from fetchaller.linkedin import search as search_mod

        async def _fake_session(browser_solver=None):
            return object()

        async def _blocked(*args, **kwargs):
            raise api.LinkedInBlockedError("https://secret-url.example?q=leak")

        monkeypatch.setattr(api, "_get_session", _fake_session)
        monkeypatch.setattr(api, "resolve_geo_id", _blocked)

        result = await search_mod.search_linkedin_jobs("engineer", "Toronto")

        assert "error" in result
        assert "secret-url" not in result["error"]
        assert "declined" in result["error"]


class TestOutputBoundsAreRealBounds:
    def test_zero_budget_yields_nothing_not_everything(self):
        """A non-positive budget means "no room", not "no limit"."""
        from fetchaller.linkedin.render import _truncate

        assert _truncate("x" * 1000, 0) == ""
        assert _truncate("x" * 1000, -5) == ""

    def test_caller_text_in_the_heading_is_escaped(self):
        from fetchaller.linkedin.render import render_search_results

        out = render_search_results([], keywords="[x](http://evil.example)")

        assert "](http" not in out

    def test_datetime_attribute_is_sanitised(self):
        html = (
            '<li><div class="base-search-card" data-entity-urn="urn:li:jobPosting:123456">'
            '<h3 class="base-search-card__title">Role</h3>'
            '<time class="job-search-card__listdate" datetime="[x](http://evil)">now</time>'
            "</div></li>"
        )
        card = parse_search_fragment(html)[0]

        assert "](http" not in card.posted_date
