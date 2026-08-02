"""Observation: what is recorded, and when the nudge fires.

The nudge gate is the load-bearing part. A board that server-renders its first
page issues no XHR at all, so navigating and waiting finds nothing — the data
request only appears when the front end asks for page two. Two different real
boards defeated two different lenient versions of this gate.
"""

import os

import pytest

from fetchaller.discovery.observe import (
    DATA_RESOURCE_TYPES,
    MAX_BODY_BYTES,
    MAX_EXCHANGES,
    NUDGE_SELECTORS,
    SKIP_RESOURCE_TYPES,
    Capture,
    Exchange,
    _has_own_host_data,
    registrable_domain,
)

RECORDS = '{"r":[{"id":1,"t":"a"},{"id":2,"t":"b"}]}'


def exchange(url, *, order=0, kind="fetch", status=200, body=RECORDS, phase="load"):
    return Exchange(
        order=order,
        phase=phase,
        method="GET",
        url=url,
        resource_type=kind,
        status=status,
        request_headers={},
        request_body=None,
        response_headers={"content-type": "application/json"},
        body=body,
    )


class TestRegistrableDomain:
    def test_plain_domains(self):
        assert registrable_domain("www.google.com") == "google.com"
        assert registrable_domain("google.com") == "google.com"

    def test_deep_subdomains(self):
        assert registrable_domain("explore.jobs.netflix.net") == "netflix.net"

    def test_multipart_suffixes(self):
        assert registrable_domain("jobs.example.co.uk") == "example.co.uk"

    def test_case_and_trailing_dot(self):
        assert registrable_domain("WWW.Google.COM.") == "google.com"

    def test_empty(self):
        assert registrable_domain("") == ""


class TestNudgeGate:
    def test_own_host_data_suppresses_the_nudge(self):
        # Netflix issues its listing XHR on its own host during load.
        exchanges = [exchange("https://explore.jobs.netflix.net/api/apply/v2/jobs/1/jobs")]
        assert _has_own_host_data(exchanges, "explore.jobs.netflix.net")

    def test_a_sibling_host_does_not_suppress_it(self):
        # Apple's search page issues no XHR of its own but pulls a global-header
        # payload from www.apple.com while sitting on jobs.apple.com. That
        # payload scores 12.35 — enough to suppress a registrable-domain gate
        # and leave POST /api/v1/search undiscovered forever.
        exchanges = [exchange("https://www.apple.com/api-www/global-elements/flyouts")]
        assert not _has_own_host_data(exchanges, "jobs.apple.com")

    def test_an_empty_beacon_does_not_suppress_it(self):
        # Google's results page fires two 204 beacons at www.google.com/g/collect
        # on load. They are same-host XHRs and nothing else, and counting them
        # left batchexecute undiscovered.
        exchanges = [exchange("https://www.google.com/g/collect", status=204, body="")]
        assert not _has_own_host_data(exchanges, "www.google.com")

    def test_a_non_2xx_response_does_not_suppress_it(self):
        exchanges = [exchange("https://www.google.com/api/x", status=429, body="blocked")]
        assert not _has_own_host_data(exchanges, "www.google.com")

    def test_the_navigation_document_does_not_suppress_it(self):
        # Google's SSR results page is a 1.25 MB document and no data XHR.
        exchanges = [exchange("https://www.google.com/careers", kind="document", body="x" * 1000)]
        assert not _has_own_host_data(exchanges, "www.google.com")

    def test_a_payload_with_no_record_set_does_not_suppress_it(self):
        # Meta's /jobsearch/ SSRs its results and only issues routing and
        # telemetry: bulk-route-definitions (309 distinct values, zero records)
        # and three /graphql calls of 114, 185 and 1569 bytes. A gate satisfied
        # by "a non-empty same-host payload" is satisfied by those, and
        # discovery then minimizes the routing endpoint instead of the search.
        routes = '{"payload":{"payloads":{"/jobsearch/?q=engineer":{"result":{"type":"route_definition"}}}}}'
        exchanges = [exchange("https://www.metacareers.com/ajax/bulk-route-definitions/", body=routes)]
        assert not _has_own_host_data(exchanges, "www.metacareers.com")

    def test_a_real_record_set_does_suppress_it(self):
        # Netflix, Workday and Amazon all fetch their listing on load.
        exchanges = [exchange("https://explore.jobs.netflix.net/api/apply/v2/jobs/1/jobs")]
        assert _has_own_host_data(exchanges, "explore.jobs.netflix.net")

    def test_only_the_load_phase_counts(self):
        exchanges = [exchange("https://x.example.com/api", phase="nudge")]
        assert not _has_own_host_data(exchanges, "x.example.com")


