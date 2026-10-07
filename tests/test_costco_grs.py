"""Costco's catalogue search (gdx-api.costco.com, Google Retail Search).

The site moved every keyword search and category page to this service; the
Fusion endpoint this package was built on answered a redirected keyword ("tv")
with zero documents, and category pages were sent to it as keywords. Both
rendered "No products found". These tests pin the replacement's behaviour,
including the fallbacks that keep a failure from reading as an empty shelf.
"""

import json

import pytest

from fetchaller.costco import grs
from fetchaller.costco import search as costco_search
from fetchaller.costco.grs import (
    Location,
    nearest_warehouse,
    parse_items,
    parse_site_config,
    refine_filters,
)
from fetchaller.costco.search import _page_params, format_grs_results


def _rsc(obj_text: str) -> str:
    """Wrap config JSON the way a Next.js page streams it (escaped quotes)."""
    return 'self.__next_f.push([1,"' + obj_text.replace('"', '\\"') + '"])'


PAGE = _rsc(
    '{"search":{"endpoint":"https://gdx-api.costco.com/catalog/search/api/v1/search","method":"POST",'
    '"required_request_headers":{"client-identifier":"search-client","client_id":{"USBC":"USBC","CABC":"CABC"},'
    '"locale":{"en-us":"en-US","en-ca":"en-CA"},"searchResultProvider":"GRS"},'
    '"required_request_parameters":{"visitorId":"","pageSize":20,"personalizationEnabled":true}},'
    '"warehouseLocatorSalesLocationApi":{"endpoint":"https://ecom-api.costco.com/core/warehouse-locator/v1/salesLocations.json",'
    '"required_request_headers":{"client-identifier":"locator-client"}},'
    '"locationCatalogAPIService":{"endpoint":" https://ecom-api.costco.com/ebusiness/inventory/v1/location/distributioncenters"},'
    '"defaultLocation":{"latitude":43.681,"longitude":-79.399},'
    '"defaultLocation":"M4V 2H7","defaultState":"ON"}'
)


class TestSiteConfig:
    def test_reads_every_published_value_for_the_site(self):
        config = parse_site_config(PAGE, "ca")
        assert config is not None and config.from_page
        assert config.search_endpoint == "https://gdx-api.costco.com/catalog/search/api/v1/search"
        assert config.search_headers == {
            "client-identifier": "search-client",
            "client_id": "CABC",
            "locale": "en-CA",
            "searchResultProvider": "GRS",
        }
        assert config.search_template["pageSize"] == 20
        assert config.locator_headers == {"client-identifier": "locator-client"}
        assert config.dc_endpoint.startswith("https://ecom-api.costco.com/")
        assert (config.latitude, config.longitude, config.postal, config.state) == (43.681, -79.399, "M4V 2H7", "ON")

    def test_us_site_picks_its_own_codes(self):
        config = parse_site_config(PAGE, "com")
        assert config.search_headers["client_id"] == "USBC"
        assert config.search_headers["locale"] == "en-US"

    def test_a_page_without_the_search_service_is_not_a_config(self):
        assert parse_site_config("<html>no config here</html>", "ca") is None


def test_nearest_warehouse_skips_business_centres():
    payload = {
        "salesLocations": [
            {"salesLocationId": "595", "distance": 1.0, "subType": {"code": "Business Center"}},
            {"salesLocationId": "535", "distance": 4.4, "subType": {"code": "Warehouse"}},
            {"salesLocationId": "1316", "distance": 3.1, "subType": {"code": "Warehouse"}},
        ]
    }
    assert nearest_warehouse(payload)["salesLocationId"] == "1316"
    assert nearest_warehouse({"salesLocations": []}) is None


def test_refine_maps_brand_and_reports_the_rest():
    applied, skipped = refine_filters("||Brand_attr-Samsung||Screen_Size_attr-65")
    assert applied == ['attributes.brand: ANY("Samsung")']
    assert skipped == ["Screen_Size_attr-65"]
    assert refine_filters(None) == ([], [])


def test_sort_plus_survives_query_parsing():
    _, sort_by, _ = _page_params("https://www.costco.ca/televisions.html?sortBy=item_location_pricing_salePrice+asc")
    assert sort_by == "item_location_pricing_salePrice+asc"
    assert grs.SORT_MAP[sort_by] == "price"


