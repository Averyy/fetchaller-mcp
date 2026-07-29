"""Tests for the fetch tool's method / headers / body options.

Plenty of job boards and search backends (Getro, Algolia, GraphQL gateways)
answer only to POST. Their HTML looks static and fetches fine, so a GET-only
fetcher reports an empty board on a site that appears to work.
"""

import pytest

from fetchaller.tools.browse_reddit import close_session as close_reddit_session
from fetchaller.tools.fetch import (
    _CREDENTIAL_HEADERS,
    ALLOWED_METHODS,
    _fetch_url_impl,
    default_content_type,
    validate_request_body,
    validate_request_headers,
    validate_request_method,
)


class TestMethodValidation:
    def test_default_is_get(self):
        assert validate_request_method(None) == ("GET", None)

    def test_case_and_whitespace_are_normalized(self):
        assert validate_request_method(" post ") == ("POST", None)

    @pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE", "TRACE", "CONNECT"])
    def test_mutating_and_exotic_verbs_are_refused(self, method):
        """This tool fetches. PUT/PATCH/DELETE have no retrieval use at all —
        their only effect is to mutate someone else's state."""
        chosen, error = validate_request_method(method)
        assert chosen is None
        assert "Unsupported method" in error

    def test_allowed_set_is_exactly_get_and_post(self):
        assert ALLOWED_METHODS == {"GET", "POST"}

    @pytest.mark.parametrize("value", [1, [], {}, True])
    def test_non_strings_are_refused(self, value):
        assert validate_request_method(value)[0] is None


class TestHeaderValidation:
    def test_none_yields_empty_mapping(self):
        assert validate_request_headers(None) == ({}, None)

    def test_names_are_lowercased(self):
        headers, error = validate_request_headers({"Accept": "application/json"})
        assert error is None
        assert headers == {"accept": "application/json"}

    @pytest.mark.parametrize(
        "value",
        ["a\r\nInjected: 1", "a\nInjected: 1", "a\r\n", "bad\x00value", "x\x7f"],
    )
    def test_control_characters_cannot_inject_a_second_header(self, value):
        assert validate_request_headers({"X-Test": value})[0] is None

    def test_tab_is_legal_inside_a_value(self):
        headers, error = validate_request_headers({"X-Test": "a\tb"})
        assert error is None
        assert headers == {"x-test": "a\tb"}

    @pytest.mark.parametrize("name", ["Bad Name", "bad:name", "", "a" * 129, "x\ny"])
    def test_invalid_field_names_are_refused(self, name):
        assert validate_request_headers({name: "v"})[0] is None

    @pytest.mark.parametrize(
        "name",
        [
            "Host",
            "host",
            "HOST",
            "Content-Length",
            "Transfer-Encoding",
            "Connection",
            "Upgrade",
            "TE",
            "Trailer",
            "Expect",
            "Keep-Alive",
            "Proxy-Connection",
            "Proxy-Authorization",
        ],
    )
    def test_transport_controlled_headers_are_refused(self, name):
        """Framing and connection headers decide how the request is parsed, and
        `host` decides which virtual host is addressed — which would sidestep
        the SSRF pin."""
        headers, error = validate_request_headers({name: "x"})
        assert headers is None
        assert "controlled by the transport" in error

    def test_casing_cannot_smuggle_a_duplicate(self):
        assert validate_request_headers({"Accept": "a", "accept": "b"})[0] is None

    def test_count_is_bounded(self):
        assert validate_request_headers({f"x-{n}": "v" for n in range(33)})[0] is None

    def test_value_length_is_bounded(self):
        assert validate_request_headers({"x-big": "v" * 8193})[0] is None

    @pytest.mark.parametrize("headers", ["nope", 5, [], {"x": 1}, {1: "x"}])
    def test_wrong_shapes_are_refused(self, headers):
        assert validate_request_headers(headers)[0] is None


class TestBodyValidation:
    def test_body_requires_post(self):
        chosen, error = validate_request_body("GET", '{"a":1}')
        assert chosen is None
        assert "only supported with POST" in error

    def test_post_accepts_a_body(self):
        assert validate_request_body("POST", '{"a":1}') == ('{"a":1}', None)

    def test_empty_body_is_treated_as_absent(self):
        assert validate_request_body("GET", "") == (None, None)

    def test_size_is_bounded(self):
        chosen, error = validate_request_body("POST", "y" * (1024 * 1024 + 1))
        assert chosen is None
        assert "exceeds" in error

    def test_multibyte_size_is_measured_in_bytes(self):
        """A megabyte of astral characters is four megabytes on the wire."""
        chosen, _ = validate_request_body("POST", "\U0001f600" * 300_000)
        assert chosen is None

    @pytest.mark.parametrize("value", [1, [], {"a": 1}])
    def test_non_strings_are_refused(self, value):
        assert validate_request_body("POST", value)[0] is None


