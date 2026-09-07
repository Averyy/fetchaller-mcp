"""jobbank.gc.ca: location resolution, result parsing, and rendering.

Each parsing test corresponds to a defect the live board actually produced.
"""

from __future__ import annotations

import json

import pytest

from fetchaller import server
from fetchaller.jobbank import api, render, search

RESULTS_HTML = """
<html><body>
<h1>Available jobs near St. Catharines (ON) - Search</h1>
<span class="found" id="results-count">1,036</span>
<section>
  <h3>Distance from St. Catharines (ON)</h3>
  <ul class="list-group">
    <li><a href="?d=10&amp;mid=22415"><span class="badge">152 <span class="wb-inv">jobs found in</span></span> 10km</a></li>
    <li><a href="?d=25&amp;mid=22415"><span class="badge">424 <span class="wb-inv">jobs found in</span></span> 25km</a></li>
  </ul>
</section>
<article id="article-50045614" class="action-buttons"><a href="/jobsearch/jobposting/50045614;jsessionid=ABC123.jobsearch75?source=searchresults" class="resultJobItem">
  <h3 class="title">
    <span class="flag"><span class="new">New</span><span class="distance">1 - 3 km</span><span class="telework">On site</span></span>
    <span class="job-source"><span class="wb-inv">Job Bank</span></span>
    <span class="noctitle"> material handler
    </span>
  </h3>
  <ul class="list-unstyled">
    <li class="date">August 10, 2026</li>
    <li class="business">Steelcon</li>
    <li class="location"><span class="fas fa-map-marker-alt"></span> <span class="wb-inv">Location</span>
        St. Catharines (ON)</li>
    <li class="salary"><span class="fa fa-dollar"></span>
        Salary
        $23.00 hourly</li>
    <li class="source"><span class="wb-inv">Job number:</span><span class="fa fa-hashtag"></span> 3642935</li>
  </ul></a></article>
<article id="article-49974098"><a href="/jobsearch/jobposting/49974098?source=searchresults" class="resultJobItem">
  <h3 class="title"><span class="flag"><span class="telework">Remote</span></span>
    <span class="noctitle">driver, truck</span></h3>
  <ul class="list-unstyled">
    <li class="date">July 29, 2026</li>
    <li class="business">Trillium Floral</li>
    <li class="location"><span class="wb-inv">Location</span> Thorold (ON)</li>
    <li class="salary"><span class="wb-inv">Salary</span> Salary $55,000.00 to $90,000.00 annually</li>
  </ul></a></article>
</body></html>
"""

SUGGEST_JSON = json.dumps(
    {
        "response": {
            "docs": [
                {"name": "St. Catharines", "city_id": "22415", "province_cd": "ON"},
                {"name": "St. Catharines South", "city_id": "99999", "province_cd": "ON"},
            ]
        }
    }
)

AMBIGUOUS_JSON = json.dumps(
    {
        "response": {
            "docs": [
                {"name": "London", "city_id": "1111", "province_cd": "ON"},
                {"name": "London", "city_id": "2222", "province_cd": "NB"},
            ]
        }
    }
)


class FakeSession:
    def __init__(self, body):
        self.body = body
        self.urls = []

    async def get(self, url, **kwargs):
        from types import SimpleNamespace

        self.urls.append(url)
        return SimpleNamespace(status_code=200, text=self.body)


@pytest.fixture
def no_rate_limit(monkeypatch):
    async def instant(*args, **kwargs):
        return None

    monkeypatch.setattr(api.jobbank_limiter, "wait", instant)


class TestResultCount:
    def test_reads_the_results_total_not_a_facet_badge(self):
        # Every distance facet renders "<badge>152 <wb-inv>jobs found in</wb-inv>"
        # so a "N jobs found" text match returns whichever facet came first —
        # 152 for the 10km option on a search whose real total is 1,036.
        assert api.board_total(RESULTS_HTML) == 1036

    def test_missing_count_is_zero_not_a_crash(self):
        assert api.board_total("<html><body>nothing</body></html>") == 0

    def test_a_200_without_the_result_container_is_not_an_empty_search(self):
        with pytest.raises(api.JobBankUnrecognisedPageError):
            api.parse_search_page("<html><body>Sign in to continue</body></html>")

    def test_a_recognised_zero_result_page_is_empty(self):
        jobs, total = api.parse_search_page(
            '<html><body><span id="results-count">0</span></body></html>'
        )
        assert jobs == []
        assert total == 0

    def test_a_positive_total_without_cards_is_not_reported_as_empty(self):
        with pytest.raises(api.JobBankUnrecognisedPageError):
            api.parse_search_page(
                '<html><body><span id="results-count">12</span></body></html>'
            )


