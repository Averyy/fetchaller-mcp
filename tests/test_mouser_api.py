"""Tests for Mouser API client (mouser/api.py).

Tests URL parsing, part formatting, search results formatting, and error handling.
API calls are mocked with httpx.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from fetchaller.mouser.api import (
    _extract_parts,
    _extract_total,
    _format_part,
    _format_search_results,
    extract_mpn_from_url,
    extract_search_keyword,
    get_product,
    search_by_keyword,
    search_by_part_number,
)

# ---------------------------------------------------------------------------
# Sample API response data
# ---------------------------------------------------------------------------

SAMPLE_PART = {
    "Description": "Dual Op Amp",
    "ManufacturerPartNumber": "LM358P",
    "Manufacturer": "Texas Instruments",
    "MouserPartNumber": "595-LM358P",
    "Category": "Operational Amplifiers - Op Amps",
    "LifecycleStatus": "Active",
    "ROHSStatus": "RoHS Compliant",
    "DataSheetUrl": "https://www.mouser.com/datasheet/lm358.pdf",
    "Availability": "16,563",
    "LeadTime": "84 Days",
    "Min": "1",
    "Mult": "1",
    "PriceBreaks": [
        {"Quantity": 1, "Price": "$0.414", "Currency": "CAD"},
        {"Quantity": 10, "Price": "$0.287", "Currency": "CAD"},
        {"Quantity": 100, "Price": "$0.218", "Currency": "CAD"},
    ],
    "ProductAttributes": [
        {"AttributeName": "Packaging", "AttributeValue": "Tube"},
        {"AttributeName": "Standard Pack Qty", "AttributeValue": "50"},
    ],
    "ProductDetailUrl": "https://www.mouser.com/ProductDetail/Texas-Instruments/LM358P",
}

SAMPLE_SEARCH_RESPONSE = {
    "SearchResults": {
        "NumberOfResult": 42,
        "Parts": [SAMPLE_PART],
    }
}

SAMPLE_EMPTY_RESPONSE = {
    "SearchResults": {
        "NumberOfResult": 0,
        "Parts": [],
    }
}


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------


class TestURLParsing:
    def test_extract_mpn_from_product_url(self):
        url = "https://www.mouser.com/ProductDetail/Texas-Instruments/LM358P?qs=abcdef"
        assert extract_mpn_from_url(url) == "LM358P"

    def test_extract_mpn_from_product_url_no_query(self):
        url = "https://www.mouser.com/ProductDetail/Espressif/ESP32-S3-WROOM-1"
        assert extract_mpn_from_url(url) == "ESP32-S3-WROOM-1"

    def test_extract_mpn_returns_none_for_search(self):
        url = "https://www.mouser.com/Search/Refine?Keyword=LM358"
        assert extract_mpn_from_url(url) is None

    def test_extract_mpn_returns_none_for_generic(self):
        url = "https://www.mouser.com/"
        assert extract_mpn_from_url(url) is None

    def test_extract_search_keyword(self):
        url = "https://www.mouser.com/Search/Refine?Keyword=LM358"
        assert extract_search_keyword(url) == "LM358"

    def test_extract_search_keyword_returns_none_for_product(self):
        url = "https://www.mouser.com/ProductDetail/TI/LM358P"
        assert extract_search_keyword(url) is None

    def test_extract_search_keyword_returns_none_for_missing_param(self):
        url = "https://www.mouser.com/Search/Refine?category=OpAmps"
        assert extract_search_keyword(url) is None


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


class TestFormatPart:
    def test_format_part_heading(self):
        md = _format_part(SAMPLE_PART)
        assert md.startswith("# LM358P \u2014 Dual Op Amp")

    def test_format_part_contains_manufacturer(self):
        md = _format_part(SAMPLE_PART)
        assert "**Manufacturer:** Texas Instruments" in md

    def test_format_part_contains_mouser_pn(self):
        md = _format_part(SAMPLE_PART)
        assert "**Mouser Part #:** 595-LM358P" in md

    def test_format_part_contains_pricing_table(self):
        md = _format_part(SAMPLE_PART)
        assert "| 1 | CAD $0.414 |" in md
        assert "| 100 | CAD $0.218 |" in md

    def test_format_part_contains_specs(self):
        md = _format_part(SAMPLE_PART)
        assert "- **Packaging:** Tube" in md
        assert "- **Standard Pack Qty:** 50" in md

    def test_format_part_contains_datasheet(self):
        md = _format_part(SAMPLE_PART)
        assert "**Datasheet:** https://www.mouser.com/datasheet/lm358.pdf" in md

    def test_format_part_contains_product_url(self):
        md = _format_part(SAMPLE_PART)
        assert "https://www.mouser.com/ProductDetail/Texas-Instruments/LM358P" in md

    def test_format_part_minimal(self):
        """Part with minimal fields should not crash."""
        md = _format_part({"ManufacturerPartNumber": "X", "Description": "Y"})
        assert "# X \u2014 Y" in md

    def test_format_part_stock_section(self):
        md = _format_part(SAMPLE_PART)
        assert "**In Stock:** 16,563" in md
        assert "**Lead Time:** 84 Days" in md


class TestFormatSearchResults:
    def test_format_search_results(self):
        md = _format_search_results([SAMPLE_PART], 42)
        assert "**42 results found** (showing 1)" in md
        assert "LM358P" in md

    def test_format_search_results_empty(self):
        md = _format_search_results([], 0)
        assert md == "No results found."


# ---------------------------------------------------------------------------
# Response extraction helpers
# ---------------------------------------------------------------------------


class TestExtractHelpers:
    def test_extract_parts(self):
        parts = _extract_parts(SAMPLE_SEARCH_RESPONSE)
        assert len(parts) == 1
        assert parts[0]["ManufacturerPartNumber"] == "LM358P"

    def test_extract_parts_empty(self):
        assert _extract_parts(SAMPLE_EMPTY_RESPONSE) == []

    def test_extract_total(self):
        assert _extract_total(SAMPLE_SEARCH_RESPONSE) == 42

    def test_extract_total_empty(self):
        assert _extract_total(SAMPLE_EMPTY_RESPONSE) == 0


# ---------------------------------------------------------------------------
# API call mocking
# ---------------------------------------------------------------------------


def _mock_post_response(json_data: dict, status_code: int = 200):
    """Create a mock httpx response."""
    resp = httpx.Response(status_code, json=json_data, request=httpx.Request("POST", "https://test"))
    return resp


class TestSearchByKeyword:
    @pytest.mark.asyncio
    async def test_search_calls_api(self):
        mock_resp = _mock_post_response(SAMPLE_SEARCH_RESPONSE)
        with patch("fetchaller.mouser.api.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            # Reset rate limiter to avoid delays
            from fetchaller.mouser.api import mouser_limiter
            mouser_limiter._last_time = 0.0

            data = await search_by_keyword("LM358", "test-key")
            assert data["SearchResults"]["NumberOfResult"] == 42
            mock_client.post.assert_called_once()
            call_kwargs = mock_client.post.call_args
            assert call_kwargs.kwargs["params"] == {"apiKey": "test-key"}


class TestSearchByPartNumber:
    @pytest.mark.asyncio
    async def test_search_pipes_part_numbers(self):
        mock_resp = _mock_post_response(SAMPLE_SEARCH_RESPONSE)
        with patch("fetchaller.mouser.api.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            from fetchaller.mouser.api import mouser_limiter
            mouser_limiter._last_time = 0.0

            await search_by_part_number(["LM358P", "LM741CN"], "test-key")
            call_kwargs = mock_client.post.call_args
            payload = call_kwargs.kwargs["json"]
            assert "LM358P|LM741CN" in payload["SearchByPartRequest"]["mouserPartNumber"]


# ---------------------------------------------------------------------------
# get_product entry point
# ---------------------------------------------------------------------------


class TestGetProduct:
    @pytest.mark.asyncio
    async def test_missing_api_key_returns_error(self):
        with patch.dict("os.environ", {}, clear=True):
            result = await get_product("https://www.mouser.com/ProductDetail/TI/LM358P", api_key="")
            assert "error" in result
            assert "MOUSER_API_KEY" in result["error"]

    @pytest.mark.asyncio
    async def test_product_url_returns_content(self):
        mock_resp = _mock_post_response(SAMPLE_SEARCH_RESPONSE)
        with patch("fetchaller.mouser.api.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            from fetchaller.mouser.api import mouser_limiter
            mouser_limiter._last_time = 0.0

            result = await get_product(
                "https://www.mouser.com/ProductDetail/Texas-Instruments/LM358P",
                api_key="test-key",
            )
            assert "content" in result
            assert "LM358P" in result["content"]

    @pytest.mark.asyncio
    async def test_search_url_returns_content(self):
        mock_resp = _mock_post_response(SAMPLE_SEARCH_RESPONSE)
        with patch("fetchaller.mouser.api.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            from fetchaller.mouser.api import mouser_limiter
            mouser_limiter._last_time = 0.0

            result = await get_product(
                "https://www.mouser.com/Search/Refine?Keyword=LM358",
                api_key="test-key",
            )
            assert "content" in result
            assert "42 results found" in result["content"]

    @pytest.mark.asyncio
    async def test_unrecognized_url_returns_error(self):
        result = await get_product("https://www.mouser.com/", api_key="test-key")
        assert "error" in result
        assert "Could not extract" in result["error"]

    @pytest.mark.asyncio
    async def test_http_401_returns_auth_error(self):
        mock_resp = httpx.Response(
            401,
            request=httpx.Request("POST", "https://test"),
        )
        with patch("fetchaller.mouser.api.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_resp
            mock_client.post.side_effect = httpx.HTTPStatusError(
                "401", request=mock_resp.request, response=mock_resp,
            )
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            from fetchaller.mouser.api import mouser_limiter
            mouser_limiter._last_time = 0.0

            result = await get_product(
                "https://www.mouser.com/ProductDetail/TI/LM358P",
                api_key="bad-key",
            )
            assert "error" in result
            assert "Invalid" in result["error"]

    @pytest.mark.asyncio
    async def test_http_429_returns_rate_limit_error(self):
        mock_resp = httpx.Response(
            429,
            request=httpx.Request("POST", "https://test"),
        )
        with patch("fetchaller.mouser.api.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.side_effect = httpx.HTTPStatusError(
                "429", request=mock_resp.request, response=mock_resp,
            )
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            from fetchaller.mouser.api import mouser_limiter
            mouser_limiter._last_time = 0.0

            result = await get_product(
                "https://www.mouser.com/ProductDetail/TI/LM358P",
                api_key="test-key",
            )
            assert "error" in result
            assert "rate limit" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_timeout_returns_error(self):
        with patch("fetchaller.mouser.api.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.side_effect = httpx.TimeoutException("timeout")
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            from fetchaller.mouser.api import mouser_limiter
            mouser_limiter._last_time = 0.0

            result = await get_product(
                "https://www.mouser.com/ProductDetail/TI/LM358P",
                api_key="test-key",
            )
            assert "error" in result
            assert "timed out" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_product_not_found_falls_back_to_keyword_search(self):
        """When part number search returns empty, falls back to keyword search."""
        empty_resp = _mock_post_response(SAMPLE_EMPTY_RESPONSE)
        full_resp = _mock_post_response(SAMPLE_SEARCH_RESPONSE)

        with patch("fetchaller.mouser.api.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            # First call (part number) returns empty, second (keyword) returns results
            mock_client.post.side_effect = [empty_resp, full_resp]
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            from fetchaller.mouser.api import mouser_limiter
            mouser_limiter._last_time = 0.0

            result = await get_product(
                "https://www.mouser.com/ProductDetail/TI/LM358P",
                api_key="test-key",
            )
            assert "content" in result
            assert "42 results found" in result["content"]