class TestDefaultContentType:
    def test_json_body_is_labelled_json(self):
        assert default_content_type('{"hitsPerPage":100}') == "application/json"

    def test_json_scalar_still_counts(self):
        assert default_content_type("123") == "application/json"

    def test_non_json_falls_back_to_text(self):
        assert default_content_type("a=1&b=2") == "text/plain; charset=utf-8"


class TestRequestPlumbing:
    """The validated request must actually reach the transport."""

    async def test_post_sends_method_body_and_headers(self, monkeypatch):
        seen = {}

        async def _fake_request(self, method, url, **kwargs):
            seen["method"] = method
            seen["url"] = url
            seen["body"] = kwargs.get("body")
            seen["headers"] = kwargs.get("headers")
            return _JsonResponse(url)

        monkeypatch.setattr("wafer.AsyncSession.request", _fake_request)
        monkeypatch.setattr(
            "fetchaller.tools.fetch.check_host",
            _allow_host,
        )

        result = await _fetch_url_impl(
            "https://api.example.com/search/jobs",
            method="POST",
            headers={"Accept": "application/json"},
            body='{"hitsPerPage":1}',
            timeout=10,
        )

        assert seen["method"] == "POST"
        assert seen["body"] == '{"hitsPerPage":1}'
        assert seen["headers"]["accept"] == "application/json"
        # Supplied for the caller, who did not set it.
        assert seen["headers"]["content-type"] == "application/json"
        assert result["content_type"] == "json"

    async def test_caller_content_type_is_not_overridden(self, monkeypatch):
        seen = {}

        async def _fake_request(self, method, url, **kwargs):
            seen.update(kwargs.get("headers") or {})
            return _JsonResponse(url)

        monkeypatch.setattr("wafer.AsyncSession.request", _fake_request)
        monkeypatch.setattr("fetchaller.tools.fetch.check_host", _allow_host)

        await _fetch_url_impl(
            "https://api.example.com/x",
            method="POST",
            body="a=1",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )

        assert seen["content-type"] == "application/x-www-form-urlencoded"

    async def test_post_bypasses_the_response_cache(self, monkeypatch):
        """The cache is keyed by URL alone, so a POST would serve and poison the
        GET entry for the same address."""

        class _Cache:
            def __init__(self):
                self.reads = []
                self.writes = []

            def get(self, key):
                self.reads.append(key)
                return None

            def set(self, key, *args, **kwargs):
                self.writes.append(key)

        cache = _Cache()

        async def _fake_request(self, method, url, **kwargs):
            return _JsonResponse(url)

        monkeypatch.setattr("wafer.AsyncSession.request", _fake_request)
        monkeypatch.setattr("fetchaller.tools.fetch.check_host", _allow_host)

        await _fetch_url_impl(
            "https://api.example.com/x",
            method="POST",
            body="{}",
            timeout=10,
            cache=cache,
        )

        assert cache.reads == []
        assert cache.writes == []

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.aliexpress.com/item/1005.html",
            "https://www.alibaba.com/product-detail/foo_1005.html",
            "https://www.aliexpress.com/w/wholesale-widget.html",
            "https://boards.greenhouse.io/example",
            "https://jobs.ashbyhq.com/example",
            "https://jobs.lever.co/example",
            "https://example.bamboohr.com/careers",
            "https://www.reddit.com/r/python/",
            "https://github.com/owner/repo/blob/main/README.md",
            "https://wellfound.com/jobs",
            "https://www.kijiji.ca/b-city/k0l1700273",
        ],
    )
    async def test_post_skips_every_site_interceptor(self, monkeypatch, url):
        """Every interceptor answers a URL pattern by issuing its OWN request
        against a structured API, which would silently discard the caller's
        method and body and hand back a GET's answer to a POST.

        Asserted at the transport: an interceptor that fires is visible either
        as a session.get() or as its own outbound call, and neither may happen.
        """
        seen = []

        async def _fake_request(self, method, target, **kwargs):
            seen.append((method, target))
            return _JsonResponse(target)

        async def _fake_get(self, target, **kwargs):
            seen.append(("GET", target))
            return _JsonResponse(target)

        async def _no_wait():
            return None

        monkeypatch.setattr("wafer.AsyncSession.request", _fake_request)
        monkeypatch.setattr("wafer.AsyncSession.get", _fake_get)
        monkeypatch.setattr("fetchaller.tools.fetch.check_host", _allow_host)
        # Reddit reaches the transport through two pieces of process-global
        # state: a shared rate limiter another test may have deferred, and a
        # cached session another test may have replaced with its own mock.
        # Either one would keep this case away from the transport entirely and
        # let the assertion below pass for the wrong reason.
        monkeypatch.setattr("fetchaller.ratelimit.reddit_limiter.wait", _no_wait)
        await close_reddit_session()

        await _fetch_url_impl(url, method="POST", body="{}", timeout=10)

        assert seen == [("POST", url)], f"interceptor fired for {url}: {seen}"

    async def test_get_still_uses_get(self, monkeypatch):
        seen = {}

        async def _fake_get(self, url, **kwargs):
            seen["url"] = url
            return _JsonResponse(url)

        monkeypatch.setattr("wafer.AsyncSession.get", _fake_get)
        monkeypatch.setattr("fetchaller.tools.fetch.check_host", _allow_host)

        await _fetch_url_impl("https://api.example.com/x", timeout=10)

        assert seen["url"] == "https://api.example.com/x"