def _result(product_id, title, *, attrs=None, rating=None, item="8884043"):
    return {
        "id": product_id,
        "product": {
            "title": title,
            "uri": f"https://www.costco.ca/p/-/x/{product_id}",
            "brands": ["Hisense"],
            "attributes": attrs or {"model": {"text": ["43QD4SR"]}},
            "rating": rating or {"averageRating": 4.5625, "ratingCount": 64},
        },
        "variantRollupValues": {"variantId": [item]},
    }


DATA = {
    "searchResult": {
        "results": [
            _result("4201032445", 'Hisense 43" TV', attrs={
                "model": {"text": ["43QD4SR"]},
                "promotional_statement": {"text": ["Price valid until 10/08/26"]},
            }),
            _result("4201006314", "OLED TV", item="1"),
            _result("4201019071", "Cart-price TV", attrs={"disp_price_in_cart_only": {"numbers": [1.0]}}, item="2"),
        ],
        "totalSize": 137,
        "facets": [
            {"key": "brands", "values": [{"value": "Samsung", "count": 52}, {"value": "LG", "count": 30}]},
            {"key": "attributes.category_uri", "values": [{"value": "televisions", "count": 137}]},
        ],
    },
    "inventoryResponse": [
        {
            "productId": "4201032445",
            "deliveryPrice": {"minPrice": "297.99", "maxPrice": "297.99"},
            "warehousePrice": {"minPrice": "297.99", "maxPrice": "297.99"},
            "originalPrice": {"minPrice": "297.99", "maxPrice": "297.99"},
            "deliveryAvailability": "IN_STOCK",
            "warehouseAvailability": "LOW_STOCK",
            "promotions": [{"short_text": "Spend and Save"}],
        },
        {
            "productId": "4201006314",
            "deliveryPrice": {"minPrice": "1494.99", "maxPrice": "1494.99"},
            "originalPrice": {"minPrice": "1994.99", "maxPrice": "1994.99"},
            "promotions": [{"short_text": "$500 OFF"}],
        },
    ],
    "breadcrumb": {"name": "Electronics|TVs", "url": "electronics|televisions"},
}

LOCATION = Location("1316-wh", "Thorncliffe Park", "Toronto", "M4V 2H7", "ON", ["1316-wh"])


def test_items_join_their_inventory_rows():
    items = parse_items(DATA)
    first = items[0]
    assert first["item_number"] == "8884043" and first["model"] == "43QD4SR"
    assert first["delivery_price"]["minPrice"] == "297.99"
    assert first["promotions"] == ["Spend and Save"]
    assert first["statements"] == ["Price valid until 10/08/26"]
    assert items[2]["price_in_cart"] is True


class TestRender:
    def _render(self, **kwargs):
        defaults = dict(
            domain="ca",
            url="https://www.costco.ca/televisions.html",
            query="",
            category="televisions",
            page=1,
            location=LOCATION,
            sort_label="relevance",
        )
        defaults.update(kwargs)
        return format_grs_results(DATA, **defaults)

    def test_header_location_and_rows(self):
        out = self._render()
        assert out.startswith("# Costco.ca: Electronics > TVs")
        assert "**137 products** · showing 1–3" in out
        assert "warehouse #1316 Thorncliffe Park, Toronto and delivery to M4V 2H7 ON" in out
        assert "1. **Hisense 43\" TV** — $297.99 · ★4.6 (64) · Item 8884043 · Model 43QD4SR · Spend and Save" in out
        assert "delivery: in stock · warehouse: low stock" in out

    def test_sale_shows_the_was_price_and_cart_prices_are_not_invented(self):
        out = self._render()
        assert "$1,494.99 (was $1,994.99)" in out
        assert "$500 OFF" in out
        assert "**Cart-price TV** — price shown in cart" in out

    def test_next_page_and_facets(self):
        out = self._render()
        assert "Next page: https://www.costco.ca/televisions.html?currentPage=2" in out
        assert "- **brands:** Samsung (52); LG (30)" in out
        assert "category_uri" not in out

    def test_redirect_is_disclosed(self):
        out = self._render(query="tv", redirected_to="https://www.costco.ca/televisions.html")
        assert 'Costco sends the search "tv" to https://www.costco.ca/televisions.html' in out

    def test_unknown_category_says_so(self):
        empty = {"searchResult": {"results": [], "totalSize": 0}}
        out = format_grs_results(
            empty, domain="ca", url="https://www.costco.ca/nope.html", query="", category="nope",
            page=1, location=LOCATION, sort_label="relevance",
        )
        assert "returned no products for the category 'nope' — check the URL" in out


