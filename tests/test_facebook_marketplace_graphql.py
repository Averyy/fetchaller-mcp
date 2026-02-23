"""Tests for Facebook Marketplace GraphQL client and URL detection."""

from fetchaller.content.facebook_marketplace import (
    extract_listing_id,
    extract_location_from_url,
    extract_price_filters,
    extract_search_query,
    is_facebook_marketplace,
    is_facebook_marketplace_listing,
    is_facebook_marketplace_search,
)
from fetchaller.facebook_marketplace.graphql import (
    build_listing_variables,
    build_search_variables,
    parse_listing_images,
    parse_listing_response,
    parse_search_response,
)
from fetchaller.facebook_marketplace.search import format_search_results

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------


class TestIsFacebookMarketplace:
    def test_marketplace_root(self):
        assert is_facebook_marketplace("https://www.facebook.com/marketplace/")

    def test_marketplace_search(self):
        assert is_facebook_marketplace("https://www.facebook.com/marketplace/search/?query=bicycle")

    def test_marketplace_city(self):
        assert is_facebook_marketplace("https://www.facebook.com/marketplace/vancouver/")

    def test_marketplace_item(self):
        assert is_facebook_marketplace("https://www.facebook.com/marketplace/item/1234567890/")

    def test_mobile(self):
        assert is_facebook_marketplace("https://m.facebook.com/marketplace/search/?query=bike")

    def test_web_subdomain(self):
        assert is_facebook_marketplace("https://web.facebook.com/marketplace/search/?query=bike")

    def test_non_marketplace(self):
        assert not is_facebook_marketplace("https://www.facebook.com/groups/123/")

    def test_non_facebook(self):
        assert not is_facebook_marketplace("https://example.com/marketplace/")


class TestIsFacebookMarketplaceSearch:
    def test_search_with_query(self):
        assert is_facebook_marketplace_search("https://www.facebook.com/marketplace/search/?query=bicycle")

    def test_city_search(self):
        assert is_facebook_marketplace_search("https://www.facebook.com/marketplace/vancouver/search/?query=bicycle")

    def test_city_browse(self):
        assert is_facebook_marketplace_search("https://www.facebook.com/marketplace/vancouver/")

    def test_root_browse(self):
        assert is_facebook_marketplace_search("https://www.facebook.com/marketplace/")

    def test_root_browse_no_trailing_slash(self):
        assert is_facebook_marketplace_search("https://www.facebook.com/marketplace")

    def test_numeric_location_search(self):
        assert is_facebook_marketplace_search("https://www.facebook.com/marketplace/108565719168398/search/?query=bike")

    def test_numeric_location_browse(self):
        assert is_facebook_marketplace_search("https://www.facebook.com/marketplace/108565719168398/")

    def test_item_not_search(self):
        assert not is_facebook_marketplace_search("https://www.facebook.com/marketplace/item/123/")

    def test_create_not_search(self):
        assert not is_facebook_marketplace_search("https://www.facebook.com/marketplace/create/")

    def test_you_not_search(self):
        assert not is_facebook_marketplace_search("https://www.facebook.com/marketplace/you/selling/")

    def test_categories_not_search(self):
        assert not is_facebook_marketplace_search("https://www.facebook.com/marketplace/categories/")

    def test_category_not_search(self):
        assert not is_facebook_marketplace_search("https://www.facebook.com/marketplace/category/vehicles")

    def test_search_with_price_filters(self):
        assert is_facebook_marketplace_search(
            "https://www.facebook.com/marketplace/search/?query=bike&minPrice=100&maxPrice=500"
        )


class TestIsFacebookMarketplaceListing:
    def test_listing(self):
        assert is_facebook_marketplace_listing("https://www.facebook.com/marketplace/item/1234567890/")

    def test_search_not_listing(self):
        assert not is_facebook_marketplace_listing("https://www.facebook.com/marketplace/search/?query=x")


class TestExtractListingId:
    def test_extracts_id(self):
        assert extract_listing_id("https://www.facebook.com/marketplace/item/1234567890/") == "1234567890"

    def test_no_id(self):
        assert extract_listing_id("https://www.facebook.com/marketplace/search/") is None


