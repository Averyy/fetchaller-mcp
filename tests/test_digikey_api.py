"""Tests for DigiKey API client (digikey/api.py).

Tests URL parsing, token manager, product formatting, search, and error handling.
API calls are mocked with httpx.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from fetchaller.digikey.api import (
    TokenManager,
    _format_product,
    _format_search_results,
    extract_part_from_url,
    extract_search_keyword,
    get_product,
)

# ---------------------------------------------------------------------------
# Sample API response data
# ---------------------------------------------------------------------------

SAMPLE_PRODUCT = {
    "Description": {
        "DetailedDescription": "IC OPAMP GP 2 CIRCUIT 8DIP",
        "ProductDescription": "Dual Op Amp",
    },
    "ManufacturerProductNumber": "LM358P",
    "Manufacturer": {"Name": "Texas Instruments"},
    "Category": {"Name": "Operational Amplifiers - Op Amps"},
    "ProductStatus": {"Status": "Active"},
    "DatasheetUrl": "https://www.ti.com/lit/ds/symlink/lm358.pdf",
    "QuantityAvailable": 16563,
    "ManufacturerLeadWeeks": "12",
    "ProductVariations": [
        {
            "DigiKeyProductNumber": "296-1395-5-ND",
            "PackageType": {"Id": 6, "Name": "Tube"},
            "StandardPricing": [
                {"BreakQuantity": 1, "UnitPrice": 0.414},
                {"BreakQuantity": 10, "UnitPrice": 0.287},
                {"BreakQuantity": 100, "UnitPrice": 0.218},
            ],
            "MinimumOrderQuantity": 1,
            "StandardPackage": 50,
        }
    ],
    "Parameters": [
        {"ParameterText": "Package / Case", "ValueText": "8-DIP"},
        {"ParameterText": "Supply Voltage", "ValueText": "3V ~ 32V"},
    ],
    "ProductUrl": "/en/products/detail/texas-instruments/LM358P/277042",
}

SAMPLE_SEARCH_RESPONSE = {
    "ProductsCount": 42,
    "Products": [SAMPLE_PRODUCT],
}

SAMPLE_TOKEN_RESPONSE = {
    "access_token": "test-token-123",
    "token_type": "Bearer",
    "expires_in": 599,
}


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------


class TestURLParsing:
    def test_extract_part_from_product_url(self):
        url = "https://www.digikey.com/en/products/detail/texas-instruments/LM358P/277042"
        result = extract_part_from_url(url)
        assert result == ("texas-instruments", "LM358P", "277042")

    def test_extract_part_from_ca_url(self):
        url = "https://www.digikey.ca/en/products/detail/stmicro/STM32F103C8T6/1754198"
        result = extract_part_from_url(url)
        assert result == ("stmicro", "STM32F103C8T6", "1754198")

    def test_extract_part_without_locale_prefix(self):
        url = "https://www.digikey.com/products/detail/ti/LM358P/277042"
        result = extract_part_from_url(url)
        assert result == ("ti", "LM358P", "277042")

    def test_extract_part_returns_none_for_search(self):
        url = "https://www.digikey.com/en/products/result?keywords=LM358"
        assert extract_part_from_url(url) is None

    def test_extract_part_returns_none_for_generic(self):
        url = "https://www.digikey.com/"
        assert extract_part_from_url(url) is None

    def test_extract_search_keyword(self):
        url = "https://www.digikey.com/en/products/result?keywords=LM358"
        assert extract_search_keyword(url) == "LM358"

    def test_extract_search_keyword_ca(self):
        url = "https://www.digikey.ca/en/products/result?keywords=ESP32"
        assert extract_search_keyword(url) == "ESP32"

    def test_extract_search_keyword_returns_none_for_product(self):
        url = "https://www.digikey.com/en/products/detail/ti/LM358P/277042"
        assert extract_search_keyword(url) is None

    def test_extract_search_keyword_returns_none_for_missing_param(self):
        url = "https://www.digikey.com/en/products/result?category=opamps"
        assert extract_search_keyword(url) is None


# ---------------------------------------------------------------------------
# Token manager
# ---------------------------------------------------------------------------


class TestTokenManager:
    @pytest.mark.asyncio
    async def test_ensure_token_fetches_when_expired(self):
        tm = TokenManager("id", "secret")
        mock_resp = httpx.Response(
            200,
            json=SAMPLE_TOKEN_RESPONSE,
            request=httpx.Request("POST", "https://test"),
        )
        with patch("fetchaller.digikey.api.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            token = await tm.ensure_token()
            assert token == "test-token-123"
            assert tm._token == "test-token-123"
            assert tm.is_expired is False

    @pytest.mark.asyncio
    async def test_ensure_token_reuses_valid_token(self):
        tm = TokenManager("id", "secret")
        tm._token = "cached-token"
        tm._token_expires = time.time() + 600

        with patch("fetchaller.digikey.api.httpx.AsyncClient") as mock_client_cls:
            token = await tm.ensure_token()
            assert token == "cached-token"
            # Should not have made any HTTP call
            mock_client_cls.assert_not_called()


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


class TestFormatProduct:
    def test_format_product_heading(self):
        md = _format_product(SAMPLE_PRODUCT)
        assert md.startswith("# LM358P \u2014 Dual Op Amp")

    def test_format_product_contains_manufacturer(self):
        md = _format_product(SAMPLE_PRODUCT)
        assert "**Manufacturer:** Texas Instruments" in md

    def test_format_product_contains_dk_pn(self):
        md = _format_product(SAMPLE_PRODUCT)
        assert "**DigiKey Part #:** 296-1395-5-ND" in md

    def test_format_product_contains_package_type(self):
        md = _format_product(SAMPLE_PRODUCT)
        assert "- **Package Type:** Tube" in md

    def test_format_product_contains_pricing_table(self):
        md = _format_product(SAMPLE_PRODUCT)
        assert "| 1 | $0.414 |" in md
        assert "| 100 | $0.218 |" in md

    def test_format_product_contains_specs(self):
        md = _format_product(SAMPLE_PRODUCT)
        assert "- **Package / Case:** 8-DIP" in md
        assert "- **Supply Voltage:** 3V ~ 32V" in md

    def test_format_product_contains_datasheet(self):
        md = _format_product(SAMPLE_PRODUCT)
        assert "**Datasheet:** https://www.ti.com/lit/ds/symlink/lm358.pdf" in md

    def test_format_product_contains_stock(self):
        md = _format_product(SAMPLE_PRODUCT)
        assert "**In Stock:** 16,563" in md

    def test_format_product_url_prepends_domain(self):
        md = _format_product(SAMPLE_PRODUCT)
        assert "https://www.digikey.ca/en/products/detail/texas-instruments/LM358P/277042" in md

    def test_format_product_minimal(self):
        """Product with minimal fields should not crash."""
        md = _format_product({"ManufacturerProductNumber": "X", "Description": "Y"})
        assert "# X" in md


class TestFormatSearchResults:
    def test_format_search_results(self):
        md = _format_search_results([SAMPLE_PRODUCT], 42)
        assert "**42 results found** (showing 1)" in md
        assert "LM358P" in md

    def test_format_search_results_empty(self):
        md = _format_search_results([], 0)
        assert md == "No results found."


# ---------------------------------------------------------------------------
# get_product entry point
# ---------------------------------------------------------------------------


def _mock_httpx_client(post_responses=None, get_responses=None):
    """Create a mock httpx.AsyncClient context manager."""
    mock_client = AsyncMock()
    if post_responses is not None:
        if isinstance(post_responses, list):
            mock_client.post.side_effect = post_responses
        else:
            mock_client.post.return_value = post_responses
    if get_responses is not None:
        if isinstance(get_responses, list):
            mock_client.get.side_effect = get_responses
        else:
            mock_client.get.return_value = get_responses
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client


class TestGetProduct:
    @pytest.mark.asyncio
    async def test_missing_credentials_returns_error(self):
        with patch.dict("os.environ", {}, clear=True):
            result = await get_product(
                "https://www.digikey.com/en/products/detail/ti/LM358P/277042",
                client_id="",
                client_secret="",
            )
            assert "error" in result
            assert "DIGIKEY_CLIENT_ID" in result["error"]

    @pytest.mark.asyncio
    async def test_product_url_returns_content(self):
        token_resp = httpx.Response(200, json=SAMPLE_TOKEN_RESPONSE, request=httpx.Request("POST", "https://test"))
        product_resp = httpx.Response(200, json={"Product": SAMPLE_PRODUCT}, request=httpx.Request("GET", "https://test"))

        with patch("fetchaller.digikey.api.httpx.AsyncClient") as mock_client_cls:
            mock_client = _mock_httpx_client(post_responses=token_resp, get_responses=product_resp)
            mock_client_cls.return_value = mock_client

            from fetchaller.digikey.api import _token_managers, digikey_limiter
            _token_managers.clear()
            digikey_limiter._last_time = 0.0

            result = await get_product(
                "https://www.digikey.com/en/products/detail/texas-instruments/LM358P/277042",
                client_id="test-id",
                client_secret="test-secret",
            )
            assert "content" in result
            assert "LM358P" in result["content"]
            assert "Texas Instruments" in result["content"]

    @pytest.mark.asyncio
    async def test_search_url_returns_content(self):
        token_resp = httpx.Response(200, json=SAMPLE_TOKEN_RESPONSE, request=httpx.Request("POST", "https://test"))
        search_resp = httpx.Response(200, json=SAMPLE_SEARCH_RESPONSE, request=httpx.Request("POST", "https://test"))

        call_count = 0

        async def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return token_resp  # Token request
            return search_resp  # Search request

        with patch("fetchaller.digikey.api.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=mock_post)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            from fetchaller.digikey.api import _token_managers, digikey_limiter
            _token_managers.clear()
            digikey_limiter._last_time = 0.0

            result = await get_product(
                "https://www.digikey.com/en/products/result?keywords=LM358",
                client_id="test-id",
                client_secret="test-secret",
            )
            assert "content" in result
            assert "42 results found" in result["content"]

    @pytest.mark.asyncio
    async def test_unrecognized_url_returns_error(self):
        from fetchaller.digikey.api import _token_managers
        _token_managers.clear()

        result = await get_product(
            "https://www.digikey.com/",
            client_id="test-id",
            client_secret="test-secret",
        )
        assert "error" in result
        assert "Could not extract" in result["error"]

    @pytest.mark.asyncio
    async def test_http_401_returns_auth_error(self):
        token_resp = httpx.Response(200, json=SAMPLE_TOKEN_RESPONSE, request=httpx.Request("POST", "https://test"))
        error_resp = httpx.Response(401, request=httpx.Request("GET", "https://test"))

        with patch("fetchaller.digikey.api.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = token_resp
            mock_client.get.side_effect = httpx.HTTPStatusError(
                "401", request=error_resp.request, response=error_resp,
            )
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            from fetchaller.digikey.api import _token_managers, digikey_limiter
            _token_managers.clear()
            digikey_limiter._last_time = 0.0

            result = await get_product(
                "https://www.digikey.com/en/products/detail/ti/LM358P/277042",
                client_id="test-id",
                client_secret="test-secret",
            )
            assert "error" in result
            assert "authentication" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_http_429_returns_rate_limit_error(self):
        token_resp = httpx.Response(200, json=SAMPLE_TOKEN_RESPONSE, request=httpx.Request("POST", "https://test"))
        error_resp = httpx.Response(429, request=httpx.Request("GET", "https://test"))

        with patch("fetchaller.digikey.api.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = token_resp
            mock_client.get.side_effect = httpx.HTTPStatusError(
                "429", request=error_resp.request, response=error_resp,
            )
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            from fetchaller.digikey.api import _token_managers, digikey_limiter
            _token_managers.clear()
            digikey_limiter._last_time = 0.0

            result = await get_product(
                "https://www.digikey.com/en/products/detail/ti/LM358P/277042",
                client_id="test-id",
                client_secret="test-secret",
            )
            assert "error" in result
            assert "rate limit" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_timeout_returns_error(self):
        token_resp = httpx.Response(200, json=SAMPLE_TOKEN_RESPONSE, request=httpx.Request("POST", "https://test"))

        with patch("fetchaller.digikey.api.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = token_resp
            mock_client.get.side_effect = httpx.TimeoutException("timeout")
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            from fetchaller.digikey.api import _token_managers, digikey_limiter
            _token_managers.clear()
            digikey_limiter._last_time = 0.0

            result = await get_product(
                "https://www.digikey.com/en/products/detail/ti/LM358P/277042",
                client_id="test-id",
                client_secret="test-secret",
            )
            assert "error" in result
            assert "timed out" in result["error"].lower()