async def _resolve(value):
    return value


class TestSearchCostcoFlow:
    @pytest.fixture(autouse=True)
    def _stub_location(self, monkeypatch):
        config = parse_site_config(PAGE, "ca")
        monkeypatch.setattr(grs, "site_config", lambda *a, **k: _resolve(config))
        monkeypatch.setattr(grs, "default_location", lambda *a, **k: _resolve(LOCATION))

    async def test_a_redirected_keyword_reads_the_category_with_the_callers_page(self, monkeypatch):
        calls = []

        async def fake_search(config, location, **kwargs):
            calls.append((kwargs["query"], kwargs["category"], kwargs["page"]))
            if kwargs["query"]:
                return {"searchResult": {"results": [], "totalSize": 0, "redirectUri": "/televisions.html"}}
            return DATA

        monkeypatch.setattr(grs, "search", fake_search)
        result = await costco_search.search_costco("https://www.costco.ca/s?keyword=tv&currentPage=2")
        assert calls == [("tv", None, 2), ("", "televisions", 2)]
        assert 'Costco sends the search "tv" to https://www.costco.ca/televisions.html' in result["content"]

    async def test_keyword_search_falls_back_to_fusion_and_says_so(self, monkeypatch):
        async def failing(*args, **kwargs):
            raise grs.GrsError("HTTP 503")

        async def fusion(**kwargs):
            return {"response": {"numFound": 1, "docs": [{"item_product_name": "Laptop", "item_number": "1"}]}}

        monkeypatch.setattr(grs, "search", failing)
        monkeypatch.setattr(costco_search.api, "search", fusion)
        result = await costco_search.search_costco("https://www.costco.ca/s?keyword=laptop")
        assert "older search service" in result["content"]
        assert "Laptop" in result["content"]

    async def test_a_category_has_no_silent_fallback(self, monkeypatch):
        async def failing(*args, **kwargs):
            raise grs.GrsError("HTTP 503")

        monkeypatch.setattr(grs, "search", failing)
        result = await costco_search.search_costco("https://www.costco.ca/televisions.html")
        assert "did not answer for this category" in result["error"]


def test_fixture_page_is_valid_rsc_text():
    # Guard the helper itself: the escaped config must round-trip.
    assert json.loads(PAGE.split('[1,"', 1)[1].rsplit('"])', 1)[0].replace('\\"', '"'))["search"]


def test_offset_links_page_by_row_index():
    page, _, _ = _page_params("https://www.costco.ca/televisions.html?offset=48")
    assert page == 48 // grs.PAGE_SIZE + 1
    page, _, _ = _page_params("https://www.costco.ca/televisions.html?currentPage=3&offset=0")
    assert page == 3


async def test_a_category_slug_cannot_break_out_of_its_filter(monkeypatch):
    config = parse_site_config(PAGE, "ca")
    seen = {}

    async def fake_post(url, *, json=None, headers=None, timeout=None):
        seen["body"] = json

        class R:
            status_code = 200

            @staticmethod
            def json():
                return {"searchResult": {"results": [], "totalSize": 0}}

        return R()

    class Session:
        post = staticmethod(fake_post)

    monkeypatch.setattr(grs, "_get_session", lambda: _resolve(Session()))
    await grs.search(config, LOCATION, query="", category='tv") OR ("x', timeout=5)
    assert seen["body"]["filterBy"][0] == 'attributes.category_uri: ANY("tv\\") OR (\\"x")'


async def test_a_failed_config_read_is_not_cached(monkeypatch):
    calls = []

    class Session:
        async def get(self, url, timeout=None):
            calls.append(url)
            raise OSError("down")

    monkeypatch.setattr(grs, "_get_session", lambda: _resolve(Session()))
    monkeypatch.setattr(grs, "_configs", {})

    class NoWait:
        async def wait(self, *a, **k):
            return None

    monkeypatch.setattr(grs, "costco_limiter", NoWait())
    first = await grs.site_config("ca", "https://www.costco.ca/", timeout=5)
    second = await grs.site_config("ca", "https://www.costco.ca/", timeout=5)
    assert not first.from_page and not second.from_page
    assert len(calls) == 2  # the page is tried again, not pinned to the dated fallback