class TestExtractSearchQuery:
    def test_with_query(self):
        assert extract_search_query("https://www.facebook.com/marketplace/search/?query=bicycle") == "bicycle"

    def test_no_query(self):
        assert extract_search_query("https://www.facebook.com/marketplace/vancouver/") == ""

    def test_query_with_spaces(self):
        assert extract_search_query("https://www.facebook.com/marketplace/search/?query=road+bike") == "road bike"


class TestExtractLocationFromUrl:
    def test_city_search(self):
        assert extract_location_from_url("https://www.facebook.com/marketplace/vancouver/search/") == "vancouver"

    def test_city_browse(self):
        assert extract_location_from_url("https://www.facebook.com/marketplace/vancouver/") == "vancouver"

    def test_no_city(self):
        assert extract_location_from_url("https://www.facebook.com/marketplace/search/?query=x") == ""

    def test_item_not_city(self):
        assert extract_location_from_url("https://www.facebook.com/marketplace/item/123/") == ""

    def test_numeric_location(self):
        assert extract_location_from_url("https://www.facebook.com/marketplace/108565719168398/search/") == "108565719168398"

    def test_category_not_location(self):
        assert extract_location_from_url("https://www.facebook.com/marketplace/category/vehicles") == ""

    def test_create_not_location(self):
        assert extract_location_from_url("https://www.facebook.com/marketplace/create/") == ""

    def test_hyphenated_city(self):
        assert extract_location_from_url("https://www.facebook.com/marketplace/new-york/") == "new-york"


class TestExtractPriceFilters:
    def test_both_prices(self):
        min_p, max_p = extract_price_filters("https://www.facebook.com/marketplace/search/?query=bike&minPrice=100&maxPrice=500")
        assert min_p == 100
        assert max_p == 500

    def test_min_only(self):
        min_p, max_p = extract_price_filters("https://www.facebook.com/marketplace/search/?query=bike&minPrice=100")
        assert min_p == 100
        assert max_p is None

    def test_no_prices(self):
        min_p, max_p = extract_price_filters("https://www.facebook.com/marketplace/search/?query=bike")
        assert min_p is None
        assert max_p is None

    def test_invalid_price(self):
        min_p, max_p = extract_price_filters("https://www.facebook.com/marketplace/search/?minPrice=abc")
        assert min_p is None
        assert max_p is None


# ---------------------------------------------------------------------------
# GraphQL variable building
# ---------------------------------------------------------------------------


class TestBuildSearchVariables:
    def test_basic(self):
        result = build_search_variables("bicycle", latitude=49.28, longitude=-123.12)
        assert result["count"] == 24
        params = result["params"]
        assert params["bqf"]["query"] == "bicycle"
        browse = params["browse_request_params"]
        assert browse["filter_location_latitude"] == 49.28
        assert browse["filter_location_longitude"] == -123.12
        assert browse["filter_radius_km"] == 50
        # custom_request_params required by GraphQL schema
        assert params["custom_request_params"]["surface"] == "SEARCH"

    def test_price_filters(self):
        result = build_search_variables("bike", min_price=1000, max_price=5000)
        browse = result["params"]["browse_request_params"]
        assert browse["filter_price_lower_bound"] == 1000
        assert browse["filter_price_upper_bound"] == 5000

    def test_no_price_filters_uses_defaults(self):
        result = build_search_variables("bike")
        browse = result["params"]["browse_request_params"]
        assert browse["filter_price_lower_bound"] == 0
        assert browse["filter_price_upper_bound"] == 214748364700

    def test_min_price_zero_not_replaced(self):
        """min_price=0 should stay 0, not become the default."""
        result = build_search_variables("bike", min_price=0)
        browse = result["params"]["browse_request_params"]
        assert browse["filter_price_lower_bound"] == 0


class TestBuildListingVariables:
    def test_contains_target_id(self):
        result = build_listing_variables("1234567890")
        assert result["targetId"] == "1234567890"
        assert result["feedbackSource"] == 56
        assert result["feedLocation"] == "MARKETPLACE_MEGAMALL"
        assert result["scale"] == 2

    def test_relay_providers_present(self):
        result = build_listing_variables("123")
        relay_keys = [k for k in result if k.startswith("__relay_internal__pv__")]
        assert len(relay_keys) >= 8


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