class TestRedirectSemantics:
    async def test_303_downgrades_post_to_a_bodyless_get(self, monkeypatch):
        calls = []

        async def _fake_request(self, method, url, **kwargs):
            calls.append(("request", method, kwargs.get("body")))
            return _Redirect("https://api.example.com/result", 303)

        async def _fake_get(self, url, **kwargs):
            calls.append(("get", "GET", kwargs.get("body")))
            return _JsonResponse(url)

        monkeypatch.setattr("wafer.AsyncSession.request", _fake_request)
        monkeypatch.setattr("wafer.AsyncSession.get", _fake_get)
        monkeypatch.setattr("fetchaller.tools.fetch.check_host", _allow_host)

        await _fetch_url_impl(
            "https://api.example.com/search",
            method="POST",
            body="{}",
            timeout=10,
        )

        assert calls[0][1] == "POST"
        assert calls[1] == ("get", "GET", None)

    async def test_307_replays_method_and_body(self, monkeypatch):
        calls = []

        async def _fake_request(self, method, url, **kwargs):
            calls.append((method, kwargs.get("body")))
            if len(calls) == 1:
                return _Redirect("https://api.example.com/result", 307)
            return _JsonResponse(url)

        monkeypatch.setattr("wafer.AsyncSession.request", _fake_request)
        monkeypatch.setattr("fetchaller.tools.fetch.check_host", _allow_host)

        await _fetch_url_impl(
            "https://api.example.com/search",
            method="POST",
            body='{"q":1}',
            timeout=10,
        )

        assert calls == [("POST", '{"q":1}'), ("POST", '{"q":1}')]

    async def test_credentials_are_dropped_when_the_redirect_changes_host(self, monkeypatch):
        seen = []

        async def _fake_get(self, url, **kwargs):
            seen.append(kwargs.get("headers") or {})
            if len(seen) == 1:
                return _Redirect("https://elsewhere.example/x", 302)
            return _JsonResponse(url)

        monkeypatch.setattr("wafer.AsyncSession.get", _fake_get)
        monkeypatch.setattr("fetchaller.tools.fetch.check_host", _allow_host)

        await _fetch_url_impl(
            "https://api.example.com/x",
            headers={"Authorization": "Bearer secret", "Accept": "application/json"},
            timeout=10,
        )

        assert seen[0]["authorization"] == "Bearer secret"
        assert "authorization" not in seen[1]
        # Non-credential headers still describe what the caller wants back.
        assert seen[1]["accept"] == "application/json"

    def test_credential_list_covers_the_usual_bearers(self):
        assert {"authorization", "cookie"} <= _CREDENTIAL_HEADERS


# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------


async def _allow_host(hostname):
    from fetchaller.security.ssrf import HostVerdict

    return HostVerdict(hostname, False, ["93.184.216.34"])


class _Headers(dict):
    pass


class _JsonResponse:
    def __init__(self, url):
        self.status_code = 200
        self.url = url
        self.headers = _Headers({"content-type": "application/json"})
        self.text = '{"results":{"count":1}}'
        self.content = self.text.encode()


