"""Tests for Kijiji GraphQL API client (kijiji/api.py).

Tests URL detection, price formatting, listing/search formatting, and error handling.
GraphQL calls are mocked with wafer session.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from wafer import WaferError

from fetchaller.kijiji.api import (
    _format_listing_detail,
    _format_listing_summary,
    _format_price,
    _format_search_results,
    extract_listing_id,
    get_listing,
    is_kijiji_listing,
    is_kijiji_search,
)

# ---------------------------------------------------------------------------
# Sample data (amounts in cents, matching real API responses)
# ---------------------------------------------------------------------------

SAMPLE_LISTING = {
    "id": "1701831111",
    "title": "2017 Volkswagen Golf R",
    "description": "Well maintained, 6-speed manual. All records available.",
    "activationDate": "2025-01-15T14:30:00Z",
    "imageUrls": ["https://i.ebayimg.com/img1.jpg", "https://i.ebayimg.com/img2.jpg"],
    "url": "/v-cars-trucks/st-catharines/2017-volkswagen-golf-r/1701831111",
    "categoryId": "174",
    "price": {
        "type": "FIXED",
        "amount": 3250000,  # $32,500.00 in cents
        "currency": "CAD",
        "originalAmount": None,
    },
    "location": {
        "name": "St. Catharines",
        "address": "123 Main St, St. Catharines, ON",
    },
    "posterInfo": {
        "posterId": "12345",
        "sellerType": "OWNER",
        "verified": True,
    },
    "attributes": {
        "all": [
            {"canonicalName": "carmileageinkms", "canonicalValues": ["85000"], "name": "Kilometres", "values": ["85,000"]},
            {"canonicalName": "drivetrain", "canonicalValues": ["awd"], "name": "Drivetrain", "values": ["All-wheel drive (AWD)"]},
            {"canonicalName": "condition", "canonicalValues": ["Used"], "name": "Condition", "values": ["Used"]},
        ]
    },
    "metrics": {"views": 1523},
}

# Search results use canonicalName/canonicalValues only (no name/values)
SAMPLE_SEARCH_LISTING = {
    "id": "1701831111",
    "title": "2017 Volkswagen Golf R",
    "imageCount": 5,
    "url": "/v-cars-trucks/st-catharines/2017-volkswagen-golf-r/1701831111",
    "activationDate": "2025-01-15T14:30:00Z",
    "sortingDate": "2025-01-15T14:30:00Z",
    "location": {"name": "St. Catharines", "distance": "15 km"},
    "price": {"type": "FIXED", "amount": 3250000, "originalAmount": None},
    "posterInfo": {"posterId": "12345", "verified": True},
    "attributes": {
        "all": [
            {"canonicalName": "carmileageinkms", "canonicalValues": ["85000"]},
            {"canonicalName": "drivetrain", "canonicalValues": ["awd"]},
            {"canonicalName": "caryear", "canonicalValues": ["2017"]},
        ]
    },
}

SAMPLE_SEARCH_RESPONSE = {
    "data": {
        "searchResultsPageByUrl": {
            "pagination": {"totalCount": 147},
            "results": {
                "mainListings": [SAMPLE_SEARCH_LISTING],
            },
        }
    }
}

SAMPLE_LISTING_RESPONSE = {
    "data": {
        "listing": SAMPLE_LISTING,
    }
}


# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------


class TestURLDetection:
    def test_is_kijiji_search_category_page(self):
        assert is_kijiji_search("https://www.kijiji.ca/b-cars-trucks/st-catharines/2017-golf-r/k0c174l1700357")

    def test_is_kijiji_search_bare_domain(self):
        assert is_kijiji_search("https://kijiji.ca/b-buy-sell/ottawa/c10l1700185")

    def test_is_kijiji_search_not_listing(self):
        assert not is_kijiji_search("https://www.kijiji.ca/v-cars-trucks/city/slug/1234567890")

    def test_is_kijiji_search_not_other_site(self):
        assert not is_kijiji_search("https://example.com/b-cars/page")

    def test_is_kijiji_listing_v_path(self):
        assert is_kijiji_listing("https://www.kijiji.ca/v-cars-trucks/st-catharines/golf-r/1701831111")

    def test_is_kijiji_listing_bare_domain(self):
        assert is_kijiji_listing("https://kijiji.ca/v-cell-phone/ottawa/iphone/1701831111")

    def test_is_kijiji_listing_not_search(self):
        assert not is_kijiji_listing("https://www.kijiji.ca/b-cars-trucks/page/k0c174")

    def test_is_kijiji_listing_not_other_site(self):
        assert not is_kijiji_listing("https://example.com/v-cars/page/123")

    def test_extract_listing_id(self):
        url = "https://www.kijiji.ca/v-cars-trucks/st-catharines/golf-r/1701831111"
        assert extract_listing_id(url) == "1701831111"

    def test_extract_listing_id_with_query(self):
        url = "https://www.kijiji.ca/v-cars-trucks/city/slug/1234567890?utm_source=foo"
        assert extract_listing_id(url) == "1234567890"

    def test_extract_listing_id_returns_none_for_search(self):
        url = "https://www.kijiji.ca/b-cars-trucks/city/k0c174"
        assert extract_listing_id(url) is None

    def test_extract_listing_id_returns_none_for_short_id(self):
        url = "https://www.kijiji.ca/v-cars/city/slug/1234"
        assert extract_listing_id(url) is None

    def test_extract_listing_id_trailing_slash(self):
        url = "https://www.kijiji.ca/v-cars-trucks/city/slug/1234567890/"
        assert extract_listing_id(url) == "1234567890"

    def test_extract_listing_id_hash_fragment(self):
        url = "https://www.kijiji.ca/v-cars-trucks/city/slug/1234567890#details"
        assert extract_listing_id(url) == "1234567890"


# ---------------------------------------------------------------------------
# Price formatting (amounts in cents)
# ---------------------------------------------------------------------------


class TestFormatPrice:
    def test_fixed_amount_in_cents(self):
        assert _format_price({"type": "FIXED", "amount": 3250000}) == "$32,500.00"

    def test_specified_amount_alias(self):
        """SPECIFIED_AMOUNT is an alternative type string for the same behavior."""
        assert _format_price({"type": "SPECIFIED_AMOUNT", "amount": 1000}) == "$10.00"

    def test_currency_non_cad(self):
        assert _format_price({"type": "FIXED", "amount": 10000, "currency": "USD"}) == "$100.00 USD"

    def test_currency_cad_no_suffix(self):
        assert _format_price({"type": "FIXED", "amount": 5000, "currency": "CAD"}) == "$50.00"

    def test_price_drop_in_cents(self):
        result = _format_price({"type": "FIXED", "amount": 2500000, "originalAmount": 3000000})
        assert "$25,000.00" in result
        assert "(was $30,000.00)" in result

    def test_free(self):
        assert _format_price({"type": "FREE"}) == "Free"

    def test_give_away(self):
        """GIVE_AWAY is the actual API type for free items."""
        assert _format_price({"type": "GIVE_AWAY"}) == "Free"

    def test_please_contact(self):
        assert _format_price({"type": "PLEASE_CONTACT"}) == "Please Contact"

    def test_contact(self):
        """CONTACT is the actual API type for 'please contact' items."""
        assert _format_price({"type": "CONTACT"}) == "Please Contact"

    def test_swap_trade(self):
        assert _format_price({"type": "SWAP_TRADE"}) == "Swap / Trade"

    def test_none_price(self):
        assert _format_price(None) == ""

    def test_empty_dict(self):
        assert _format_price({}) == ""

    def test_zero_amount(self):
        assert _format_price({"type": "FIXED", "amount": 0}) == "$0.00"

    def test_small_amount(self):
        assert _format_price({"type": "FIXED", "amount": 500}) == "$5.00"


# ---------------------------------------------------------------------------
# Listing detail formatting
# ---------------------------------------------------------------------------


class TestFormatListingDetail:
    def test_title_heading(self):
        md = _format_listing_detail(SAMPLE_LISTING)
        assert md.startswith("# 2017 Volkswagen Golf R")

    def test_price_converted_from_cents(self):
        md = _format_listing_detail(SAMPLE_LISTING)
        assert "**Price:** $32,500.00" in md

    def test_location_uses_address(self):
        md = _format_listing_detail(SAMPLE_LISTING)
        assert "**Location:** 123 Main St, St. Catharines, ON" in md

    def test_location_falls_back_to_name(self):
        listing = {**SAMPLE_LISTING, "location": {"name": "Ottawa", "address": ""}}
        md = _format_listing_detail(listing)
        assert "**Location:** Ottawa" in md

    def test_posted_date(self):
        md = _format_listing_detail(SAMPLE_LISTING)
        assert "**Posted:** Jan 15, 2025" in md

    def test_views(self):
        md = _format_listing_detail(SAMPLE_LISTING)
        assert "**Views:** 1,523" in md

    def test_seller_info(self):
        md = _format_listing_detail(SAMPLE_LISTING)
        assert "**Seller:** Owner | Verified" in md

    def test_description_section(self):
        md = _format_listing_detail(SAMPLE_LISTING)
        assert "## Description" in md
        assert "Well maintained, 6-speed manual" in md

    def test_attributes_uses_name_field(self):
        """Listing detail attributes use human-readable `name` when available."""
        md = _format_listing_detail(SAMPLE_LISTING)
        assert "## Details" in md
        assert "**Kilometres:** 85,000" in md
        assert "**Drivetrain:** All-wheel drive (AWD)" in md

    def test_attributes_falls_back_to_canonical(self):
        """Falls back to canonicalName when name is missing."""
        listing = {
            **SAMPLE_LISTING,
            "attributes": {"all": [
                {"canonicalName": "carmileageinkms", "canonicalValues": ["90000"]},
            ]},
        }
        md = _format_listing_detail(listing)
        assert "**carmileageinkms:** 90000" in md

    def test_image_count(self):
        md = _format_listing_detail(SAMPLE_LISTING)
        assert "**Images:** 2" in md

    def test_url(self):
        md = _format_listing_detail(SAMPLE_LISTING)
        assert "https://www.kijiji.ca/v-cars-trucks/st-catharines/2017-volkswagen-golf-r/1701831111" in md

    def test_minimal_listing(self):
        md = _format_listing_detail({"title": "Item", "id": "1"})
        assert "# Item" in md

    def test_free_listing_no_price_line(self):
        listing = {**SAMPLE_LISTING, "price": None}
        md = _format_listing_detail(listing)
        assert "**Price:**" not in md

    def test_free_listing_explicit_type(self):
        listing = {**SAMPLE_LISTING, "price": {"type": "FREE"}}
        md = _format_listing_detail(listing)
        assert "**Price:** Free" in md


# ---------------------------------------------------------------------------
# Search results formatting
# ---------------------------------------------------------------------------


class TestFormatSearchResults:
    def test_total_count(self):
        md = _format_search_results(SAMPLE_SEARCH_RESPONSE["data"])
        assert "**147 results**" in md
        assert "(showing 1)" in md

    def test_listing_summary_title(self):
        md = _format_search_results(SAMPLE_SEARCH_RESPONSE["data"])
        assert "**2017 Volkswagen Golf R**" in md

    def test_listing_summary_price_in_dollars(self):
        md = _format_search_results(SAMPLE_SEARCH_RESPONSE["data"])
        assert "$32,500.00" in md

    def test_listing_summary_location_with_distance(self):
        md = _format_search_results(SAMPLE_SEARCH_RESPONSE["data"])
        assert "St. Catharines (15 km)" in md

    def test_listing_summary_km_formatted(self):
        md = _format_search_results(SAMPLE_SEARCH_RESPONSE["data"])
        assert "85,000 km" in md

    def test_listing_summary_drivetrain(self):
        md = _format_search_results(SAMPLE_SEARCH_RESPONSE["data"])
        assert "AWD" in md

    def test_listing_summary_year(self):
        md = _format_search_results(SAMPLE_SEARCH_RESPONSE["data"])
        assert "2017" in md

    def test_empty_results(self):
        data = {"searchResultsPageByUrl": {"pagination": {"totalCount": 0}, "results": {"mainListings": []}}}
        assert _format_search_results(data) == "No results found."

    def test_missing_data(self):
        assert _format_search_results({}) == "No results found."


class TestFormatListingSummary:
    def test_numbered_format(self):
        md = _format_listing_summary(1, SAMPLE_SEARCH_LISTING)
        assert md.startswith("1. **2017 Volkswagen Golf R**")

    def test_includes_url(self):
        md = _format_listing_summary(1, SAMPLE_SEARCH_LISTING)
        assert "https://www.kijiji.ca/v-cars-trucks/" in md

    def test_free_listing(self):
        listing = {**SAMPLE_SEARCH_LISTING, "price": {"type": "FREE"}}
        md = _format_listing_summary(1, listing)
        assert "Free" in md

    def test_null_price(self):
        """NonAmountPrice types result in null price from AmountPrice fragment."""
        listing = {**SAMPLE_SEARCH_LISTING, "price": None}
        md = _format_listing_summary(1, listing)
        assert "**2017 Volkswagen Golf R**" in md

    def test_rental_attributes(self):
        """Rental listings show bedrooms, bathrooms, unit type, and sqft."""
        listing = {
            "id": "999",
            "title": "2 Bedroom Apartment",
            "url": "/v-apartments/city/slug/999999999",
            "price": {"type": "FIXED", "amount": 200000},
            "location": {"name": "Ottawa"},
            "attributes": {"all": [
                {"canonicalName": "numberbedrooms", "canonicalValues": ["2"]},
                {"canonicalName": "numberbathrooms", "canonicalValues": ["10"]},
                {"canonicalName": "unittype", "canonicalValues": ["apartment"]},
                {"canonicalName": "areainfeet", "canonicalValues": ["800"]},
            ]},
        }
        md = _format_listing_summary(1, listing)
        assert "2 bd" in md
        assert "1 ba" in md
        assert "Apartment" in md
        assert "800 sqft" in md

    def test_rental_bachelor(self):
        """Zero bedrooms renders as 'Bachelor'."""
        listing = {
            "id": "888",
            "title": "Bachelor Apt",
            "url": "/v-apartments/city/slug/888888888",
            "price": {"type": "FIXED", "amount": 100000},
            "location": {"name": "Toronto"},
            "attributes": {"all": [
                {"canonicalName": "numberbedrooms", "canonicalValues": ["0"]},
                {"canonicalName": "numberbathrooms", "canonicalValues": ["10"]},
            ]},
        }
        md = _format_listing_summary(1, listing)
        assert "Bachelor" in md
        assert "1 ba" in md

    def test_rental_half_bathroom(self):
        """Bathroom tenths encoding: 15 = 1.5."""
        listing = {
            "id": "777",
            "title": "Townhouse",
            "url": "/v-house/city/slug/777777777",
            "price": {"type": "FIXED", "amount": 250000},
            "location": {"name": "Ottawa"},
            "attributes": {"all": [
                {"canonicalName": "numberbathrooms", "canonicalValues": ["15"]},
            ]},
        }
        md = _format_listing_summary(1, listing)
        assert "1.5 ba" in md

    def test_condition_attribute_display_name(self):
        """Condition canonical values are mapped to display names."""
        listing = {
            "id": "666",
            "title": "iPhone 14",
            "url": "/v-phones/city/slug/666666666",
            "price": {"type": "FIXED", "amount": 52500},
            "location": {"name": "Toronto"},
            "attributes": {"all": [
                {"canonicalName": "condition", "canonicalValues": ["usedlikenew"]},
            ]},
        }
        md = _format_listing_summary(1, listing)
        assert "Like New" in md

    def test_condition_attribute_new(self):
        listing = {
            "id": "555",
            "title": "Widget",
            "url": "/v-other/city/slug/555555555",
            "price": {"type": "FIXED", "amount": 1000},
            "location": {"name": "Ottawa"},
            "attributes": {"all": [
                {"canonicalName": "condition", "canonicalValues": ["new"]},
            ]},
        }
        md = _format_listing_summary(1, listing)
        assert "New" in md


# ---------------------------------------------------------------------------
# get_listing entry point
# ---------------------------------------------------------------------------


class TestGetListing:
    @pytest.mark.asyncio
    async def test_listing_url_returns_content(self):
        with _mock_session(SAMPLE_LISTING_RESPONSE):
            _reset_limiter()
            result = await get_listing("https://www.kijiji.ca/v-cars-trucks/st-catharines/golf-r/1701831111")
            assert "content" in result
            assert "2017 Volkswagen Golf R" in result["content"]
            assert "$32,500.00" in result["content"]

    @pytest.mark.asyncio
    async def test_search_url_returns_content(self):
        with _mock_session(SAMPLE_SEARCH_RESPONSE):
            _reset_limiter()
            result = await get_listing("https://www.kijiji.ca/b-cars-trucks/st-catharines/2017-golf-r/k0c174l1700357")
            assert "content" in result
            assert "147 results" in result["content"]

    @pytest.mark.asyncio
    async def test_search_passes_pagination_in_both_places(self):
        """Pagination must be nested in searchResultsByUrlInput AND as a separate variable."""
        with _mock_session(SAMPLE_SEARCH_RESPONSE) as mock_get_session:
            _reset_limiter()
            await get_listing("https://www.kijiji.ca/b-cars-trucks/ontario/c174l9004")
            # mock_get_session.return_value is the session mock
            session_mock = mock_get_session.return_value
            call_kwargs = session_mock.post.call_args
            payload = call_kwargs.kwargs["json"]
            variables = payload["variables"]
            # Pagination in both places
            assert "pagination" in variables["searchResultsByUrlInput"]
            assert "pagination" in variables
            assert variables["searchResultsByUrlInput"]["pagination"]["limit"] == 40
            assert variables["pagination"]["limit"] == 40

    @pytest.mark.asyncio
    async def test_unrecognized_url_returns_error(self):
        result = await get_listing("https://www.kijiji.ca/")
        assert "error" in result
        assert "Could not extract" in result["error"]

    @pytest.mark.asyncio
    async def test_listing_not_found(self):
        with _mock_session({"data": {"listing": None}}):
            _reset_limiter()
            result = await get_listing("https://www.kijiji.ca/v-cars-trucks/city/slug/9999999999")
            assert "error" in result
            assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_graphql_error_with_no_data(self):
        with _mock_session({"errors": [{"message": "Something went wrong"}], "data": {"listing": None}}):
            _reset_limiter()
            result = await get_listing("https://www.kijiji.ca/v-cars-trucks/city/slug/1234567890")
            assert "error" in result
            assert "Something went wrong" in result["error"]

    @pytest.mark.asyncio
    async def test_partial_errors_with_data_succeeds(self):
        """GraphQL can return errors alongside valid data — should succeed."""
        response = {
            "errors": [{"message": "Attribute not found", "path": ["listing", "attributes", "all", 5, "name"]}],
            "data": {"listing": SAMPLE_LISTING},
        }
        with _mock_session(response):
            _reset_limiter()
            result = await get_listing("https://www.kijiji.ca/v-cars-trucks/city/slug/1701831111")
            assert "content" in result
            assert "2017 Volkswagen Golf R" in result["content"]

    @pytest.mark.asyncio
    async def test_http_error_returns_error(self):
        with _mock_session_error(WaferError("Server returned HTTP 500")):
            _reset_limiter()
            result = await get_listing("https://www.kijiji.ca/v-cars-trucks/city/slug/1234567890")
            assert "error" in result
            assert "500" in result["error"]

    @pytest.mark.asyncio
    async def test_timeout_returns_error(self):
        with _mock_session_error(WaferError("Connection timeout")):
            _reset_limiter()
            result = await get_listing("https://www.kijiji.ca/v-cars-trucks/city/slug/1234567890")
            assert "error" in result
            assert "timed out" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_listing_missing_id_returns_error(self):
        result = await get_listing("https://www.kijiji.ca/v-cars-trucks/city/slug/abc")
        assert "error" in result
        assert "Could not extract listing ID" in result["error"]

    @pytest.mark.asyncio
    async def test_search_empty_results_no_error(self):
        """Empty search results should return 'No results found.' not an error."""
        response = {
            "data": {
                "searchResultsPageByUrl": {
                    "pagination": {"totalCount": 0},
                    "results": {"mainListings": []},
                }
            }
        }
        with _mock_session(response):
            _reset_limiter()
            result = await get_listing("https://www.kijiji.ca/b-cars-trucks/ontario/c174l9004")
            assert "content" in result
            assert "No results found" in result["content"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reset_limiter() -> None:
    from fetchaller.kijiji.api import kijiji_limiter
    kijiji_limiter._last_time = 0.0


def _mock_session(response_json: dict):
    """Patch _get_session to return a mock session whose post() returns response_json."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = response_json
    mock_resp.raise_for_status.return_value = None

    mock_sess = AsyncMock()
    mock_sess.post.return_value = mock_resp
    return patch("fetchaller.kijiji.api._get_session", return_value=mock_sess)


def _mock_session_error(error: Exception):
    """Patch _get_session to return a mock session whose post() raises an error."""
    mock_sess = AsyncMock()
    mock_sess.post.side_effect = error
    return patch("fetchaller.kijiji.api._get_session", return_value=mock_sess)
