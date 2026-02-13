"""Tests for botfighter: challenge detection, ACW solver, cookie cache, and integration."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fetchaller.botfighter import (
    CachedCookies,
    ChallengeSolver,
    CookieCache,
    detect_challenge,
    is_acw_waf_challenge,
    is_amazon_captcha,
    solve_acw_sc_v2,
)

# ── ACW Detection ─────────────────────────────────────────────────────────────


class TestAcwDetection:
    def test_detects_acw_challenge(self):
        body = "<html><script>var arg1='ABC123';var _0x4818=[]; acw_sc__v2</script></html>"
        assert is_acw_waf_challenge(body) is True

    def test_rejects_non_acw(self):
        assert is_acw_waf_challenge("<html><body>Normal page</body></html>") is False
        assert is_acw_waf_challenge("acw_sc__v2 but missing the other marker") is False
        assert is_acw_waf_challenge("arg1 but no acw cookie") is False


# ── ACW Solver ────────────────────────────────────────────────────────────────


class TestAcwSolver:
    def test_solves_known_arg1(self):
        """Solver produces correct 40-char hex cookie from known arg1. [#26]"""
        html = "var arg1='70D9569CD5E5895C84F284A09503B1598C5762A1'; acw_sc__v2"
        result = solve_acw_sc_v2(html)
        # Deterministic expected value for this arg1 with the fixed shuffle + XOR
        assert result == "65b09b125d70704c9e7ba57fa8966899a253c522"

    def test_deterministic(self):
        """Same arg1 always produces same cookie."""
        html = "var arg1='AABBCCDDEE1122334455AABBCCDDEE1122334455';"
        assert solve_acw_sc_v2(html) == solve_acw_sc_v2(html)

    def test_returns_none_for_missing_arg1(self):
        assert solve_acw_sc_v2("<html><script>var x=1;</script></html>") is None

    def test_returns_none_for_short_arg1(self):
        """arg1 shorter than 40 chars (max shuffle index) returns None."""
        assert solve_acw_sc_v2("var arg1='ABC';") is None

    def test_returns_none_for_empty_html(self):
        assert solve_acw_sc_v2("") is None


# ── Challenge Detection ───────────────────────────────────────────────────────


class TestDetectChallenge:
    """Test detect_challenge() identifies all supported challenge types."""

    def test_detects_acw(self):
        assert detect_challenge(200, {}, "var arg1='x'; acw_sc__v2") == "acw"

    def test_detects_cloudflare_header(self):
        assert detect_challenge(403, {"cf-mitigated": "challenge"}, "") == "cloudflare"

    def test_detects_cloudflare_body(self):
        assert detect_challenge(403, {}, "window._cf_chl_opt = {}") == "cloudflare"

    def test_detects_akamai_cookie_403(self):
        assert detect_challenge(403, {"set-cookie": "_abck=abc"}, "") == "akamai"

    def test_detects_akamai_body_non_200(self):
        """Akamai body markers only trigger on non-200 status. [#19]"""
        assert detect_challenge(403, {"set-cookie": "_abck=abc"}, "bmSz check") == "akamai"

    def test_akamai_body_200_no_trigger(self):
        """Akamai body markers on 200 should NOT trigger false positive. [#19]"""
        assert detect_challenge(200, {"set-cookie": "_abck=abc"}, "bmSz check") is None

    def test_detects_akamai_keyword(self):
        assert detect_challenge(403, {}, "Akamai Bot Manager") == "akamai"

    def test_detects_datadome_cookie(self):
        assert detect_challenge(403, {"set-cookie": "datadome=abc"}, "") == "datadome"

    def test_detects_datadome_body(self):
        assert detect_challenge(403, {}, "datadome challenge") == "datadome"

    def test_detects_perimeterx_cookie(self):
        assert detect_challenge(403, {"set-cookie": "_px3=abc"}, "") == "perimeterx"

    def test_detects_perimeterx_body(self):
        assert detect_challenge(403, {}, "human.security verification") == "perimeterx"

    def test_detects_imperva_cookie(self):
        assert detect_challenge(403, {"set-cookie": "reese84=abc"}, "") == "imperva"

    def test_detects_imperva_body(self):
        assert detect_challenge(403, {}, "incapsula incident") == "imperva"

    def test_detects_kasada(self):
        assert detect_challenge(429, {"x-kpsdk-ct": "1"}, "") == "kasada"

    def test_detects_kasada_mixed_case_header(self):
        """Kasada header check is case-insensitive."""
        assert detect_challenge(429, {"X-KPSDK-CT": "1"}, "") == "kasada"

    def test_detects_unknown_js_challenge(self):
        body = "<html><script>challenge()</script></html>"
        assert detect_challenge(403, {}, body) == "unknown"

    def test_returns_none_for_normal_200(self):
        assert detect_challenge(200, {}, "<html>Normal page</html>") is None

    def test_returns_none_for_large_403(self):
        """Large 403 pages are real error pages, not challenges."""
        body = "x" * 60_000
        assert detect_challenge(403, {}, body) is None

    def test_returns_none_for_403_without_script(self):
        assert detect_challenge(403, {}, "<html>Access Denied</html>") is None

    def test_acw_takes_priority_over_generic(self):
        """ACW detection should trigger before generic fallback."""
        body = "<html><script>var arg1='x'; acw_sc__v2</script></html>"
        assert detect_challenge(403, {}, body) == "acw"

    def test_detects_amazon_captcha(self):
        """Amazon rate-limit page: status 200, small body, 'Continue shopping' + Amazon markers."""
        body = '<html><body>Continue shopping<a href="https://www.amazon.ca/">amazon.ca</a></body></html>'
        assert detect_challenge(200, {}, body) == "amazon"

    def test_amazon_requires_small_body(self):
        """Normal Amazon product pages (1-3M chars) must NOT trigger Amazon captcha detection."""
        body = "Continue shopping amazon.ca " + "x" * 60_000
        assert detect_challenge(200, {}, body) is None

    def test_amazon_requires_amazon_marker(self):
        """Small page with 'Continue shopping' but no Amazon markers is not Amazon captcha."""
        body = "<html><body>Continue shopping on our store</body></html>"
        assert detect_challenge(200, {}, body) is None

    def test_cloudflare_takes_priority_over_unknown(self):
        """CF header detection takes priority over generic JS challenge."""
        assert detect_challenge(403, {"cf-mitigated": "challenge"}, "<script>x</script>") == "cloudflare"


# ── Amazon Captcha Detection ─────────────────────────────────────────────────


class TestAmazonCaptcha:
    def test_detects_captcha_with_amazon_domain(self):
        body = '<html><body>Sorry, we need to make sure you\'re not a robot. <a href="https://www.amazon.ca/">Continue shopping</a></body></html>'
        assert is_amazon_captcha(body) is True

    def test_detects_captcha_with_amzn_marker(self):
        body = '<html><body>amzn.to<br>Continue shopping</body></html>'
        assert is_amazon_captcha(body) is True

    def test_detects_validate_captcha_url(self):
        body = '<html><body><form action="/errors/validateCaptcha">Continue shopping</form></body></html>'
        assert is_amazon_captcha(body) is True

    def test_rejects_normal_product_page(self):
        """Normal Amazon pages are > 50K and should not trigger."""
        body = "Continue shopping amazon " + "x" * 60_000
        assert is_amazon_captcha(body) is False

    def test_rejects_non_amazon_small_page(self):
        """Small non-Amazon page with 'continue shopping' should not trigger."""
        body = "<html><body>Continue shopping at our store</body></html>"
        assert is_amazon_captcha(body) is False


# ── Cookie Cache ──────────────────────────────────────────────────────────────


class TestCookieCache:
    def test_set_and_get(self):
        cache = CookieCache()
        cookies = [{"name": "test", "value": "123", "domain": ".example.com", "path": "/"}]
        cache.set("example.com", "akamai", cookies, "Mozilla/5.0", "chrome131")
        entry = cache.get("example.com")
        assert entry is not None
        assert entry.challenge_type == "akamai"
        assert len(entry.cookies) == 1
        assert entry.user_agent == "Mozilla/5.0"
        assert entry.impersonate == "chrome131"
        assert entry.expires is None  # Non-CF: no expiry

    def test_get_returns_none_for_missing(self):
        cache = CookieCache()
        assert cache.get("unknown.com") is None

    def test_evict(self):
        cache = CookieCache()
        cache.set("example.com", "akamai", [], "UA", "chrome131")
        cache.evict("example.com")
        assert cache.get("example.com") is None

    def test_evict_nonexistent_is_noop(self):
        cache = CookieCache()
        cache.evict("nonexistent.com")  # Should not raise

    def test_cf_expiry_tracking(self):
        cache = CookieCache()
        future_time = time.time() + 3600
        cookies = [
            {"name": "cf_clearance", "value": "abc", "domain": ".example.com", "path": "/", "expires": future_time}
        ]
        cache.set("example.com", "cloudflare", cookies, "UA", "chrome131")
        entry = cache.get("example.com")
        assert entry is not None
        assert entry.expires == future_time

    def test_cf_expired_entry_evicted_on_get(self):
        cache = CookieCache()
        past_time = time.time() - 100
        cookies = [
            {"name": "cf_clearance", "value": "abc", "domain": ".example.com", "path": "/", "expires": past_time}
        ]
        cache.set("example.com", "cloudflare", cookies, "UA", "chrome131")
        assert cache.get("example.com") is None  # Should be evicted

    def test_non_cf_no_expiry(self):
        """Non-CF entries don't track expiry, even if cookies have expires."""
        cache = CookieCache()
        cookies = [
            {"name": "_abck", "value": "abc", "domain": ".example.com", "path": "/", "expires": time.time() - 100}
        ]
        cache.set("example.com", "akamai", cookies, "UA", "chrome131")
        entry = cache.get("example.com")
        assert entry is not None  # Should NOT be evicted — non-CF

    def test_lru_eviction(self):
        """Oldest entry evicted when cache exceeds max size. [#27]"""
        from fetchaller.botfighter import _MAX_CACHE_ENTRIES
        cache = CookieCache()
        # Fill to capacity
        for i in range(_MAX_CACHE_ENTRIES):
            cache.set(f"domain{i}.com", "akamai", [], "UA", "chrome131")
        # Next insert should evict oldest
        cache.set("new-domain.com", "akamai", [], "UA", "chrome131")
        assert cache.get("new-domain.com") is not None
        assert cache.get("domain0.com") is None  # Oldest should be evicted

    def test_rejects_private_final_url(self):
        """Private/internal final_url is rejected on set. [#10]"""
        cache = CookieCache()
        cache.set("example.com", "cloudflare", [
            {"name": "cf_clearance", "value": "abc", "expires": time.time() + 3600}
        ], "UA", "chrome131", final_url="http://192.168.1.1/admin")
        entry = cache.get("example.com")
        assert entry is not None
        assert entry.final_url is None  # Private URL should have been rejected

    def test_persist_save_and_load(self, tmp_path):
        """Cookies saved to disk are restored on new CookieCache init."""
        path = str(tmp_path / "cookies.json")
        cache1 = CookieCache(persist_path=path)
        cookies = [{"name": "test", "value": "123", "domain": ".example.com"}]
        cache1.set("example.com", "akamai", cookies, "Mozilla/5.0", "chrome131")

        # New cache instance loads from same file
        cache2 = CookieCache(persist_path=path)
        entry = cache2.get("example.com")
        assert entry is not None
        assert entry.challenge_type == "akamai"
        assert entry.cookies == cookies
        assert entry.user_agent == "Mozilla/5.0"

    def test_persist_evict_removes_from_disk(self, tmp_path):
        """Evicted entries are removed from the persisted file."""
        path = str(tmp_path / "cookies.json")
        cache1 = CookieCache(persist_path=path)
        cache1.set("example.com", "akamai", [], "UA", "chrome131")
        cache1.evict("example.com")

        cache2 = CookieCache(persist_path=path)
        assert cache2.get("example.com") is None

    def test_persist_skips_expired_cf_on_load(self, tmp_path):
        """Expired CF entries in the JSON file are skipped on load."""
        path = str(tmp_path / "cookies.json")
        cache1 = CookieCache(persist_path=path)
        past_time = time.time() - 100
        cookies = [{"name": "cf_clearance", "value": "abc", "expires": past_time}]
        # Write directly to bypass the in-memory expiry check
        cache1._cache["example.com"] = CachedCookies(
            challenge_type="cloudflare", cookies=cookies,
            user_agent="UA", impersonate="chrome131", expires=past_time,
        )
        cache1._save()

        cache2 = CookieCache(persist_path=path)
        assert cache2.get("example.com") is None

    def test_persist_preserves_final_url(self, tmp_path):
        """Redirect final_url survives round-trip to disk."""
        path = str(tmp_path / "cookies.json")
        cache1 = CookieCache(persist_path=path)
        cache1.set("glassdoor.com", "cloudflare", [
            {"name": "cf_clearance", "value": "abc", "expires": time.time() + 3600}
        ], "UA", "chrome131", final_url="https://www.glassdoor.ca/")

        cache2 = CookieCache(persist_path=path)
        entry = cache2.get("glassdoor.com")
        assert entry is not None
        assert entry.final_url == "https://www.glassdoor.ca/"

    def test_persist_no_path_is_noop(self):
        """Without persist_path, save/load are silent noops."""
        cache = CookieCache()  # No path
        cache.set("example.com", "akamai", [], "UA", "chrome131")
        # Should not raise — just doesn't persist

    def test_persist_corrupt_file_starts_fresh(self, tmp_path):
        """Corrupt JSON file is handled gracefully — starts with empty cache."""
        path = tmp_path / "cookies.json"
        path.write_text("not valid json{{{")
        cache = CookieCache(persist_path=str(path))
        assert cache.get("example.com") is None

    def test_batch_save_avoids_double_write(self):
        """Using _save=False then _save=True avoids double disk write. [#20]"""
        cache = CookieCache()
        cache.set("domain1.com", "akamai", [], "UA", "chrome131", _save=False)
        cache.set("domain2.com", "akamai", [], "UA", "chrome131", _save=True)
        # Both entries should exist
        assert cache.get("domain1.com") is not None
        assert cache.get("domain2.com") is not None

    def test_persist_malformed_entry_skipped(self, tmp_path):
        """A single malformed entry doesn't kill loading of remaining entries."""
        import json
        path = tmp_path / "cookies.json"
        data = {
            "bad.com": {"cookies": []},  # Missing required fields
            "good.com": {
                "challenge_type": "akamai", "cookies": [{"name": "x", "value": "y"}],
                "user_agent": "UA", "impersonate": "chrome131",
            },
        }
        path.write_text(json.dumps(data))
        cache = CookieCache(persist_path=str(path))
        assert cache.get("bad.com") is None
        assert cache.get("good.com") is not None
        assert cache.get("good.com").cookies == [{"name": "x", "value": "y"}]

    def test_persist_extra_keys_ignored(self, tmp_path):
        """Future keys in JSON are ignored, not passed to constructor."""
        import json
        path = tmp_path / "cookies.json"
        data = {
            "example.com": {
                "challenge_type": "akamai", "cookies": [], "user_agent": "UA",
                "impersonate": "chrome131", "future_field": "should be ignored",
            },
        }
        path.write_text(json.dumps(data))
        cache = CookieCache(persist_path=str(path))
        entry = cache.get("example.com")
        assert entry is not None
        assert entry.challenge_type == "akamai"


# ── Challenge Solver ──────────────────────────────────────────────────────────


class TestChallengeSolver:
    @pytest.mark.asyncio
    async def test_returns_error_when_lock_busy(self):
        """When lock is held, returns error dict immediately."""
        solver = ChallengeSolver()
        # Acquire the lock manually to simulate busy state
        await solver._lock.acquire()
        try:
            result = await solver.solve("https://example.com", "cloudflare")
            assert result is not None
            assert "error" in result
            assert "try again" in result["error"].lower()
        finally:
            solver._lock.release()

    @pytest.mark.asyncio
    async def test_returns_none_when_browser_fails(self):
        """When Chrome can't start, returns None."""
        solver = ChallengeSolver()
        with patch("fetchaller.botfighter.ChallengeSolver._ensure_browser", return_value=None):
            result = await solver.solve("https://example.com", "cloudflare")
            assert result is None

    @pytest.mark.asyncio
    async def test_dispatches_cloudflare(self):
        """Cloudflare challenges dispatch to _solve_cloudflare."""
        solver = ChallengeSolver()
        mock_tab = AsyncMock()
        mock_cookies = [{"name": "cf_clearance", "value": "abc", "domain": ".example.com"}]

        with patch.object(solver, "_ensure_browser", return_value=mock_tab):
            with patch.object(solver, "_solve_cloudflare", return_value={"cookies": mock_cookies, "user_agent": "Mozilla/5.0"}):
                result = await solver.solve("https://example.com", "cloudflare")
                assert result is not None
                assert result["cookies"] == mock_cookies
                assert result["impersonate"] == "chrome131"  # [#3] Check impersonate is attached

    @pytest.mark.asyncio
    async def test_dispatches_akamai(self):
        """Akamai challenges dispatch to _solve_akamai."""
        solver = ChallengeSolver()
        mock_tab = AsyncMock()
        expected_cookies = [{"name": "_abck", "value": "valid"}]

        with patch.object(solver, "_ensure_browser", return_value=mock_tab):
            with patch.object(solver, "_solve_akamai", return_value={"cookies": expected_cookies, "user_agent": "UA"}):
                result = await solver.solve("https://example.com", "akamai")
                assert result["cookies"] == expected_cookies
                assert result["impersonate"] == "chrome131"

    @pytest.mark.asyncio
    async def test_dispatches_amazon(self):
        """Amazon challenges dispatch to _solve_amazon."""
        solver = ChallengeSolver()
        mock_tab = AsyncMock()
        expected_cookies = [{"name": "session-id", "value": "123"}]

        with patch.object(solver, "_ensure_browser", return_value=mock_tab):
            with patch.object(solver, "_solve_amazon", return_value={"cookies": expected_cookies, "user_agent": "UA"}):
                result = await solver.solve("https://www.amazon.ca/dp/B123", "amazon")
                assert result["cookies"] == expected_cookies
                assert result["impersonate"] == "chrome131"

    @pytest.mark.asyncio
    async def test_dispatches_generic_for_unknown(self):
        """Unknown challenges dispatch to _solve_generic."""
        solver = ChallengeSolver()
        mock_tab = AsyncMock()
        expected_cookies = [{"name": "x", "value": "y"}]

        with patch.object(solver, "_ensure_browser", return_value=mock_tab):
            with patch.object(solver, "_solve_generic", return_value={"cookies": expected_cookies, "user_agent": "UA"}):
                result = await solver.solve("https://example.com", "unknown")
                assert result["cookies"] == expected_cookies


# ── _handle_botfighter Integration Tests [#18] ───────────────────────────────


class TestHandleBotfighter:
    """Test the _handle_botfighter integration function from tools/fetch.py."""

    def _make_result(self, status_code=200, body=b"<html>OK</html>", headers=None, content_type="text/html"):
        """Create a mock FetchResult."""
        from fetchaller.content.fetcher import FetchResult
        return FetchResult(
            content=body,
            content_type=content_type,
            status_code=status_code,
            final_url="https://example.com",
            headers=headers or {},
        )

    @pytest.mark.asyncio
    async def test_no_challenge_returns_original_result(self):
        """Normal 200 response passes through without body decode."""
        from fetchaller.tools.fetch import _handle_botfighter
        result = self._make_result()
        fetcher = MagicMock()
        bf_result = await _handle_botfighter(result, "https://example.com", 10.0, fetcher, None, None, False)
        assert bf_result is result  # Same object — not re-decoded

    @pytest.mark.asyncio
    async def test_acw_challenge_solved_inline(self):
        """ACW challenge is solved inline with pure Python using a dedicated fetcher."""
        from fetchaller.tools.fetch import _handle_botfighter
        acw_body = b"var arg1='70D9569CD5E5895C84F284A09503B1598C5762A1'; acw_sc__v2"
        result = self._make_result(status_code=200, body=acw_body)
        success_result = self._make_result(status_code=200, body=b"<html>Success</html>")

        mock_acw_fetcher = AsyncMock()
        mock_acw_fetcher.set_cookie = AsyncMock()
        mock_acw_fetcher.fetch = AsyncMock(return_value=success_result)
        mock_acw_fetcher.close = AsyncMock()

        fetcher = MagicMock()  # Shared fetcher — should NOT be mutated

        with patch("fetchaller.tools.fetch.ContentFetcher", return_value=mock_acw_fetcher):
            bf_result = await _handle_botfighter(result, "https://example.com", 10.0, fetcher, CookieCache(), None, False)
        assert bf_result is success_result
        mock_acw_fetcher.set_cookie.assert_called_once()
        mock_acw_fetcher.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_browser_solve_error_returns_dict(self):
        """Failed browser solve returns error dict."""
        from fetchaller.tools.fetch import _handle_botfighter
        cf_body = b"window._cf_chl_opt = {}"
        result = self._make_result(status_code=403, body=cf_body)
        fetcher = MagicMock()
        fetcher.unpin_identity = MagicMock()
        solver = AsyncMock()
        solver.solve = AsyncMock(return_value=None)

        bf_result = await _handle_botfighter(result, "https://example.com", 10.0, fetcher, CookieCache(), solver, False)
        assert isinstance(bf_result, dict)
        assert "error" in bf_result

    @pytest.mark.asyncio
    async def test_stale_cookies_evicted_on_re_challenge(self):
        """When cached cookies fail, they're evicted from cache."""
        from fetchaller.tools.fetch import _handle_botfighter
        cf_body = b"window._cf_chl_opt = {}"
        result = self._make_result(status_code=403, body=cf_body)
        fetcher = MagicMock()
        cache = CookieCache()
        cache.set("example.com", "cloudflare", [{"name": "cf_clearance", "value": "old", "expires": time.time() + 3600}], "UA", "chrome131")
        solver = AsyncMock()
        solver.solve = AsyncMock(return_value=None)

        await _handle_botfighter(result, "https://example.com", 10.0, fetcher, cache, solver, True)
        assert cache.get("example.com") is None  # Evicted

    @pytest.mark.asyncio
    async def test_geo_redirect_evicts_both_domains(self):
        """Re-challenge after geo-redirect evicts both original and redirect domain.

        Without this, stale cookies on the original domain (e.g., glassdoor.com)
        would loop: cache hit → redirect to .ca → re-challenge → evict .ca only
        → next request hits stale .com entry → infinite loop.
        """
        from fetchaller.tools.fetch import _handle_botfighter
        cf_body = b"window._cf_chl_opt = {}"
        result = self._make_result(status_code=403, body=cf_body)
        fetcher = MagicMock()
        cache = CookieCache()
        # Simulate dual-domain cache from a previous solve
        future = time.time() + 3600
        cache.set("www.glassdoor.com", "cloudflare", [
            {"name": "cf_clearance", "value": "old", "expires": future}
        ], "UA", "chrome131", final_url="https://www.glassdoor.ca/")
        cache.set("www.glassdoor.ca", "cloudflare", [
            {"name": "cf_clearance", "value": "old", "expires": future}
        ], "UA", "chrome131")
        solver = AsyncMock()
        solver.solve = AsyncMock(return_value=None)

        # Simulate: fetch_url_str was overridden to glassdoor.ca (via cached final_url),
        # but the cookie lookup domain was glassdoor.com
        await _handle_botfighter(
            result, "https://www.glassdoor.ca/", 10.0, fetcher, cache, solver, True,
            cookie_lookup_domain="www.glassdoor.com",
        )
        assert cache.get("www.glassdoor.ca") is None  # Redirect domain evicted
        assert cache.get("www.glassdoor.com") is None  # Original domain also evicted

    @pytest.mark.asyncio
    async def test_geo_redirect_recaches_lookup_domain(self):
        """After re-solve via geo-redirect, original lookup domain is re-cached
        so the next request doesn't need another solve."""
        from fetchaller.tools.fetch import _handle_botfighter
        cf_body = b"window._cf_chl_opt = {}"
        result = self._make_result(status_code=403, body=cf_body)
        fetcher = MagicMock()
        fetcher.current_impersonate = "chrome131"

        mock_retry_fetcher = AsyncMock()
        mock_retry_fetcher.apply_cookies = AsyncMock()
        mock_retry_fetcher.pin_identity = MagicMock()
        mock_retry_fetcher.fetch = AsyncMock(return_value=self._make_result())
        mock_retry_fetcher.close = AsyncMock()

        new_cookies = [{"name": "cf_clearance", "value": "fresh", "expires": time.time() + 3600}]
        solver = AsyncMock()
        solver.solve = AsyncMock(return_value={
            "cookies": new_cookies,
            "user_agent": "Mozilla/5.0",
            "final_url": "https://www.glassdoor.ca/",
            "impersonate": "chrome131",
        })

        cache = CookieCache()
        with patch("fetchaller.tools.fetch.ContentFetcher", return_value=mock_retry_fetcher):
            await _handle_botfighter(
                result, "https://www.glassdoor.ca/", 10.0, fetcher, cache, solver, True,
                cookie_lookup_domain="www.glassdoor.com",
            )
        # glassdoor.ca should be cached (the domain we solved for)
        assert cache.get("www.glassdoor.ca") is not None
        # glassdoor.com should ALSO be re-cached with final_url pointing to .ca
        entry = cache.get("www.glassdoor.com")
        assert entry is not None
        assert entry.final_url == "https://www.glassdoor.ca/"
        assert entry.cookies == new_cookies

    @pytest.mark.asyncio
    async def test_retry_timeout_returns_friendly_error(self):
        """Timeout during retry after solve returns user-friendly message. [#13]"""
        from fetchaller.tools.fetch import _handle_botfighter
        cf_body = b"window._cf_chl_opt = {}"
        result = self._make_result(status_code=403, body=cf_body)
        fetcher = MagicMock()
        fetcher.current_impersonate = "chrome131"

        mock_retry_fetcher = AsyncMock()
        mock_retry_fetcher.apply_cookies = AsyncMock()
        mock_retry_fetcher.pin_identity = MagicMock()
        mock_retry_fetcher.fetch = AsyncMock(side_effect=TimeoutError("timed out"))
        mock_retry_fetcher.close = AsyncMock()

        solver = AsyncMock()
        solver.solve = AsyncMock(return_value={
            "cookies": [{"name": "cf_clearance", "value": "abc"}],
            "user_agent": "Mozilla/5.0",
            "final_url": "https://example.com",
            "impersonate": "chrome131",
        })

        with patch("fetchaller.tools.fetch.ContentFetcher", return_value=mock_retry_fetcher):
            bf_result = await _handle_botfighter(result, "https://example.com", 10.0, fetcher, CookieCache(), solver, False)
        assert isinstance(bf_result, dict)
        assert "timed out" in bf_result["error"].lower() or "timeout" in bf_result["error"].lower()

    @pytest.mark.asyncio
    async def test_ssrf_validation_on_browser_redirect(self):
        """Browser redirect to private host is blocked. [#2]"""
        from fetchaller.tools.fetch import _handle_botfighter
        cf_body = b"window._cf_chl_opt = {}"
        result = self._make_result(status_code=403, body=cf_body)
        fetcher = MagicMock()
        fetcher.current_impersonate = "chrome131"

        mock_retry_fetcher = AsyncMock()
        mock_retry_fetcher.apply_cookies = AsyncMock()
        mock_retry_fetcher.pin_identity = MagicMock()
        mock_retry_fetcher.fetch = AsyncMock(return_value=self._make_result())
        mock_retry_fetcher.close = AsyncMock()

        solver = AsyncMock()
        solver.solve = AsyncMock(return_value={
            "cookies": [{"name": "cf_clearance", "value": "abc"}],
            "user_agent": "Mozilla/5.0",
            "final_url": "http://192.168.1.1/admin",  # Private host
            "impersonate": "chrome131",
        })

        cache = CookieCache()
        with patch("fetchaller.tools.fetch.ContentFetcher", return_value=mock_retry_fetcher):
            await _handle_botfighter(result, "https://example.com", 10.0, fetcher, cache, solver, False)
        # Should succeed but not cache private redirect URL
        entry = cache.get("example.com")
        assert entry is not None
        assert entry.final_url is None  # Private URL rejected