class _Redirect:
    def __init__(self, location, status_code, url="https://api.example.com/search"):
        self.status_code = status_code
        self.url = url
        self.headers = _Headers({"location": location})
        self.text = ""
        self.content = b""


class TestCredentialsFollowTheOrigin:
    """Credentials are scoped to the origin the caller named, not the hostname.

    Keying only on hostname let an https -> http redirect on the same name put a
    bearer token on the wire in cleartext, and a port change hand it to a
    different service on the same machine.
    """

    @staticmethod
    async def _run(monkeypatch, start, location):
        seen = []

        async def _fake_get(self, target, **kwargs):
            seen.append(kwargs.get("headers") or {})
            if len(seen) == 1:
                return _Redirect(location, 302, url=start)
            return _JsonResponse(target)

        monkeypatch.setattr("wafer.AsyncSession.get", _fake_get)
        monkeypatch.setattr("fetchaller.tools.fetch.check_host", _allow_host)

        await _fetch_url_impl(
            start,
            headers={"Authorization": "Bearer secret", "Accept": "application/json"},
            timeout=10,
        )
        return seen

    async def test_scheme_downgrade_drops_credentials(self, monkeypatch):
        seen = await self._run(
            monkeypatch,
            "https://api.example.com/x",
            "http://api.example.com/x",
        )
        assert seen[0]["authorization"] == "Bearer secret"
        assert "authorization" not in seen[1]

    async def test_port_change_drops_credentials(self, monkeypatch):
        seen = await self._run(
            monkeypatch,
            "https://api.example.com/x",
            "https://api.example.com:8443/x",
        )
        assert "authorization" not in seen[1]

    async def test_same_origin_redirect_keeps_credentials(self, monkeypatch):
        """A plain path redirect within one origin is still the caller's server."""
        seen = await self._run(
            monkeypatch,
            "https://api.example.com/x",
            "https://api.example.com/y",
        )
        assert seen[1]["authorization"] == "Bearer secret"

    async def test_explicit_default_port_is_the_same_origin(self, monkeypatch):
        seen = await self._run(
            monkeypatch,
            "https://api.example.com/x",
            "https://api.example.com:443/y",
        )
        assert seen[1]["authorization"] == "Bearer secret"


class TestRedditRefererIsPinned:
    async def test_caller_referer_cannot_override_the_pin(self, monkeypatch):
        """Two Referer values would go on the wire, and the point of pinning is
        that this header is not caller-controlled."""
        seen = []

        async def _fake_get(self, target, **kwargs):
            seen.append(kwargs.get("headers") or {})
            return _JsonResponse(target)

        async def _no_wait():
            return None

        monkeypatch.setattr("wafer.AsyncSession.get", _fake_get)
        monkeypatch.setattr("fetchaller.tools.fetch.check_host", _allow_host)
        monkeypatch.setattr("fetchaller.ratelimit.reddit_limiter.wait", _no_wait)
        await close_reddit_session()

        await _fetch_url_impl(
            "https://www.reddit.com/r/python/.json",
            headers={"Referer": "https://leaky.example/secret-path"},
            timeout=10,
        )

        headers = seen[0]
        referers = [value for key, value in headers.items() if key.lower() == "referer"]
        assert referers == ["https://www.reddit.com/"]


class TestPostSkipsPostFetchEmbedHandlers:
    """The interceptors that run BEFORE the fetch are not the only ones.

    After an HTML response comes back, a preflight pass looks for embedded job
    boards (Greenhouse, Ashby, BambooHR, JazzHR), feeds, and GitHub/AliExpress
    structures, and each match issues its OWN secondary request and REPLACES the
    body with that board. On a POST that returns HTML, the caller would get a
    GET of somebody else's board instead of their own response.
    """

    @staticmethod
    async def _post_html(monkeypatch, html):
        seen = []

        async def _fake_request(self, method, target, **kwargs):
            seen.append((method, target))
            return _HtmlResponse(target, html)

        async def _fake_get(self, target, **kwargs):
            seen.append(("GET", target))
            return _HtmlResponse(target, "<html><body>secondary</body></html>")

        monkeypatch.setattr("wafer.AsyncSession.request", _fake_request)
        monkeypatch.setattr("wafer.AsyncSession.get", _fake_get)
        monkeypatch.setattr("fetchaller.tools.fetch.check_host", _allow_host)

        result = await _fetch_url_impl(
            "https://careers.example.com/",
            method="POST",
            body="{}",
            timeout=20,
        )
        return seen, result

    async def test_bamboohr_embed_does_not_hijack_a_post(self, monkeypatch):
        html = (
            "<html><body><h1>Our openings</h1>"
            '<div id="BambooHR" data-domain="acme.bamboohr.com"></div>'
            "</body></html>"
        )
        seen, result = await self._post_html(monkeypatch, html)

        assert seen == [("POST", "https://careers.example.com/")], seen
        assert "Our openings" in result.get("content", "")

    async def test_ashby_embed_does_not_hijack_a_post(self, monkeypatch):
        html = (
            "<html><body><h1>Our openings</h1>"
            '<script src="https://jobs.ashbyhq.com/acme/embed"></script>'
            "</body></html>"
        )
        seen, _ = await self._post_html(monkeypatch, html)

        assert seen == [("POST", "https://careers.example.com/")], seen

    async def test_feed_autodiscovery_does_not_hijack_a_post(self, monkeypatch):
        html = (
            "<html><head>"
            '<link rel="alternate" type="application/rss+xml" href="https://careers.example.com/feed">'
            "</head><body><p>Discourse forum posts</p></body></html>"
        )
        seen, _ = await self._post_html(monkeypatch, html)

        assert seen == [("POST", "https://careers.example.com/")], seen


