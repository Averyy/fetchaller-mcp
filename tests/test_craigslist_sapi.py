"""Tests for Craigslist SAPI client and search module."""

import time

from fetchaller.content.craigslist import is_craigslist_search_url
from fetchaller.craigslist.sapi import (
    _format_relative_time,
    cache_area_id,
    extract_area_id,
    get_area_name,
    get_cached_area_id,
    get_total_count,
    parse_sapi_items,
)
from fetchaller.craigslist.search import (
    extract_search_params,
    format_search_results,
)

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------


class TestIsCraigslistSearchUrl:
    def test_standard_search(self):
        assert is_craigslist_search_url("https://chicago.craigslist.org/search/sss?query=bicycle")

    def test_category_search(self):
        assert is_craigslist_search_url("https://sfbay.craigslist.org/search/cta?query=honda")

    def test_no_query(self):
        assert is_craigslist_search_url("https://vancouver.craigslist.org/search/sss")

    def test_listing_page_not_search(self):
        assert not is_craigslist_search_url("https://chicago.craigslist.org/chc/sss/d/some-item/12345.html")

    def test_non_craigslist(self):
        assert not is_craigslist_search_url("https://example.com/search/sss?query=bicycle")


# ---------------------------------------------------------------------------
# Search parameter extraction
# ---------------------------------------------------------------------------


class TestExtractSearchParams:
    def test_with_query(self):
        params = extract_search_params("https://chicago.craigslist.org/search/sss?query=bicycle")
        assert params["query"] == "bicycle"
        assert params["category"] == "sss"
        assert params["hostname"] == "chicago.craigslist.org"

    def test_without_query(self):
        params = extract_search_params("https://sfbay.craigslist.org/search/cta")
        assert params["query"] == ""
        assert params["category"] == "cta"

    def test_extra_params_forwarded(self):
        params = extract_search_params(
            "https://chicago.craigslist.org/search/sss?query=bike&min_price=100&max_price=500&sort=date"
        )
        assert params["extra_params"]["min_price"] == "100"
        assert params["extra_params"]["max_price"] == "500"
        assert params["extra_params"]["sort"] == "date"

    def test_not_search_url(self):
        assert extract_search_params("https://chicago.craigslist.org/chc/sss/d/item/123.html") is None

    def test_non_craigslist(self):
        assert extract_search_params("https://example.com/search/sss") is None


# ---------------------------------------------------------------------------
# Area ID extraction + caching
# ---------------------------------------------------------------------------


class TestExtractAreaId:
    def test_extracts_area_id(self):
        html = '...url":"chicago.craigslist.org","areaId":11,"city":"Chicago"...'
        assert extract_area_id(html) == 11

    def test_large_area_id(self):
        html = '"areaId":462,"city":"Vancouver"'
        assert extract_area_id(html) == 462

    def test_no_area_id(self):
        assert extract_area_id("<html><body>no area here</body></html>") is None


class TestAreaIdCache:
    def test_cache_miss(self):
        assert get_cached_area_id("unknown.craigslist.org") is None

    def test_cache_hit(self):
        cache_area_id("test-city.craigslist.org", 999)
        assert get_cached_area_id("test-city.craigslist.org") == 999


# ---------------------------------------------------------------------------
# SAPI response parsing
# ---------------------------------------------------------------------------


def _make_sapi_response(items, total=None, city="chicago", region="IL"):
    """Build a fake SAPI response dict."""
    return {
        "apiVersion": 8,
        "data": {
            "items": items,
            "totalResultCount": total if total is not None else len(items),
            "location": {
                "areaId": 11,
                "city": city,
                "region": region,
                "country": "US",
            },
        },
        "errors": [],
    }


def _make_sapi_item(
    title="Test Item",
    price=100,
    price_str="$100",
    posting_id=7908238463,
    seo="chicago-test-item",
    hostname="chicago",
    subarea="chc",
    category="sss",
    description="Chicago",
    images=None,
    posted_date=None,
):
    """Build a fake SAPI item dict."""
    if posted_date is None:
        posted_date = int(time.time()) - 3600  # 1 hour ago
    item = {
        "title": title,
        "price": price,
        "priceString": price_str,
        "postingId": posting_id,
        "seo": seo,
        "categoryAbbr": category,
        "postedDate": posted_date,
        "location": {
            "areaId": 11,
            "description": description,
            "hostname": hostname,
            "subareaAbbr": subarea,
            "lat": 41.94,
            "lon": -87.65,
        },
    }
    if images is not None:
        item["images"] = images
    return item


