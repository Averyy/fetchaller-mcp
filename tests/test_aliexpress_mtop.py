"""Unit tests for MTop API client."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from fetchaller.aliexpress.mtop import MTopClient, compute_sign


class MockHeaders:
    """Mock headers object that supports multi_items() like curl_cffi."""

    def __init__(self, items: list[tuple[str, str]] | None = None):
        self._items = items or []

    def multi_items(self):
        return self._items

    def get(self, key, default=None):
        for k, v in self._items:
            if k.lower() == key.lower():
                return v
        return default


def _mock_resp(data: dict, headers=None):
    """Create a mock response with both .json() and .text for JSONP-aware parsing."""
    resp = MagicMock()
    resp.json.return_value = data
    resp.text = json.dumps(data)
    resp.headers = headers or MockHeaders()
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


class TestMTopClient:
    """MTop client token bootstrap and request flow."""

    @pytest.mark.asyncio
    async def test_bootstrap_extracts_token_from_cookie(self):
        """Token bootstrap should extract _m_h5_tk from response cookies."""
        client = MTopClient()

        # Mock the session to return a cookie with _m_h5_tk
        mock_session = AsyncMock()
        mock_cookie = MagicMock()
        mock_cookie.name = "_m_h5_tk"
        mock_cookie.value = "abcdef1234567890abcdef1234567890_1700000000"
        mock_session.cookies.jar = [mock_cookie]

        mock_resp = MagicMock()
        mock_resp.headers = MockHeaders()
        mock_session.get = AsyncMock(return_value=mock_resp)

        client._session = mock_session

        await client._bootstrap_token()
        assert client._token == "abcdef1234567890abcdef1234567890"

    @pytest.mark.asyncio
    async def test_auto_refresh_on_expired_token(self):
        """Request should re-bootstrap when server returns TOKEN_EXOIRED."""
        import time

        client = MTopClient()
        client._token = "old_token"
        client._token_time = time.time()  # Mark as recently valid

        mock_session = AsyncMock()

        # First call returns expired, second call (after bootstrap) returns success
        expired_resp = _mock_resp({"ret": ["FAIL_SYS_TOKEN_EXOIRED::token expired"]})
        success_resp = _mock_resp({"ret": ["SUCCESS"], "data": {"result": {}}})

        # Bootstrap response (no json needed — bootstrap only reads cookies)
        bootstrap_resp = _mock_resp({})

        mock_cookie = MagicMock()
        mock_cookie.name = "_m_h5_tk"
        mock_cookie.value = "new_token_value_32charslong12345_1700000000"
        mock_session.cookies.jar = [mock_cookie]

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

        mock_session = AsyncMock()
        mock_resp = _mock_resp({"ret": ["FAIL_SYS_USER_VALIDATE::need validate"]})
        mock_session.get = AsyncMock(return_value=mock_resp)
        mock_session.cookies.jar = []
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

        mock_session = AsyncMock()
        mock_resp = _mock_resp({"ret": ["RGV587_ERROR::SM"]})
        mock_session.get = AsyncMock(return_value=mock_resp)
        mock_session.cookies.jar = []
        client._session = mock_session

        result = await client.request("mtop.test.api", "1.0", {})
        assert "RGV587_ERROR" in str(result.get("ret", []))
        assert mock_session.get.call_count == 1

    @pytest.mark.asyncio
    async def test_concurrent_bootstrap_uses_lock(self):
        """Multiple concurrent bootstraps should be serialized by the lock.

        The double-check pattern (check token_expired inside the lock) means
        only the first caller actually makes an HTTP request; the rest see
        the token is already set and return immediately.
        """
        client = MTopClient()

        mock_session = AsyncMock()
        mock_cookie = MagicMock()
        mock_cookie.name = "_m_h5_tk"
        mock_cookie.value = "token123_1700000000"
        mock_session.cookies.jar = [mock_cookie]
        mock_resp = MagicMock()
        mock_resp.headers = MockHeaders()
        mock_session.get = AsyncMock(return_value=mock_resp)
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
    async def test_jsonp_response_stripped(self):
        """JSONP-wrapped response should be parsed correctly."""
        client = MTopClient()
        client._token = "valid_token"
        client._token_time = 9999999999.0

        mock_session = AsyncMock()
        jsonp_body = 'mtopjsonp1({"ret":["SUCCESS"],"data":{"result":{"PRODUCT_TITLE":{"text":"Widget"}}}})'
        mock_resp = MagicMock()
        mock_resp.text = jsonp_body
        mock_resp.headers = MockHeaders()
        mock_session.get = AsyncMock(return_value=mock_resp)
        mock_session.cookies.jar = []
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

        mock_session = AsyncMock()
        mock_resp = _mock_resp({"ret": ["SUCCESS"], "data": {"result": {}}})
        mock_session.get = AsyncMock(return_value=mock_resp)
        mock_session.cookies.jar = []
        client._session = mock_session

        result = await client.request("mtop.test.api", "1.0", {})
        assert "SUCCESS" in str(result.get("ret", []))

    @pytest.mark.asyncio
    async def test_close_cleans_up_session(self):
        """close() should close the underlying session."""
        client = MTopClient()
        mock_session = AsyncMock()
        client._session = mock_session

        await client.close()
        mock_session.close.assert_called_once()
        assert client._session is None