class _HtmlResponse:
    def __init__(self, url, html):
        self.status_code = 200
        self.url = url
        self.headers = _Headers({"content-type": "text/html; charset=utf-8"})
        self.text = html
        self.content = html.encode()


class TestPostRedirectsAreStillSsrfChecked:
    """Adding a verb must not create a path around the pin-every-hop rule."""

    @staticmethod
    async def _redirect_to(monkeypatch, location, method="POST"):
        from fetchaller.security.ssrf import BLOCK_PRIVATE, HostVerdict

        seen = []

        async def _check(hostname):
            seen.append(hostname)
            if hostname in ("api.example.com", "elsewhere.example"):
                return HostVerdict(hostname, False, ["93.184.216.34"])
            return HostVerdict(hostname, True, [], BLOCK_PRIVATE)

        async def _fake_request(self, verb, target, **kwargs):
            return _Redirect(location, 302, url=target)

        async def _fake_get(self, target, **kwargs):
            return _Redirect(location, 302, url=target)

        monkeypatch.setattr("wafer.AsyncSession.request", _fake_request)
        monkeypatch.setattr("wafer.AsyncSession.get", _fake_get)
        monkeypatch.setattr("fetchaller.tools.fetch.check_host", _check)

        kwargs = {"method": method}
        if method == "POST":
            kwargs["body"] = "{}"
        result = await _fetch_url_impl("https://api.example.com/x", timeout=10, **kwargs)
        return seen, result

    @pytest.mark.parametrize("method", ["GET", "POST"])
    async def test_redirect_to_a_private_host_is_refused(self, monkeypatch, method):
        seen, result = await self._redirect_to(
            monkeypatch, "http://169.254.169.254/latest/meta-data/", method=method
        )

        assert "error" in result
        assert "private" in result["error"].lower()
        # The redirect target was validated BEFORE any connection to it.
        assert "169.254.169.254" in seen

    async def test_post_redirect_to_a_public_host_is_validated_and_pinned(self, monkeypatch):
        seen, _ = await self._redirect_to(monkeypatch, "https://elsewhere.example/y")

        assert "elsewhere.example" in seen


class TestTerminalChallengeAdvice:
    """A firewall rule denying the request is not a state to wait out.

    wafer reported a Cloudflare WAF block (Error 1020) as `generic_js`, a
    solvable type, so the advice was "try again — this sometimes resolves on
    retry". It never does: the rule returns the same answer to every retry,
    rotation, and browser solve. wafer now labels these `cloudflare_block`.
    """

    def test_terminal_block_does_not_promise_a_retry(self):
        from fetchaller.tools.fetch import describe_challenge

        message = describe_challenge("cloudflare_block")

        assert "Try again" not in message
        assert "not a challenge to solve" in message
        assert "403" in message

    def test_terminal_block_suggests_checking_the_url(self):
        """A parked or misspelled domain blocking everything looks exactly like
        a bot wall — airmatrix.ca vs the real airmatrix.ai."""
        from fetchaller.tools.fetch import describe_challenge

        assert "URL is right" in describe_challenge("cloudflare_block")

    @pytest.mark.parametrize("challenge", ["generic_js", "turnstile", "datadome", "reddit"])
    def test_solvable_challenges_keep_the_retry_hint(self, challenge):
        from fetchaller.tools.fetch import describe_challenge

        message = describe_challenge(challenge)

        assert "Try again" in message
        assert challenge in message

    def test_missing_challenge_type_still_renders(self):
        from fetchaller.tools.fetch import describe_challenge

        assert "unknown" in describe_challenge(None)

    async def test_the_fetch_path_uses_it(self, monkeypatch):
        import wafer

        from fetchaller.tools.fetch import _fetch_url_impl

        async def _blocked(self, target, **kwargs):
            raise wafer.ChallengeDetected("cloudflare_block", target, 403)

        monkeypatch.setattr("wafer.AsyncSession.get", _blocked)
        monkeypatch.setattr("fetchaller.tools.fetch.check_host", _allow_host)

        result = await _fetch_url_impl("https://blocked.example/", timeout=10)

        assert "Try again" not in result["error"]
        assert "cloudflare_block" in result["error"]