class TestParseResults:
    def test_finds_every_posting(self):
        jobs = api.parse_results(RESULTS_HTML)
        assert [j["job_id"] for j in jobs] == ["50045614", "49974098"]

    def test_fields_are_populated(self):
        job = api.parse_results(RESULTS_HTML)[0]
        assert job["title"] == "material handler"
        assert job["employer"] == "Steelcon"
        assert job["location"] == "St. Catharines (ON)"
        assert job["salary"] == "$23.00 hourly"
        assert job["posted"] == "August 10, 2026"
        assert job["workplace"] == "On site"

    def test_screen_reader_labels_are_not_part_of_the_value(self):
        # "Location", "Salary" and "Job number:" are wb-inv spans sharing the
        # markup with the values they label.
        for job in api.parse_results(RESULTS_HTML):
            assert not job["location"].startswith("Location")
            assert "Job number" not in job["salary"]
            assert not job["salary"].startswith("Salary")

    def test_session_id_never_leaks_into_a_shared_url(self):
        # The href carries ;jsessionid= and ?source=; a link handed to a user
        # must not carry someone else's session.
        for job in api.parse_results(RESULTS_HTML):
            assert "jsessionid" not in job["url"]
            assert "source=" not in job["url"]
            assert job["url"].startswith("https://www.jobbank.gc.ca/jobsearch/jobposting/")

    def test_remote_flag_is_captured(self):
        jobs = api.parse_results(RESULTS_HTML)
        assert jobs[1]["workplace"] == "Remote"

    def test_unterminated_article_openers_do_not_stall(self):
        # A lazy article regex restarted its bounded scan at every opener.
        import time

        started = time.monotonic()
        assert api.parse_results('<article id="article-1"' * 20_000) == []
        assert time.monotonic() - started < 2.0


class TestLocationResolution:
    @pytest.mark.asyncio
    async def test_exact_name_wins_over_a_longer_prefix_match(self, no_rate_limit):
        city = await api.resolve_city(FakeSession(SUGGEST_JSON), "St. Catharines, ON")
        assert city["city_id"] == "22415"

    @pytest.mark.asyncio
    async def test_province_disambiguates(self, no_rate_limit):
        city = await api.resolve_city(FakeSession(AMBIGUOUS_JSON), "London, NB")
        assert city["city_id"] == "2222"

    @pytest.mark.asyncio
    async def test_a_province_that_matches_nothing_returns_nothing(self, no_rate_limit):
        # "Toronto, BC" must not quietly resolve to Toronto, Ontario.
        city = await api.resolve_city(FakeSession(AMBIGUOUS_JSON), "London, AB")
        assert city is None

    @pytest.mark.asyncio
    async def test_unparseable_response_is_none_not_a_crash(self, no_rate_limit):
        assert await api.resolve_city(FakeSession("not json"), "Anywhere") is None

    def test_province_suffix_is_split_off(self):
        assert api._split_place("St. Catharines, ON") == ("St. Catharines", "ON")
        assert api._split_place("Toronto") == ("Toronto", "")
        # A comma that is not a province must stay part of the name.
        assert api._split_place("Sault Ste. Marie, Algoma")[1] == ""


class TestSearchRequest:
    @pytest.mark.asyncio
    async def test_location_is_only_sent_with_its_city_id(self, no_rate_limit):
        # The bare place name is accepted and ignored; sending it alone would
        # look like a filter and return the whole country.
        session = FakeSession(RESULTS_HTML)
        await api.search_page(session=session, city_id="", location_label="St. Catharines, ON")
        assert "locationstring" not in session.urls[0]
        assert "locationparam" not in session.urls[0]

    @pytest.mark.asyncio
    async def test_city_id_and_radius_are_sent_together(self, no_rate_limit):
        session = FakeSession(RESULTS_HTML)
        await api.search_page(
            session=session, city_id="22415", location_label="St. Catharines, ON", radius_km=25
        )
        assert "locationparam=22415" in session.urls[0]
        assert "d=25" in session.urls[0]

    @pytest.mark.asyncio
    async def test_paging_uses_the_page_parameter(self, no_rate_limit):
        session = FakeSession(RESULTS_HTML)
        await api.search_page(session=session, city_id="22415", page=3)
        assert "page=3" in session.urls[0]

    @pytest.mark.asyncio
    async def test_first_page_omits_the_page_parameter(self, no_rate_limit):
        session = FakeSession(RESULTS_HTML)
        await api.search_page(session=session, city_id="22415", page=1)
        assert "page=" not in session.urls[0]