class TestParseSearchResponse:
    def test_parses_listings(self):
        data = {
            "data": {
                "marketplace_search": {
                    "feed_units": {
                        "edges": [
                            {
                                "node": {
                                    "listing": {
                                        "id": "123",
                                        "marketplace_listing_title": "Trek Bike",
                                        "listing_price": {"formatted_amount": "CA$450"},
                                        "location": {
                                            "reverse_geocode": {"city": "Vancouver"}
                                        },
                                        "condition_text": "Used - Good",
                                        "is_pending": False,
                                        "primary_listing_photo": {
                                            "image": {"uri": "https://example.com/img.jpg"}
                                        },
                                    }
                                }
                            }
                        ]
                    }
                }
            }
        }
        listings = parse_search_response(data)
        assert len(listings) == 1
        assert listings[0]["title"] == "Trek Bike"
        assert listings[0]["price"] == "CA$450"
        assert listings[0]["location"] == "Vancouver"
        assert listings[0]["condition"] == "Used - Good"
        assert listings[0]["url"] == "https://www.facebook.com/marketplace/item/123/"

    def test_pending_listing(self):
        data = {
            "data": {
                "marketplace_search": {
                    "feed_units": {
                        "edges": [
                            {
                                "node": {
                                    "listing": {
                                        "id": "456",
                                        "marketplace_listing_title": "Pending Bike",
                                        "listing_price": {"formatted_amount": "$100"},
                                        "is_pending": True,
                                    }
                                }
                            }
                        ]
                    }
                }
            }
        }
        listings = parse_search_response(data)
        assert listings[0]["status"] == "Pending"

    def test_strikethrough_price(self):
        data = {
            "data": {
                "marketplace_search": {
                    "feed_units": {
                        "edges": [
                            {
                                "node": {
                                    "listing": {
                                        "id": "789",
                                        "marketplace_listing_title": "Discounted Item",
                                        "listing_price": {"formatted_amount": "$30"},
                                        "strikethrough_price": {"formatted_amount": "$40"},
                                    }
                                }
                            }
                        ]
                    }
                }
            }
        }
        listings = parse_search_response(data)
        assert listings[0]["price"] == "$30"
        assert listings[0]["original_price"] == "$40"

    def test_empty_response(self):
        data = {"data": {"marketplace_search": {"feed_units": {"edges": []}}}}
        assert parse_search_response(data) == []

    def test_missing_data(self):
        assert parse_search_response({}) == []

    def test_missing_listing_in_node(self):
        data = {
            "data": {
                "marketplace_search": {
                    "feed_units": {
                        "edges": [{"node": {}}]
                    }
                }
            }
        }
        assert parse_search_response(data) == []


# ---------------------------------------------------------------------------
# Listing detail parsing
# ---------------------------------------------------------------------------


class TestParseListingResponse:
    def _make_response(self, **overrides):
        """Build a minimal listing detail response for testing."""
        target = {
            "id": "1215019180833109",
            "marketplace_listing_title": "Bicycle",
            "base_marketplace_listing_title": "Bicycle",
            "listing_price": {
                "formatted_amount_zeros_stripped": "$50",
                "amount": "50.00",
                "currency": "USD",
                "amount_with_offset_in_currency": "5000",
            },
            "strikethrough_price": None,
            "redacted_description": {"text": "Good condition bike."},
            "location_text": {"text": "Vancouver, WA"},
            "is_pending": False,
            "is_sold": False,
            "is_live": True,
            "attribute_data": [
                {"attribute_name": "Condition", "value": "used_good", "label": "Used - Good"},
            ],
            "delivery_types": ["IN_PERSON"],
            "creation_time": 1771738008,
            "comparable_price": None,
            "comparable_price_type": None,
            "marketplaceListingRenderableIfLoggedOut": {
                "marketplace_listing_category_name": "Bicycles",
                "id": "1215019180833109",
            },
        }
        target.update(overrides)
        return {
            "data": {
                "viewer": {
                    "marketplace_product_details_page": {
                        "target": target,
                        "marketplace_listing_renderable_target": {
                            "seo_virtual_category": {
                                "taxonomy_path": [
                                    {"seo_info": {"seo_url": "hobbies"}, "id": "459026188375950"},
                                ],
                            },
                        },
                    }
                }
            }
        }

    def test_parses_basic_fields(self):
        listing = parse_listing_response(self._make_response())
        assert listing["id"] == "1215019180833109"
        assert listing["title"] == "Bicycle"
        assert listing["price"] == "$50"
        assert listing["description"] == "Good condition bike."
        assert listing["location"] == "Vancouver, WA"
        assert listing["condition"] == "Used - Good"
        assert listing["category"] == "Bicycles"
        assert listing["status"] == "Available"
        assert listing["url"] == "https://www.facebook.com/marketplace/item/1215019180833109/"

    def test_sold_status(self):
        listing = parse_listing_response(self._make_response(is_sold=True))
        assert listing["status"] == "Sold"

    def test_pending_status(self):
        listing = parse_listing_response(self._make_response(is_pending=True))
        assert listing["status"] == "Pending"

    def test_unavailable_status(self):
        listing = parse_listing_response(self._make_response(is_live=False))
        assert listing["status"] == "Unavailable"

    def test_strikethrough_price(self):
        listing = parse_listing_response(self._make_response(
            listing_price={
                "formatted_amount_zeros_stripped": "$30",
                "amount": "30.00",
                "currency": "USD",
            },
            strikethrough_price={"formatted_amount_zeros_stripped": "$40"},
        ))
        assert listing["price"] == "$30"
        assert listing["original_price"] == "$40"

    def test_delivery_types(self):
        listing = parse_listing_response(self._make_response(
            delivery_types=["IN_PERSON", "SHIPPING"],
        ))
        assert listing["delivery"] == ["IN_PERSON", "SHIPPING"]

    def test_empty_response(self):
        assert parse_listing_response({}) is None

    def test_missing_target(self):
        data = {"data": {"viewer": {"marketplace_product_details_page": {"target": {}}}}}
        assert parse_listing_response(data) is None


