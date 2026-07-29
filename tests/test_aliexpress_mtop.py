"""Unit tests for MTop API client."""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from wafer import ResponseTooLarge

from fetchaller.aliexpress.mtop import (
    MTopClient,
    _issued_tmd_challenge_url,
    _parse_token_cookie,
    compute_sign,
)


def _mock_resp(data: dict, cookies: list[str] | None = None):
    """Create a mock response with .text, .get_all(), and .cookies.

    Args:
        data: JSON response data.
        cookies: List of Set-Cookie header values (e.g. ["_m_h5_tk=abc_123; Path=/"]).
    """
    resp = MagicMock()
    resp.json.return_value = data
    resp.text = json.dumps(data)

    cookie_list = cookies or []

    def get_all(name):
        if name.lower() == "set-cookie":
            return cookie_list
        return []
    resp.get_all = get_all

    # Mirror wafer's WaferResponse.cookies: exact name -> value, from Set-Cookie.
    resp.cookies = {
        c.split("=", 1)[0].strip(): c.split("=", 1)[1].split(";", 1)[0]
        for c in cookie_list if "=" in c
    }
    resp.headers = {}
    return resp


class TestComputeSign:
    """Sign computation with known inputs."""

    def test_known_values(self):
        """MD5 of 'token&timestamp&appKey&data' produces expected hash."""
        result = compute_sign("abc123", "1700000000000", "12574478", '{"productId":"123"}')
        # Verify it's a valid 32-char hex MD5
        assert len(result) == 32
        assert all(c in "0123456789abcdef" for c in result)

    def test_deterministic(self):
        """Same inputs always produce same sign."""
        args = ("token123", "1700000000000", "12574478", '{"key":"val"}')
        assert compute_sign(*args) == compute_sign(*args)

    def test_different_tokens_produce_different_signs(self):
        """Different tokens must produce different signatures."""
        s1 = compute_sign("tokenA", "1700000000000", "12574478", "{}")
        s2 = compute_sign("tokenB", "1700000000000", "12574478", "{}")
        assert s1 != s2

    def test_undefined_token(self):
        """Token 'undefined' (used during bootstrap) produces a valid sign."""
        result = compute_sign("undefined", "1700000000000", "12574478", "{}")
        assert len(result) == 32


class TestTokenCookieParsing:
    def test_real_cookie_shape(self):
        assert _parse_token_cookie(
            "abcdef0123456789abcdef0123456789_1700000000000"
        ) == "abcdef0123456789abcdef0123456789"

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            "_1700000000",
            "token_",
            "token_not-a-timestamp",
            "token_extra_parts_1700000000",
            "token\ncontrol_1700000000",
            ("x" * 129) + "_1700000000",
            "token_123",
            "token_12345678901234567",
        ],
    )
    def test_rejects_malformed_cookie(self, value):
        assert _parse_token_cookie(value) is None


class TestIssuedTmdChallengeUrl:
    _VALID_URL = (
        "https://acs.aliexpress.com:443//h5/mtop.aliexpress.pdp.pc.query/1.0/"
        "_____tmd_____/punish?x5secdata=issued-token&x5step=2"
    )

    def test_accepts_only_the_issued_acs_tmd_url(self):
        result = {"data": {"url": self._VALID_URL}}
        assert _issued_tmd_challenge_url(result) == self._VALID_URL

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            "http://acs.aliexpress.com/_____tmd_____/punish?x5secdata=x",
            "https://evil.example/_____tmd_____/punish?x5secdata=x",
            "https://acs.aliexpress.com/other?x5secdata=x",
            "https://acs.aliexpress.com/_____tmd_____/punish",
            "https://acs.aliexpress.com/_____tmd_____/punish-evil?x5secdata=x",
            "https://acs.aliexpress.com/_____tmd_____/punish?x5secdata=",
            "https://acs.aliexpress.com/_____tmd_____/punish?x5secdata=x&x5secdata=y",
            "https://acs.aliexpress.com/_____tmd_____/punish?x5secdata=x#fragment",
            "https://user@acs.aliexpress.com/_____tmd_____/punish?x5secdata=x",
            (
                "https://acs.aliexpress.com/arbitrary/prefix/"
                "_____tmd_____/punish?x5secdata=x"
            ),
            (
                "https://acs.aliexpress.com/h5/mtop.alibaba.product/1.0/"
                "_____tmd_____/punish?x5secdata=x"
            ),
            (
                "https://acs.aliexpress.com/h5/"
                "mtop.aliexpress.pdp.pc.query/latest/"
                "_____tmd_____/punish?x5secdata=x"
            ),
        ],
    )
    def test_rejects_untrusted_or_incomplete_url(self, value):
        assert _issued_tmd_challenge_url({"data": {"url": value}}) is None