class TestSelectors:
    def test_ordered_most_specific_first(self):
        assert NUDGE_SELECTORS[0] == "[rel~=next]"
        assert "[aria-label*='next page' i]" in NUDGE_SELECTORS

    def test_are_conventions_not_site_knowledge(self):
        # Nothing here names a board.
        for selector in NUDGE_SELECTORS:
            lowered = selector.casefold()
            assert not any(
                brand in lowered for brand in ("apple", "google", "meta", "amazon", "workday")
            )


class TestResourceFiltering:
    def test_assets_and_telemetry_are_skipped(self):
        for kind in ("image", "stylesheet", "font", "media", "script", "ping", "websocket"):
            assert kind in SKIP_RESOURCE_TYPES

    def test_document_is_a_data_type(self):
        # Trap 10: an SSR board can answer the navigation with the payload.
        assert "document" in DATA_RESOURCE_TYPES

    def test_scripts_are_deliberately_not_captured(self):
        # The trade that makes Meta's doc_id a literal rather than a mint step.
        assert "script" in SKIP_RESOURCE_TYPES
        assert "script" not in DATA_RESOURCE_TYPES

    def test_caps_are_set(self):
        assert MAX_BODY_BYTES == 4 * 1024 * 1024
        assert MAX_EXCHANGES == 400


class TestPageStatus:
    def test_reports_the_navigation_documents_status(self):
        capture = Capture(
            url="https://www.metacareers.com/jobs?q=engineer",
            html="<html></html>",
            exchanges=[
                exchange(
                    "https://www.metacareers.com/jobs?q=engineer",
                    kind="document",
                    status=429,
                    body="<html>slow down</html>",
                )
            ],
        )
        # A throttled page still renders, and every exchange on it is correctly
        # rejected as non-data. Without this the outcome reads as "this board
        # has no API" rather than "you were throttled".
        assert capture.page_status == 429

    def test_zero_when_no_document_was_seen(self):
        capture = Capture(url="https://x.example.com/y", html="", exchanges=[])
        assert capture.page_status == 0

    def test_a_third_party_document_is_not_the_page(self):
        capture = Capture(
            url="https://board.example.com/careers",
            html="",
            exchanges=[exchange("https://www.recaptcha.net/anchor", kind="document", status=200)],
        )
        assert capture.page_status == 0


class TestExchangeProperties:
    def test_host_and_content_type_are_normalized(self):
        e = Exchange(
            order=0,
            phase="load",
            method="GET",
            url="https://Board.Example.COM/api",
            resource_type="fetch",
            status=200,
            request_headers={},
            request_body=None,
            response_headers={"content-type": "Application/JSON; charset=UTF-8"},
            body="{}",
        )
        assert e.host == "board.example.com"
        assert "json" in e.content_type
        assert e.is_data_type


class TestRecorderLifecycle:
    def test_stop_makes_the_recorder_ignore_further_responses(self):
        # Without this the listener stays live while drain() waits, so a late
        # response schedules a body read nobody awaits — and the browser then
        # closes underneath it.
        from fetchaller.discovery.observe import _Recorder

        recorder = _Recorder()
        recorder.stop()

        class FakeRequest:
            resource_type = "fetch"

        class FakeResponse:
            request = FakeRequest()

        recorder._on_response(FakeResponse())
        assert recorder.exchanges == []
        assert recorder._pending == set()


class TestOriginWarmUp:
    async def _warm(self, url, *, cookies=None, pages=None):
        from fetchaller.discovery.observe import _warm_origin

        visited: list[str] = []

        class FakeContext:
            async def cookies(self, _url):
                return cookies or []

        class FakePage:
            async def goto(self, target, **_k):
                visited.append(target)

            async def evaluate(self, _script):
                return 100

        import time as _t

        await _warm_origin(FakeContext(), FakePage(), url, _t.monotonic() + 30)
        return visited

    async def test_visits_the_origin_root_before_a_deep_link(self):
        # Landing straight on a deep search URL with no prior page view is not
        # how a browser session begins, and origins throttle it: measured on
        # metacareers.com, `/` answered 200 while `/jobs?q=…` was rate-limited.
        assert await self._warm("https://www.metacareers.com/jobs?q=engineer") == [
            "https://www.metacareers.com/"
        ]

    async def test_skips_when_the_target_is_already_the_root(self):
        assert await self._warm("https://www.metacareers.com/") == []

    async def test_skips_when_the_profile_has_already_been_here(self):
        cookies = [{"name": "datr", "value": "x"}]
        assert await self._warm("https://x.example.com/deep?q=1", cookies=cookies) == []

    async def test_the_root_is_derived_not_hardcoded(self):
        # No board is named anywhere in the warm-up path.
        assert await self._warm("https://careers.example.co.uk/search?q=a") == [
            "https://careers.example.co.uk/"
        ]

    async def test_a_url_with_no_host_is_left_alone(self):
        assert await self._warm("not-a-url") == []