class TestParseListingImages:
    def test_parses_images(self):
        data = {
            "data": {
                "viewer": {
                    "marketplace_product_details_page": {
                        "target": {
                            "listing_photos": [
                                {
                                    "accessibility_caption": "Bike photo",
                                    "image": {
                                        "uri": "https://scontent.xx.fbcdn.net/img1.jpg",
                                        "width": 960,
                                        "height": 720,
                                    },
                                },
                                {
                                    "accessibility_caption": "No photo description available.",
                                    "image": {
                                        "uri": "https://scontent.xx.fbcdn.net/img2.jpg",
                                        "width": 720,
                                        "height": 960,
                                    },
                                },
                            ]
                        }
                    }
                }
            }
        }
        images = parse_listing_images(data)
        assert len(images) == 2
        assert images[0]["uri"] == "https://scontent.xx.fbcdn.net/img1.jpg"
        assert images[0]["alt"] == "Bike photo"
        assert images[1]["width"] == 720

    def test_empty_photos(self):
        data = {
            "data": {
                "viewer": {
                    "marketplace_product_details_page": {
                        "target": {"listing_photos": []}
                    }
                }
            }
        }
        assert parse_listing_images(data) == []

    def test_missing_data(self):
        assert parse_listing_images({}) == []


# ---------------------------------------------------------------------------
# Result formatting
# ---------------------------------------------------------------------------


class TestFormatSearchResults:
    def test_with_results(self):
        listings = [
            {
                "title": "Trek Mountain Bike",
                "price": "$450",
                "location": "North Vancouver",
                "condition": "Used - Good",
                "url": "https://www.facebook.com/marketplace/item/123/",
            },
            {
                "title": "Specialized Road Bike",
                "price": "$600",
                "location": "Burnaby",
                "status": "Pending",
                "url": "https://www.facebook.com/marketplace/item/456/",
            },
        ]
        result = format_search_results(listings, "bicycle", "Vancouver, BC", 50)
        assert '"bicycle"' in result
        assert "Vancouver, BC" in result
        assert "50km" in result
        assert "Trek Mountain Bike" in result
        assert "$450" in result
        assert "Used - Good" in result
        assert "Specialized Road Bike" in result
        assert "Pending" in result

    def test_empty_results(self):
        result = format_search_results([], "bicycle", "Vancouver, BC")
        assert "No listings found" in result

    def test_strikethrough_price(self):
        listings = [
            {
                "title": "Discounted Item",
                "price": "$30",
                "original_price": "$40",
                "url": "https://www.facebook.com/marketplace/item/789/",
            },
        ]
        result = format_search_results(listings, "deals", "Test")
        assert "$30 (was $40)" in result

    def test_no_query_header(self):
        result = format_search_results([], "", "Vancouver, BC")
        assert "Facebook Marketplace:" in result
        assert "Vancouver, BC" in result