class TestMTopClient:
    """MTop client token bootstrap and request flow."""

    @pytest.mark.asyncio
    @patch("fetchaller.aliexpress.mtop.wafer.AsyncSession")
    async def test_native_session_has_a_hard_response_cap(self, mock_session):
        client = MTopClient(browser_solver="solver")

        with patch(
            "fetchaller.aliexpress.mtop.get_wafer_cache_dir",
            return_value="/tmp/wafer-cache",
        ):
            assert await client._get_session() is mock_session.return_value
        mock_session.assert_called_once_with(
            max_rotations=0,
            cache_dir="/tmp/wafer-cache",
            browser_solver="solver",
            solve_origin="https://www.aliexpress.com/",
            max_response_size=5 * 1024 * 1024,
        )

    @pytest.mark.asyncio
    async def test_native_response_too_large_is_never_downgraded_to_parse_error(
        self,
    ):
        client = MTopClient()
        client._token = "valid_token"
        client._token_time = 9999999999.0
        mock_session = MagicMock()
        mock_session.get = AsyncMock(
            side_effect=ResponseTooLarge(
                "https://acs.aliexpress.com/h5/mtop.test.api/1.0/",
                5 * 1024 * 1024 + 1,
                5 * 1024 * 1024,
            )
        )
        client._session = mock_session

        with pytest.raises(ResponseTooLarge):
            await client.request("mtop.test.api", "1.0", {})

    @pytest.mark.asyncio
    async def test_bootstrap_extracts_token_from_cookie(self):
        """Token bootstrap should extract _m_h5_tk from response Set-Cookie."""
        client = MTopClient()

        mock_session = MagicMock()
        mock_resp = _mock_resp({}, cookies=[
            "_m_h5_tk=abcdef1234567890abcdef1234567890_1700000000; Path=/; Domain=.aliexpress.com",
            "_m_h5_tk_enc=somehash; Path=/; Domain=.aliexpress.com",
        ])
        mock_session.get = AsyncMock(return_value=mock_resp)
        mock_session.get_cookie.return_value = (
            "abcdef1234567890abcdef1234567890_1700000000"
        )
        client._session = mock_session

        await client._bootstrap_token()
        assert client._token == "abcdef1234567890abcdef1234567890"

    @pytest.mark.asyncio
    async def test_bootstrap_without_token_defers_to_issued_mtop_challenge(self):
        """Do not burn a generic browser timeout before an issued TMD URL."""
        client = MTopClient(browser_solver=object())
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=_mock_resp({}, cookies=[]))
        mock_session.get_cookie.return_value = None
        client._session = mock_session

        await client._bootstrap_token()

        mock_session.get.assert_awaited_once()
        mock_session.browser_prime.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_refresh_on_expired_token(self):
        """Request should re-bootstrap when server returns TOKEN_EXOIRED."""
        import time

        client = MTopClient()
        client._token = "old_token"
        client._token_time = time.time()  # Mark as recently valid

        mock_session = MagicMock()
        mock_session.get_cookie.side_effect = [
            None,
            "0123456789abcdef0123456789abcdef_1700000000",
            None,
        ]

        # First call returns expired, second call (after bootstrap) returns success
        expired_resp = _mock_resp(
            {"ret": ["FAIL_SYS_TOKEN_EXOIRED::token expired"]},
            cookies=[],
        )
        success_resp = _mock_resp(
            {"ret": ["SUCCESS"], "data": {"result": {}}},
            cookies=[],
        )

        # Bootstrap response sets new token cookie
        bootstrap_resp = _mock_resp({}, cookies=[
            "_m_h5_tk=0123456789abcdef0123456789abcdef_1700000000; Path=/",
        ])

        call_count = 0

        async def mock_get(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return expired_resp  # First _do_request → server says expired
            elif call_count == 2:
                return bootstrap_resp  # _bootstrap_token call
            else:
                return success_resp  # Retry _do_request after new token

        mock_session.get = mock_get
        client._session = mock_session

        result = await client.request("mtop.test.api", "1.0", {})
        assert "SUCCESS" in str(result.get("ret", []))
        assert call_count == 3  # request + bootstrap + retry

    @pytest.mark.asyncio
    async def test_error_classification_user_validate(self):
        """FAIL_SYS_USER_VALIDATE should be returned without retry."""
        client = MTopClient()
        client._token = "valid_token"
        client._token_time = 9999999999.0  # Not expired

        mock_session = MagicMock()
        mock_session.get_cookie.return_value = None
        mock_resp = _mock_resp(
            {"ret": ["FAIL_SYS_USER_VALIDATE::need validate"]},
            cookies=[],
        )
        mock_session.get = AsyncMock(return_value=mock_resp)
        client._session = mock_session

        result = await client.request("mtop.test.api", "1.0", {})
        assert "FAIL_SYS_USER_VALIDATE" in str(result.get("ret", []))
        # Should NOT have re-bootstrapped (only 1 request, not 2+bootstrap)
        assert mock_session.get.call_count == 1

    @pytest.mark.asyncio
    async def test_error_classification_rgv587(self):
        """RGV587_ERROR should be returned without retry."""
        client = MTopClient()
        client._token = "valid_token"
        client._token_time = 9999999999.0

        mock_session = MagicMock()
        mock_session.get_cookie.return_value = None
        mock_resp = _mock_resp(
            {"ret": ["RGV587_ERROR::SM"]},
            cookies=[],
        )
        mock_session.get = AsyncMock(return_value=mock_resp)
        client._session = mock_session

        result = await client.request("mtop.test.api", "1.0", {})
        assert "RGV587_ERROR" in str(result.get("ret", []))
        assert mock_session.get.call_count == 1

    @pytest.mark.asyncio
    async def test_expired_operation_deadline_stops_before_mtop_transport(self):
        client = MTopClient()
        client._token = "valid_token"
        client._token_time = 9999999999.0
        mock_session = MagicMock()
        mock_session.get = AsyncMock()
        client._session = mock_session

        with pytest.raises(TimeoutError, match="deadline exhausted"):
            await client.request(
                "mtop.test.api", "1.0", {}, deadline=time.monotonic() - 0.1
            )

        mock_session.get.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error_code",
        ["FAIL_SYS_USER_VALIDATE", "RGV587_ERROR"],
    )
    async def test_api_block_deliberately_primes_browser_then_retries_once(
        self, error_code
    ):
        client = MTopClient(browser_solver=object())
        client._token = "old_token"
        client._token_time = 9999999999.0

        mock_session = MagicMock()
        mock_session.browser_prime = AsyncMock(return_value=True)
        mock_session.get_cookie.side_effect = [
            None,
            "0123456789abcdef0123456789abcdef_1700000000",
            "0123456789abcdef0123456789abcdef_1700000000",
        ]
        mock_session.get = AsyncMock(
            side_effect=[
                _mock_resp({"ret": [f"{error_code}::blocked"]}),
                _mock_resp({"ret": ["SUCCESS"], "data": {"result": {}}}),
            ]
        )
        client._session = mock_session

        result = await client.request("mtop.test.api", "1.0", {})

        assert result["ret"] == ["SUCCESS"]
        assert mock_session.get.call_count == 2
        mock_session.browser_prime.assert_awaited_once_with(
            "https://www.aliexpress.com/",
            timeout=30,
            max_response_size=2 * 1024 * 1024,
        )
        assert client._token == "0123456789abcdef0123456789abcdef"

    @pytest.mark.asyncio
    async def test_issued_tmd_url_is_solved_directly_then_retried_once(self):
        client = MTopClient(browser_solver=object())
        client._token = "old_token"
        client._token_time = 9999999999.0
        challenge_url = (
            "https://acs.aliexpress.com/_____tmd_____/punish?"
            "x5secdata=issued-token"
        )

        mock_session = MagicMock()
        mock_session.browser_solve_challenge = AsyncMock(return_value=True)
        browser_token = "0123456789abcdef0123456789abcdef_1700000000"
        mock_session.get_cookie.side_effect = [None, browser_token, browser_token]
        mock_session.get = AsyncMock(
            side_effect=[
                _mock_resp(
                    {
                        "ret": ["FAIL_SYS_USER_VALIDATE::blocked"],
                        "data": {"url": challenge_url},
                    }
                ),
                _mock_resp({"ret": ["SUCCESS"], "data": {"result": {}}}),
            ]
        )
        client._session = mock_session

        result = await client.request("mtop.test.api", "1.0", {})

        assert result["ret"] == ["SUCCESS"]
        mock_session.browser_solve_challenge.assert_awaited_once_with(
            challenge_url,
            "tmd",
            timeout=165,
            max_response_size=2 * 1024 * 1024,
        )
        mock_session.browser_prime.assert_not_called()
        assert mock_session.get.call_count == 2
        assert client._token == "0123456789abcdef0123456789abcdef"

    @pytest.mark.asyncio
    async def test_tmd_solve_without_browser_token_bootstraps_under_clearance(self):
        client = MTopClient(browser_solver=object())
        challenge_url = (
            "https://acs.aliexpress.com/_____tmd_____/punish?"
            "x5secdata=issued-token"
        )
        mock_session = MagicMock()
        mock_session.browser_solve_challenge = AsyncMock(return_value=True)
        mock_session.get_cookie.return_value = None
        client._session = mock_session
        bootstrap_calls = 0

        async def bootstrap(_deadline=None):
            nonlocal bootstrap_calls
            bootstrap_calls += 1
            if bootstrap_calls == 2:
                client._set_token_cookie(
                    "postclearance_1700000000001"
                )

        client._bootstrap_token = bootstrap
        client._do_request = AsyncMock(
            side_effect=[
                {
                    "ret": ["FAIL_SYS_USER_VALIDATE::blocked"],
                    "data": {"url": challenge_url},
                },
                {"ret": ["SUCCESS"], "data": {"result": {}}},
            ]
        )

        result = await client.request("mtop.test.api", "1.0", {})

        assert result["ret"] == ["SUCCESS"]
        assert bootstrap_calls == 2
        assert client._do_request.await_count == 2

    @pytest.mark.asyncio
    async def test_tmd_post_clearance_bootstrap_failure_does_not_retry_unsigned(self):
        client = MTopClient(browser_solver=object())
        challenge_url = (
            "https://acs.aliexpress.com/_____tmd_____/punish?"
            "x5secdata=issued-token"
        )
        mock_session = MagicMock()
        mock_session.browser_solve_challenge = AsyncMock(return_value=True)
        mock_session.get_cookie.return_value = None
        client._session = mock_session
        bootstrap_calls = 0

        async def bootstrap(_deadline=None):
            nonlocal bootstrap_calls
            bootstrap_calls += 1

        client._bootstrap_token = bootstrap
        blocked = {
            "ret": ["FAIL_SYS_USER_VALIDATE::blocked"],
            "data": {"url": challenge_url},
        }
        client._do_request = AsyncMock(return_value=blocked)

        assert await client.request("mtop.test.api", "1.0", {}) == blocked
        assert bootstrap_calls == 2
        client._do_request.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_tmd_browser_token_never_overwrites_concurrent_newer_token(self):
        client = MTopClient(browser_solver=object())
        solve_started = asyncio.Event()
        release_solve = asyncio.Event()
        mock_session = MagicMock()

        async def solve(*_args, **_kwargs):
            solve_started.set()
            await release_solve.wait()
            return True

        mock_session.browser_solve_challenge = solve
        mock_session.get_cookie.return_value = "browser_1700000000002"
        client._session = mock_session

        task = asyncio.create_task(
            client._solve_issued_tmd_challenge("https://acs.example/tmd", 0)
        )
        await solve_started.wait()
        client._set_token_cookie("newer_1700000000003")
        release_solve.set()

        assert await task
        assert client._token == "newer"

    @pytest.mark.asyncio
    async def test_issued_tmd_solve_is_capped_by_operation_deadline(self):
        client = MTopClient(browser_solver=object())
        client._token = "old_token"
        client._token_time = 9999999999.0
        challenge_url = (
            "https://acs.aliexpress.com/_____tmd_____/punish?"
            "x5secdata=issued-token"
        )
        mock_session = MagicMock()
        mock_session.browser_solve_challenge = AsyncMock(return_value=False)
        mock_session.get_cookie.return_value = None
        mock_session.get = AsyncMock(
            return_value=_mock_resp(
                {
                    "ret": ["FAIL_SYS_USER_VALIDATE::blocked"],
                    "data": {"url": challenge_url},
                }
            )
        )
        client._session = mock_session
        deadline = time.monotonic() + 0.2

        await client.request("mtop.test.api", "1.0", {}, deadline=deadline)

        kwargs = mock_session.browser_solve_challenge.call_args.kwargs
        assert 0 < kwargs["timeout"] <= 0.2

    @pytest.mark.asyncio
    async def test_concurrent_issued_tmd_recovery_shares_one_browser_solve(self):
        """A shared clearance must unblock every waiting MTop retry."""
        client = MTopClient(browser_solver=object())
        client._token = "old_token"
        client._token_time = 9999999999.0
        first_url = (
            "https://acs.aliexpress.com/_____tmd_____/punish?x5secdata=first"
        )
        second_url = (
            "https://acs.aliexpress.com/_____tmd_____/punish?x5secdata=second"
        )
        solve_started = asyncio.Event()
        release_solve = asyncio.Event()
        initial_requests = asyncio.Event()
        requests_started = 0
        calls = {"first": 0, "second": 0}

        async def solve(*_args, **_kwargs):
            solve_started.set()
            await release_solve.wait()
            return True

        async def do_request(_api, _version, data):
            nonlocal requests_started
            name = data["name"]
            calls[name] += 1
            if calls[name] == 1:
                requests_started += 1
                if requests_started == 2:
                    initial_requests.set()
                await initial_requests.wait()
                return {
                    "ret": ["FAIL_SYS_USER_VALIDATE::blocked"],
                    "data": {"url": first_url if name == "first" else second_url},
                }
            return {"ret": ["SUCCESS"], "data": {"result": {}}}

        mock_session = MagicMock()
        mock_session.browser_solve_challenge = AsyncMock(side_effect=solve)
        client._session = mock_session
        client._do_request = AsyncMock(side_effect=do_request)

        first_task = asyncio.create_task(
            client.request("mtop.test", "1.0", {"name": "first"})
        )
        second_task = asyncio.create_task(
            client.request("mtop.test", "1.0", {"name": "second"})
        )
        await solve_started.wait()
        release_solve.set()
        results = await asyncio.gather(first_task, second_task)

        assert [result["ret"] for result in results] == [
            ["SUCCESS"],
            ["SUCCESS"],
        ]
        assert mock_session.browser_solve_challenge.await_count == 1
        assert calls == {"first": 2, "second": 2}
        assert client._tmd_recovery_generation == 1

    @pytest.mark.asyncio
    async def test_failed_browser_prime_does_not_replay_blocked_request(self):
        client = MTopClient(browser_solver=object())
        client._token = "valid_token"
        client._token_time = 9999999999.0

        mock_session = MagicMock()
        mock_session.browser_prime = AsyncMock(return_value=False)
        mock_session.get_cookie.return_value = None
        mock_session.get = AsyncMock(
            return_value=_mock_resp(
                {"ret": ["FAIL_SYS_USER_VALIDATE::blocked"]}
            )
        )
        client._session = mock_session

        result = await client.request("mtop.test.api", "1.0", {})

        assert "FAIL_SYS_USER_VALIDATE" in result["ret"][0]
        assert mock_session.get.call_count == 1
        mock_session.browser_prime.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_concurrent_browser_recovery_is_deduplicated(self):
        client = MTopClient(browser_solver=object())
        mock_session = MagicMock()
        mock_session.browser_prime = AsyncMock(return_value=True)
        mock_session.get_cookie.return_value = (
            "abcdef0123456789abcdef0123456789_1700000000"
        )
        client._session = mock_session

        recovered = await asyncio.gather(
            client._warm_origin_for_token(0),
            client._warm_origin_for_token(0),
            client._warm_origin_for_token(0),
        )

        assert recovered == [True, True, True]
        mock_session.browser_prime.assert_awaited_once()
        assert client._browser_prime_generation == 1

    @pytest.mark.asyncio
    async def test_request_generation_is_captured_before_blocked_call(self):
        client = MTopClient(browser_solver=object())
        client._token = "valid"
        client._token_time = 9999999999.0
        mock_session = MagicMock()
        mock_session.browser_prime = AsyncMock(return_value=True)
        mock_session.get_cookie.return_value = (
            "abcdef0123456789abcdef0123456789_1700000000"
        )
        client._session = mock_session
        first_recovery_finished = asyncio.Event()
        both_requests_started = asyncio.Event()
        initial_requests = 0
        calls = {"first": 0, "second": 0}

        async def do_request(_api, _version, data):
            nonlocal initial_requests
            request_name = data["request"]
            calls[request_name] += 1
            if calls[request_name] == 1:
                initial_requests += 1
                if initial_requests == 2:
                    both_requests_started.set()
                await both_requests_started.wait()
                if request_name == "first":
                    return {"ret": ["FAIL_SYS_USER_VALIDATE::blocked"]}
                await first_recovery_finished.wait()
                return {"ret": ["FAIL_SYS_USER_VALIDATE::blocked"]}
            if request_name == "first":
                first_recovery_finished.set()
                return {"ret": ["SUCCESS"]}
            return {"ret": ["SUCCESS"]}

        client._do_request = AsyncMock(side_effect=do_request)

        results = await asyncio.gather(
            client.request("mtop.test", "1.0", {"request": "first"}),
            client.request("mtop.test", "1.0", {"request": "second"}),
        )

        assert results == [
            {"ret": ["SUCCESS"]},
            {"ret": ["SUCCESS"]},
        ]
        mock_session.browser_prime.assert_awaited_once()
        assert client._browser_prime_generation == 1

    @pytest.mark.asyncio
    async def test_stale_expiry_response_does_not_clear_newer_token(self):
        client = MTopClient()
        client._token = "oldtoken"
        client._token_cookie = "oldtoken_1700000000000"
        client._token_time = 9999999999.0
        stale_request_started = asyncio.Event()
        install_new_token = asyncio.Event()
        tokens_used = []

        async def do_request(_api, _version, _data):
            tokens_used.append(client._token)
            if len(tokens_used) == 1:
                stale_request_started.set()
                await install_new_token.wait()
                return {"ret": ["FAIL_SYS_TOKEN_EXOIRED::stale response"]}
            return {"ret": ["SUCCESS"]}

        client._do_request = AsyncMock(side_effect=do_request)

        task = asyncio.create_task(client.request("mtop.test", "1.0", {}))
        await stale_request_started.wait()
        client._set_token_cookie("newtoken_1700000000001")
        install_new_token.set()

        assert await task == {"ret": ["SUCCESS"]}
        assert client._token == "newtoken"
        assert tokens_used == ["oldtoken", "newtoken"]

    @pytest.mark.asyncio
    async def test_browser_prime_does_not_overwrite_newer_token(self):
        client = MTopClient(browser_solver=object())
        prime_started = asyncio.Event()
        finish_prime = asyncio.Event()
        mock_session = MagicMock()

        async def browser_prime(*_args, **_kwargs):
            prime_started.set()
            await finish_prime.wait()
            return True

        mock_session.browser_prime = browser_prime
        mock_session.get_cookie.return_value = "primedtoken_1700000000002"
        client._session = mock_session

        task = asyncio.create_task(client._warm_origin_for_token(0))
        await prime_started.wait()
        client._set_token_cookie("newtoken_1700000000001")
        finish_prime.set()

        assert await task
        assert client._token == "newtoken"
        assert client._browser_prime_generation == 0

    @pytest.mark.asyncio
    async def test_browser_prime_runtime_error_is_recovery_failure(self):
        client = MTopClient(browser_solver=object())
        mock_session = MagicMock()
        mock_session.browser_prime = AsyncMock(
            side_effect=RuntimeError("solver closed")
        )
        client._session = mock_session

        assert not await client._warm_origin_for_token(0)
        assert client._browser_prime_generation == 0
        assert client._token == ""

    @pytest.mark.asyncio
    async def test_session_creation_error_is_recovery_failure(self):
        client = MTopClient(browser_solver=object())
        client._get_session = AsyncMock(
            side_effect=RuntimeError("session unavailable")
        )

        assert not await client._warm_origin_for_token(0)
        assert client._browser_prime_generation == 0
        assert client._token == ""

    @pytest.mark.asyncio
    async def test_cookie_lookup_error_is_recovery_failure(self):
        client = MTopClient(browser_solver=object())
        mock_session = MagicMock()
        mock_session.browser_prime = AsyncMock(return_value=True)
        mock_session.get_cookie.side_effect = RuntimeError("jar unavailable")
        client._session = mock_session

        assert not await client._warm_origin_for_token(0)
        assert client._browser_prime_generation == 0
        assert client._token == ""

    @pytest.mark.asyncio
    async def test_malformed_browser_token_never_advances_generation(self):
        client = MTopClient(browser_solver=object())
        mock_session = MagicMock()
        mock_session.browser_prime = AsyncMock(return_value=True)
        mock_session.get_cookie.return_value = "_1700000000"
        client._session = mock_session

        recovered = await asyncio.gather(
            client._warm_origin_for_token(0),
            client._warm_origin_for_token(0),
            client._warm_origin_for_token(0),
        )

        assert recovered == [False, False, False]
        assert mock_session.browser_prime.await_count == 3
        assert client._browser_prime_generation == 0
        assert client._token == ""

    @pytest.mark.asyncio
    async def test_concurrent_bootstrap_uses_lock(self):
        """Multiple concurrent bootstraps should be serialized by the lock.

        The double-check pattern (check token_expired inside the lock) means
        only the first caller actually makes an HTTP request; the rest see
        the token is already set and return immediately.
        """
        client = MTopClient()

        mock_session = MagicMock()
        mock_resp = _mock_resp({}, cookies=[
            "_m_h5_tk=token123_1700000000; Path=/",
        ])
        mock_session.get = AsyncMock(return_value=mock_resp)
        mock_session.get_cookie.return_value = "token123_1700000000"
        client._session = mock_session

        # Launch 3 concurrent bootstraps — only 1 should hit the network
        await asyncio.gather(
            client._bootstrap_token(),
            client._bootstrap_token(),
            client._bootstrap_token(),
        )

        # Double-check pattern: first call bootstraps, others see token is set
        assert mock_session.get.call_count == 1
        assert client._token == "token123"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "lock_name",
        ["_bootstrap_lock", "_browser_prime_lock", "_tmd_recovery_lock"],
    )
    async def test_recovery_lock_wait_respects_operation_deadline(self, lock_name):
        client = MTopClient()
        lock = getattr(client, lock_name)
        await lock.acquire()
        try:
            with pytest.raises(TimeoutError, match="deadline exhausted"):
                async with client._deadline_lock(
                    lock, time.monotonic() + 0.01
                ):
                    pytest.fail("contended recovery lock must not be acquired")
        finally:
            lock.release()

    @pytest.mark.asyncio
    async def test_jsonp_response_stripped(self):
        """JSONP-wrapped response should be parsed correctly."""
        client = MTopClient()
        client._token = "valid_token"
        client._token_time = 9999999999.0

        mock_session = MagicMock()
        mock_session.get_cookie.return_value = None
        jsonp_body = 'mtopjsonp1({"ret":["SUCCESS"],"data":{"result":{"PRODUCT_TITLE":{"text":"Widget"}}}})'
        mock_resp = MagicMock()
        mock_resp.text = jsonp_body

        def get_all(name):
            return []
        mock_resp.get_all = get_all
        mock_resp.headers = {}
        mock_session.get = AsyncMock(return_value=mock_resp)
        client._session = mock_session

        result = await client.request("mtop.test.api", "1.0", {})
        assert "SUCCESS" in str(result.get("ret", []))
        assert result["data"]["result"]["PRODUCT_TITLE"]["text"] == "Widget"

    @pytest.mark.asyncio
    async def test_plain_json_response_works(self):
        """Standard JSON response (no JSONP wrapper) should still parse."""
        client = MTopClient()
        client._token = "valid_token"
        client._token_time = 9999999999.0

        mock_session = MagicMock()
        mock_session.get_cookie.return_value = None
        mock_resp = _mock_resp(
            {"ret": ["SUCCESS"], "data": {"result": {}}},
            cookies=[],
        )
        mock_session.get = AsyncMock(return_value=mock_resp)
        client._session = mock_session

        result = await client.request("mtop.test.api", "1.0", {})
        assert "SUCCESS" in str(result.get("ret", []))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("body", ["[]", "null", '"not an object"', "42"])
    async def test_non_object_json_response_is_parse_error(self, body):
        client = MTopClient()
        mock_session = MagicMock()
        mock_session.get_cookie.return_value = None
        mock_resp = MagicMock()
        mock_resp.text = body
        mock_session.get = AsyncMock(return_value=mock_resp)
        client._session = mock_session

        assert await client._do_request("mtop.test.api", "1.0", {}) == {
            "ret": ["PARSE_ERROR"],
            "data": {},
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ret", [[1, None, {"error": "unexpected"}], {"error": "unexpected"}])
    async def test_non_string_ret_values_do_not_crash_error_handling(self, ret):
        client = MTopClient()
        client._token = "validtoken"
        client._token_time = 9999999999.0
        mock_session = MagicMock()
        mock_session.get_cookie.return_value = None
        mock_session.get = AsyncMock(return_value=_mock_resp({"ret": ret}))
        client._session = mock_session

        assert await client.request("mtop.test.api", "1.0", {}) == {"ret": ret}

    @pytest.mark.asyncio
    async def test_close_releases_session(self):
        """close() should release the session reference."""
        client = MTopClient()
        client._session = AsyncMock()

        await client.close()
        assert client._session is None