class TestDroppedKeyword:
    """`searchstring` filters for most terms and is silently dropped for some."""

    @pytest.mark.asyncio
    async def test_a_keyword_the_board_ignored_is_reported(self, no_rate_limit, monkeypatch):
        # Measured: "assistant" within 25 km of St. Catharines returned 424 —
        # the same count and the same first five titles as no keyword at all.
        # The board's figure must not be reported for a query it never ran.
        async def fake_search_all(**kwargs):
            return api.parse_results(RESULTS_HTML), 424, True

        async def fake_unfiltered(**kwargs):
            return 424

        async def fake_resolve(session, location):
            return {"name": "St. Catharines", "city_id": "22415", "province_cd": "ON"}

        async def fake_session(*a, **k):
            return FakeSession(RESULTS_HTML)

        monkeypatch.setattr(api, "search_all", fake_search_all)
        monkeypatch.setattr(api, "unfiltered_total", fake_unfiltered)
        monkeypatch.setattr(api, "resolve_city", fake_resolve)
        monkeypatch.setattr(api, "_get_session", fake_session)

        result = await search.search_jobbank(
            title="assistant", location="St. Catharines, ON", strict_title=False
        )
        assert "ignored the keyword" in result["content"]
        # The board's 424 must not be presented as the count for this query.
        assert "Job Bank has 424" not in result["content"]

    @pytest.mark.asyncio
    async def test_a_keyword_the_board_honoured_is_left_alone(self, no_rate_limit, monkeypatch):
        async def fake_search_all(**kwargs):
            return api.parse_results(RESULTS_HTML), 33, True

        async def fake_unfiltered(**kwargs):
            return 424

        async def fake_resolve(session, location):
            return {"name": "St. Catharines", "city_id": "22415", "province_cd": "ON"}

        async def fake_session(*a, **k):
            return FakeSession(RESULTS_HTML)

        monkeypatch.setattr(api, "search_all", fake_search_all)
        monkeypatch.setattr(api, "unfiltered_total", fake_unfiltered)
        monkeypatch.setattr(api, "resolve_city", fake_resolve)
        monkeypatch.setattr(api, "_get_session", fake_session)

        result = await search.search_jobbank(
            title="driver", location="St. Catharines, ON", strict_title=False
        )
        assert "ignored the keyword" not in result["content"]

    @pytest.mark.asyncio
    async def test_no_extra_request_when_no_title_was_given(self, no_rate_limit, monkeypatch):
        calls = {"n": 0}

        async def fake_search_all(**kwargs):
            return [], 424, True

        async def fake_unfiltered(**kwargs):
            calls["n"] += 1
            return 424

        async def fake_resolve(session, location):
            return {"name": "St. Catharines", "city_id": "22415", "province_cd": "ON"}

        async def fake_session(*a, **k):
            return FakeSession(RESULTS_HTML)

        monkeypatch.setattr(api, "search_all", fake_search_all)
        monkeypatch.setattr(api, "unfiltered_total", fake_unfiltered)
        monkeypatch.setattr(api, "resolve_city", fake_resolve)
        monkeypatch.setattr(api, "_get_session", fake_session)

        await search.search_jobbank(location="St. Catharines, ON")
        assert calls["n"] == 0

    @pytest.mark.asyncio
    async def test_title_only_search_still_checks_for_an_ignored_keyword(
        self, no_rate_limit, monkeypatch
    ):
        calls = []

        async def fake_search_all(**kwargs):
            return api.parse_results(RESULTS_HTML), 64_017, False

        async def fake_unfiltered(**kwargs):
            calls.append(kwargs)
            return 64_017

        async def fake_session(*args, **kwargs):
            return FakeSession(RESULTS_HTML)

        monkeypatch.setattr(api, "search_all", fake_search_all)
        monkeypatch.setattr(api, "unfiltered_total", fake_unfiltered)
        monkeypatch.setattr(api, "_get_session", fake_session)

        result = await search.search_jobbank(title="assistant", strict_title=False)

        assert len(calls) == 1
        assert calls[0]["city_id"] == ""
        assert "ignored the keyword" in result["content"]
        assert "national result set" in result["content"]

    @pytest.mark.asyncio
    async def test_suppressed_total_does_not_claim_the_examined_window_was_all(
        self, no_rate_limit, monkeypatch
    ):
        async def fake_search_all(**kwargs):
            return api.parse_results(RESULTS_HTML), 424, False

        async def fake_unfiltered(**kwargs):
            return 424

        async def fake_resolve(session, location):
            return {"name": "St. Catharines", "city_id": "22415", "province_cd": "ON"}

        async def fake_session(*args, **kwargs):
            return FakeSession(RESULTS_HTML)

        monkeypatch.setattr(api, "search_all", fake_search_all)
        monkeypatch.setattr(api, "unfiltered_total", fake_unfiltered)
        monkeypatch.setattr(api, "resolve_city", fake_resolve)
        monkeypatch.setattr(api, "_get_session", fake_session)

        result = await search.search_jobbank(
            title="driver", location="St. Catharines, ON"
        )

        assert "All 2 postings" not in result["content"]
        assert "first 2 postings returned" in result["content"]