class TestParseSapiItems:
    def test_parses_items(self):
        data = _make_sapi_response([
            _make_sapi_item("Klein Quantum Road Bike", 750, "$750", 7915185949,
                            "chicago-klein-quantum-road-bike-57-cm"),
            _make_sapi_item("Schwinn Mountain Bike", 200, "$200", 7915185950,
                            "chicago-schwinn-mountain-bike"),
        ])
        items = parse_sapi_items(data)
        assert len(items) == 2
        assert items[0]["title"] == "Klein Quantum Road Bike"
        assert items[0]["price_str"] == "$750"
        assert "7915185949" in items[0]["url"]
        assert items[0]["location"] == "Chicago"
        assert items[1]["title"] == "Schwinn Mountain Bike"

    def test_builds_correct_url(self):
        data = _make_sapi_response([
            _make_sapi_item(
                "Bike", posting_id=123456, seo="chicago-bike",
                hostname="chicago", subarea="chc", category="bik",
            ),
        ])
        items = parse_sapi_items(data)
        assert items[0]["url"] == "https://chicago.craigslist.org/chc/bik/d/chicago-bike/123456.html"

    def test_skips_items_without_title(self):
        data = _make_sapi_response([
            _make_sapi_item(""),
            _make_sapi_item("Valid Item"),
        ])
        items = parse_sapi_items(data)
        assert len(items) == 1
        assert items[0]["title"] == "Valid Item"

    def test_posted_str_relative_time(self):
        now = int(time.time())
        data = _make_sapi_response([
            _make_sapi_item(posted_date=now - 7200),  # 2 hours ago
        ])
        items = parse_sapi_items(data)
        assert items[0]["posted_str"] == "2h ago"

    def test_posted_str_days(self):
        now = int(time.time())
        data = _make_sapi_response([
            _make_sapi_item(posted_date=now - 86400 * 5),  # 5 days ago
        ])
        items = parse_sapi_items(data)
        assert items[0]["posted_str"] == "5d ago"

    def test_empty_items(self):
        data = _make_sapi_response([])
        items = parse_sapi_items(data)
        assert items == []


class TestGetTotalCount:
    def test_extracts_total(self):
        data = _make_sapi_response([], total=1787)
        assert get_total_count(data) == 1787

    def test_missing_total(self):
        assert get_total_count({"data": {}}) is None


class TestGetAreaName:
    def test_extracts_city(self):
        data = _make_sapi_response([], city="new york city", region="NY")
        assert get_area_name(data) == "New York City"

    def test_single_word_city(self):
        data = _make_sapi_response([], city="chicago", region="IL")
        assert get_area_name(data) == "Chicago"

    def test_city_with_abbreviation(self):
        data = _make_sapi_response([], city="vancouver, BC", region="BC")
        assert get_area_name(data) == "Vancouver, BC"

    def test_hyphenated_city(self):
        data = _make_sapi_response([], city="seattle-tacoma", region="WA")
        assert get_area_name(data) == "Seattle-Tacoma"

    def test_missing_location(self):
        assert get_area_name({"data": {}}) == ""


class TestFormatRelativeTime:
    def test_minutes_ago(self):
        now = int(time.time())
        assert _format_relative_time(now - 300) == "5m ago"

    def test_hours_ago(self):
        now = int(time.time())
        assert _format_relative_time(now - 7200) == "2h ago"

    def test_days_ago(self):
        now = int(time.time())
        assert _format_relative_time(now - 86400 * 3) == "3d ago"

    def test_older_than_month_shows_date(self):
        # 45 days ago → should show month abbreviation
        now = int(time.time())
        result = _format_relative_time(now - 86400 * 45)
        # Should be a date string like "Jan 09"
        assert len(result) > 0
        assert "ago" not in result

    def test_future_timestamp(self):
        now = int(time.time())
        assert _format_relative_time(now + 1000) == ""


# ---------------------------------------------------------------------------
# Search result formatting
# ---------------------------------------------------------------------------


class TestFormatSearchResults:
    def test_with_results(self):
        items = [
            {"title": "Trek Mountain Bike", "price_str": "$450", "location": "North Vancouver",
             "posted_str": "2h ago",
             "url": "https://vancouver.craigslist.org/van/bik/d/trek-mountain/123.html"},
            {"title": "Giant Escape 3", "price_str": "$200", "location": "Burnaby",
             "posted_str": "5d ago", "url": ""},
        ]
        result = format_search_results(items, "bicycle", "Vancouver")
        assert '"bicycle"' in result
        assert "Vancouver" in result
        assert "2 results" in result
        assert "1. Trek Mountain Bike" in result
        assert "$450" in result
        assert "North Vancouver" in result
        assert "2h ago" in result
        assert "https://vancouver.craigslist.org/van/bik/d/trek-mountain/123.html" in result
        assert "2. Giant Escape 3" in result

    def test_showing_x_of_y(self):
        """When total > items returned, show 'showing X of Y'."""
        items = [{"title": f"Item {i}", "price_str": "$100", "location": "Chicago",
                  "posted_str": "", "url": ""} for i in range(120)]
        result = format_search_results(items, "bicycle", "Chicago", total=1787)
        assert "showing 120 of 1,787" in result

    def test_exact_count_when_all_returned(self):
        """When total == items count, show 'N results'."""
        items = [{"title": "Item 1", "price_str": "$100", "location": "Chicago",
                  "posted_str": "", "url": ""}]
        result = format_search_results(items, "bicycle", "Chicago", total=1)
        assert "1 results" in result

    def test_url_included_in_output(self):
        items = [{"title": "Test Item", "price_str": "$100", "location": "Chicago",
                  "posted_str": "", "url": "https://chicago.craigslist.org/chc/sss/d/item/456.html"}]
        result = format_search_results(items, "test", "Chicago")
        assert "https://chicago.craigslist.org/chc/sss/d/item/456.html" in result

    def test_empty_results(self):
        result = format_search_results([], "bicycle", "Chicago")
        assert "No listings found" in result

    def test_no_query(self):
        result = format_search_results([], "", "Chicago")
        assert "Chicago" in result

    def test_no_location_info(self):
        items = [{"title": "Bare Item", "price_str": "$50", "location": "",
                  "posted_str": "", "url": ""}]
        result = format_search_results(items, "test")
        assert "1. Bare Item" in result
        assert "$50" in result