class TestSharedCacheIsolation:
    """The response cache is keyed by URL alone, so only a plain GET may use it.

    Reads were gated on `structured`; writes were not. A POST returning HTML
    wrote into the GET entry for that URL, and a GET carrying `Authorization`
    stored its authenticated body under the plain URL for the next caller.
    """

    class _Cache:
        def __init__(self):
            self.reads: list[str] = []
            self.writes: list[str] = []

        def get(self, key):
            self.reads.append(key)
            return None

        def set(self, key, *args, **kwargs):
            self.writes.append(key)

    @staticmethod
    def _html(url, body="<html><body><h1>Secret</h1><p>Account data.</p></body></html>"):
        return _HtmlResponse(url, body)

    async def _run(self, monkeypatch, **kwargs):
        cache = self._Cache()

        async def _fake_get(self, target, **kw):
            return TestSharedCacheIsolation._html(target)

        async def _fake_request(self, verb, target, **kw):
            return TestSharedCacheIsolation._html(target)

        monkeypatch.setattr("wafer.AsyncSession.get", _fake_get)
        monkeypatch.setattr("wafer.AsyncSession.request", _fake_request)
        monkeypatch.setattr("fetchaller.tools.fetch.check_host", _allow_host)

        result = await _fetch_url_impl(
            "https://api.example.com/account", timeout=10, cache=cache, **kwargs
        )
        return cache, result

    async def test_post_returning_html_never_writes_the_get_cache(self, monkeypatch):
        cache, result = await self._run(monkeypatch, method="POST", body="{}")

        assert "content" in result
        assert cache.writes == [], f"POST poisoned the shared cache: {cache.writes}"
        assert cache.reads == []

    async def test_authenticated_get_never_writes_the_shared_cache(self, monkeypatch):
        """Otherwise the next anonymous caller of this URL is served the
        authenticated body."""
        cache, result = await self._run(
            monkeypatch, headers={"Authorization": "Bearer secret"}
        )

        assert "content" in result
        assert cache.writes == [], f"authenticated body cached: {cache.writes}"

    async def test_authenticated_get_never_reads_the_shared_cache(self, monkeypatch):
        """It would be served an anonymous body as if it were its own."""
        cache, _ = await self._run(monkeypatch, headers={"Authorization": "Bearer secret"})

        assert cache.reads == []

    async def test_any_caller_header_opts_out_of_the_shared_cache(self, monkeypatch):
        """Not just credentials: `Accept` changes the representation, and the
        key does not record it."""
        cache, _ = await self._run(monkeypatch, headers={"Accept": "application/json"})

        assert cache.writes == []
        assert cache.reads == []

    async def test_a_plain_get_still_uses_the_cache(self, monkeypatch):
        """The gate must not disable caching for ordinary fetches."""
        cache, result = await self._run(monkeypatch)

        assert "content" in result
        assert cache.reads, "plain GET stopped reading the cache"
        assert cache.writes, "plain GET stopped writing the cache"

    async def test_header_carrying_get_skips_site_interceptors(self, monkeypatch):
        """An interceptor issues its own request and would drop the header,
        answering a different request than the caller asked for."""
        seen = []

        async def _fake_get(self, target, **kw):
            seen.append(target)
            return TestSharedCacheIsolation._html(target)

        monkeypatch.setattr("wafer.AsyncSession.get", _fake_get)
        monkeypatch.setattr("fetchaller.tools.fetch.check_host", _allow_host)

        url = "https://boards.greenhouse.io/example"
        await _fetch_url_impl(url, timeout=10, headers={"Authorization": "Bearer s"})

        assert seen == [url], f"interceptor fired for a header-carrying GET: {seen}"