class TestUnresolvedLocation:
    @pytest.mark.asyncio
    async def test_unresolved_location_is_reported_and_filtered_locally(
        self, no_rate_limit, monkeypatch
    ):
        # The board ignores a place name it cannot resolve and answers with the
        # whole country. Saying so beats 64,000 postings under a city heading.
        async def fake_search_all(**kwargs):
            assert kwargs.get("city_id") == "", "must not send an unresolved location"
            return api.parse_results(RESULTS_HTML), 64017, False

        async def fake_resolve(session, location):
            return None

        async def fake_session(*a, **k):
            return FakeSession(RESULTS_HTML)

        monkeypatch.setattr(api, "search_all", fake_search_all)
        monkeypatch.setattr(api, "resolve_city", fake_resolve)
        monkeypatch.setattr(api, "_get_session", fake_session)

        result = await search.search_jobbank(location="Nowhereville, ZZ")
        assert "did not match a city Job Bank knows" in result["content"]


class TestRadius:
    def test_snaps_to_a_radius_the_board_offers(self):
        assert search._nearest_radius(10) == 10
        assert search._nearest_radius(30) == 25
        assert search._nearest_radius(9999) == 500

    def test_default_matches_the_boards_own(self):
        assert search.DEFAULT_RADIUS_KM == 50


class TestRender:
    def test_radius_is_reported_as_part_of_the_scope(self):
        # A St. Catharines search legitimately returns Thorold postings; saying
        # "within 25 km" is what makes that informative rather than broken.
        out = render.render_search_results(
            api.parse_results(RESULTS_HTML),
            location="St. Catharines, ON",
            radius_km=25,
            board_total=424,
            title_filtered=3,
            examined=5,
        )
        assert "within 25 km of St. Catharines" in out

    def test_salary_is_shown(self):
        out = render.render_search_results(api.parse_results(RESULTS_HTML))
        assert "$23.00 hourly" in out

    def test_empty_result_says_so(self):
        assert "No matching postings." in render.render_search_results([])

    def test_notes_are_surfaced(self):
        out = render.render_search_results([], notes=["no location filter was applied"])
        assert "no location filter was applied" in out


class TestToolSurface:
    def test_requires_a_title_or_a_location(self):
        error = server._validate_tool_arguments("search_jobbank", {})
        assert error and "at least one of" in error
        assert server._validate_tool_arguments("search_jobbank", {"title": "driver"}) is None

    def test_radius_is_bounded(self):
        assert server._validate_tool_arguments("search_jobbank", {"title": "x", "radius_km": 25}) is None
        assert server._validate_tool_arguments("search_jobbank", {"title": "x", "radius_km": 5}) is not None
        assert server._validate_tool_arguments("search_jobbank", {"title": "x", "radius_km": 900}) is not None


class TestRadiusOnEveryPage:
    @pytest.mark.asyncio
    async def test_pagination_keeps_the_radius(self, no_rate_limit):
        """Regression: `d` was sent on page 1 only.

        Live proof at the time: page 2 with `d=100` reported 10,694 and without
        it 1,102 — the 50km default. So pages 2+ silently sampled a different
        slice, filled the pool with out-of-radius postings, and `complete`
        flipped true against a first-page total.
        """
        urls: list[str] = []

        class Session:
            async def get(self, url, **kwargs):
                from types import SimpleNamespace

                urls.append(url)
                # Keep yielding fresh ids so the loop pages more than once.
                n = len(urls)
                body = RESULTS_HTML.replace("50045614", f"9{n:06d}").replace(
                    "49974098", f"8{n:06d}"
                )
                return SimpleNamespace(status_code=200, text=body)

        await api.search_all(
            session=Session(), city_id="22415", radius_km=100, max_records=6
        )
        assert len(urls) > 1, "needed more than one page to prove the point"
        assert all("d=100" in url for url in urls), urls


class TestPaginationStopsAtReportedTotal:
    @pytest.mark.asyncio
    async def test_complete_first_page_does_not_fetch_an_empty_second_page(
        self, no_rate_limit
    ):
        urls: list[str] = []
        body = RESULTS_HTML.replace("1,036", "2")

        class Session:
            async def get(self, url, **kwargs):
                from types import SimpleNamespace

                urls.append(url)
                return SimpleNamespace(status_code=200, text=body)

        jobs, total, complete = await api.search_all(
            session=Session(), max_records=200
        )

        assert len(jobs) == total == 2
        assert complete is True
        assert len(urls) == 1