class TestPersistentProfile:
    def test_the_profile_path_is_stable(self):
        from fetchaller.discovery.observe import default_profile_dir

        # A blank profile every pass means the origin sees a brand-new
        # anonymous browser each time — the visitor most likely to be throttled.
        assert default_profile_dir() == default_profile_dir()
        assert default_profile_dir().endswith("browser-profile")

    def test_a_redirect_hop_is_not_the_pages_outcome(self):
        # Meta redirects /jobs?q=… to /jobsearch/?q=…. Reporting the first
        # document's status called a perfectly good capture "HTTP 301".
        capture = Capture(
            url="https://www.metacareers.com/jobsearch/?q=engineer",
            html="<html></html>",
            exchanges=[
                exchange("https://www.metacareers.com/jobs?q=engineer",
                         order=0, kind="document", status=301, body=""),
                exchange("https://www.metacareers.com/jobsearch/?q=engineer",
                         order=1, kind="document", status=200, body="<html>ok</html>"),
            ],
        )
        assert capture.page_status == 200

    def test_a_redirect_with_no_landing_still_reports_something(self):
        capture = Capture(
            url="https://x.example.com/a",
            html="",
            exchanges=[exchange("https://x.example.com/a", kind="document", status=301, body="")],
        )
        assert capture.page_status == 301


class TestChallengedExchanges:
    def test_a_refused_data_request_is_detected(self):
        # Uber's board renders fine, then prefetches each posting; Cloudflare
        # answers every one with 403 "Just a moment...". Without checking per
        # exchange the board looks like it simply has no data endpoint.
        from fetchaller.discovery.observe import challenged_exchanges

        interstitial = '<!DOCTYPE html><html><head><title>Just a moment...</title>'
        exchanges = [
            exchange("https://jobs.uber.com/en/jobs/", kind="document", status=200, body="<html>ok</html>"),
            exchange("https://jobs.uber.com/en/jobs/300543/?_rsc=1tnrg", status=403, body=interstitial),
            exchange("https://jobs.uber.com/en/jobs/300886/?_rsc=1tnrg", status=403, body=interstitial),
        ]
        blocked = challenged_exchanges(exchanges, "jobs.uber.com")
        assert len(blocked) == 2
        assert all(b.status == 403 for b in blocked)

    def test_a_healthy_board_reports_nothing_blocked(self):
        from fetchaller.discovery.observe import challenged_exchanges

        exchanges = [exchange("https://explore.jobs.netflix.net/api/apply/v2/jobs/1/jobs")]
        assert challenged_exchanges(exchanges, "explore.jobs.netflix.net") == []

    def test_an_ordinary_403_without_an_interstitial_is_not_a_challenge(self):
        # A plain authorization failure is not bot protection.
        from fetchaller.discovery.observe import challenged_exchanges

        exchanges = [exchange("https://x.example.com/api", status=403, body='{"error":"forbidden"}')]
        assert challenged_exchanges(exchanges, "x.example.com") == []


class TestLaunchHardening:
    """The capture browser must not announce itself.

    A bare ``headless=True`` launch leaves ``HeadlessChrome/…`` in the user
    agent and ``--enable-automation`` on the command line. That alone earned
    Meta's rate limiter and Cloudflare's challenge on Uber's prefetches — and
    because a flagged browser returns *degraded* answers rather than errors,
    both were recorded as facts about those boards. Three wrong verdicts came
    out of it before anyone looked at the user agent.
    """

    def test_wafers_config_strips_the_automation_signals(self):
        from wafer.browser import hardened_launch_config

        config = hardened_launch_config(headless=True)
        # Consumed from wafer, never copied here, so a Chrome bump reaches us.
        assert "--enable-automation" in config.ignore_default_args
        assert "--headless" in config.ignore_default_args
        assert "--headless=new" in config.args

    def test_scrubbing_keeps_the_version_truthful(self):
        from wafer.browser import scrub_headless_ua

        raw = "Mozilla/5.0 (Macintosh) HeadlessChrome/147.0.7727.15 Safari/537.36"
        scrubbed = scrub_headless_ua(raw)
        assert "Headless" not in scrubbed
        assert "147.0.7727.15" in scrubbed  # composed agents lie; scrubbed ones do not


@pytest.mark.skipif(
    os.environ.get("FETCHALLER_RUN_BROWSER_CANARY") != "1",
    reason="set FETCHALLER_RUN_BROWSER_CANARY=1 to launch a real browser",
)
async def test_capture_browser_does_not_report_headless():
    """The canary that would have caught the entire misdiagnosis."""
    from patchright.async_api import async_playwright

    from fetchaller.discovery.observe import _open

    async with async_playwright() as pw:
        browser, context, _config = await _open(pw, headless=True, profile_dir=None)
        try:
            page = await context.new_page()
            agent = await page.evaluate("navigator.userAgent")
            assert "Headless" not in agent, agent
            assert await page.evaluate("navigator.webdriver") is not True
        finally:
            await (browser.close() if browser is not None else context.close())
