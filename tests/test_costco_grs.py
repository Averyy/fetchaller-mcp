"""Costco's catalogue search (gdx-api.costco.com, Google Retail Search).

The site moved every keyword search and category page to this service; the
Fusion endpoint this package was built on answered a redirected keyword ("tv")
with zero documents, and category pages were sent to it as keywords. Both
rendered "No products found". These tests pin the replacement's behaviour,
including the fallbacks that keep a failure from reading as an empty shelf.
"""

import asyncio
import json
import time

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

    async def test_every_call_shares_the_callers_timeout(self, monkeypatch):
        # Each call used to get the whole timeout, so together they overran
        # fetch's own deadline, which cancelled wafer mid-solve (2026-11-04).
        clock = [500.0]
        config = parse_site_config(PAGE, "ca")
        budgets = []

        async def site_config(*args, timeout, **kwargs):
            budgets.append(("config", timeout))
            clock[0] += 1.0
            return config

        async def default_location(*args, timeout, **kwargs):
            budgets.append(("location", timeout))
            clock[0] += 0.5
            return LOCATION

        async def fake_search(config, location, **kwargs):
            budgets.append(("search", kwargs["timeout"], kwargs["requested_timeout"]))
            clock[0] += 3.0
            if kwargs["query"]:
                return {"searchResult": {"results": [], "totalSize": 0, "redirectUri": "/televisions.html"}}
            return DATA

        monkeypatch.setattr(costco_search.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(grs, "site_config", site_config)
        monkeypatch.setattr(grs, "default_location", default_location)
        monkeypatch.setattr(grs, "search", fake_search)
        result = await costco_search.search_costco("https://www.costco.ca/s?keyword=tv", timeout=10)

        assert "content" in result
        assert budgets == [
            ("config", 10.0), ("location", 9.0), ("search", 8.5, 10), ("search", 5.5, 10),
        ]

    async def test_a_spent_budget_says_so_instead_of_starting_a_call(self, monkeypatch):
        clock = [500.0]
        config = parse_site_config(PAGE, "ca")
        searched = []

        async def slow_config(*args, timeout, **kwargs):
            clock[0] += 11.0
            return config

        async def fake_search(*args, **kwargs):
            searched.append(kwargs)
            return DATA

        fusion_calls = []

        async def fusion(**kwargs):
            fusion_calls.append(kwargs)
            return None

        monkeypatch.setattr(costco_search.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(grs, "site_config", slow_config)
        monkeypatch.setattr(grs, "search", fake_search)
        monkeypatch.setattr(costco_search.api, "search", fusion)
        result = await costco_search.search_costco("https://www.costco.com/s?keyword=laptop", timeout=10)

        assert searched == []
        assert fusion_calls == []
        assert "used up the 10s timeout" in result["error"]

    async def test_a_late_refusal_keeps_its_reason_instead_of_trying_the_older_service(self, monkeypatch):
        # The catalogue gave up at 9.4 s of 10; the older service then queued
        # behind Costco's request spacing, fetch's own timeout cut it off, and
        # its bare "timed out" replaced the catalogue's reason (2026-11-04).
        clock = [500.0]
        fusion_calls = []

        async def late_refusal(*args, **kwargs):
            clock[0] += 9.4
            raise grs.GrsError(grs._kasada_refusal(429, True, timeout=10))

        async def fusion(**kwargs):
            fusion_calls.append(kwargs)
            return None

        monkeypatch.setattr(costco_search.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(grs, "search", late_refusal)
        monkeypatch.setattr(costco_search.api, "search", fusion)
        result = await costco_search.search_costco("https://www.costco.com/s?keyword=laptop", timeout=10)

        assert fusion_calls == []
        assert "no time was left to try its older search service" in result["error"]
        assert "did not finish within the 10s timeout" in result["error"]
        assert "timeout=60" in result["error"]

    async def test_the_older_service_ends_before_the_callers_deadline(self, monkeypatch):
        async def failing(*args, **kwargs):
            raise grs.GrsError("HTTP 503")

        async def hanging_fusion(**kwargs):
            await asyncio.sleep(30)

        monkeypatch.setattr(costco_search, "_FALLBACK_MIN_SECONDS", 0.05)
        monkeypatch.setattr(costco_search, "_DEADLINE_RESERVE_SECONDS", 0.1)
        monkeypatch.setattr(grs, "search", failing)
        monkeypatch.setattr(costco_search.api, "search", hanging_fusion)
        started = time.monotonic()
        result = await costco_search.search_costco("https://www.costco.ca/s?keyword=laptop", timeout=0.4)

        assert time.monotonic() - started < 0.4
        assert "older search service did not answer either" in result["error"]
        assert "HTTP 503" in result["error"]

    async def test_both_services_failing_names_the_catalogue_refusal(self, monkeypatch):
        async def refused(*args, **kwargs):
            raise grs.GrsError(
                "Costco's catalogue search refused the request with its Kasada bot protection (HTTP 429); "
                "the site's own page sends Kasada tokens with this request"
            )

        async def fusion(**kwargs):
            return None

        monkeypatch.setattr(grs, "search", refused)
        monkeypatch.setattr(costco_search.api, "search", fusion)
        result = await costco_search.search_costco("https://www.costco.ca/s?keyword=laptop")

        assert "content" not in result
        assert "Kasada bot protection (HTTP 429)" in result["error"]
        assert "older search service did not answer either" in result["error"]

    async def test_both_catalogue_reads_get_the_browser_solver(self, monkeypatch):
        # costco.com's Kasada-gated search is signed by the site's own script in
        # wafer's browser, which needs the server's solver on the search session.
        solver = object()
        seen = []

        async def fake_search(config, location, **kwargs):
            seen.append(kwargs.get("browser_solver"))
            if kwargs["query"]:
                return {"searchResult": {"results": [], "totalSize": 0, "redirectUri": "/televisions.html"}}
            return DATA

        monkeypatch.setattr(grs, "search", fake_search)
        await costco_search.search_costco("https://www.costco.com/s?keyword=tv", browser_solver=solver)
        assert seen == [solver, solver]

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

    monkeypatch.setattr(grs, "_get_search_session", lambda *a: _resolve(Session()))
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


class _Response:
    def __init__(self, status: int, headers: dict, payload=None):
        self.status_code = status
        self.headers = headers
        self._payload = payload

    def json(self):
        return self._payload


class _Session:
    def __init__(self, response):
        self.response = response

    async def post(self, *args, **kwargs):
        return self.response


class TestCatalogueRefusal:
    """costco.com, 2026-11-03: an empty text/plain 429 with Kasada headers."""

    async def _search(self, monkeypatch, response):
        config = parse_site_config(PAGE, "ca")

        async def session(*args):
            return _Session(response)

        async def no_wait(*args, **kwargs):
            return None

        monkeypatch.setattr(grs, "_get_search_session", session)
        monkeypatch.setattr(grs.costco_limiter, "wait", no_wait)
        return await grs.search(config, LOCATION, query="laptop", timeout=5)

    async def test_kasada_refusal_is_named(self, monkeypatch):
        response = _Response(429, {"content-type": "text/plain;charset=UTF-8", "x-kpsdk-ct": "0fh", "x-kpsdk-r": "1-AA"})

        with pytest.raises(grs.GrsError, match="Kasada bot protection \\(HTTP 429\\)") as caught:
            await self._search(monkeypatch, response)
        assert "no browser is available here" in str(caught.value)

    async def _signed_search(self, monkeypatch, *, timeout, elapsed, wait=0.0, requested_timeout=None, posts=None):
        """A refused search with a browser: the limiter holds it *wait* seconds
        and the POST takes *elapsed*."""
        response = _Response(429, {"content-type": "text/plain;charset=UTF-8", "x-kpsdk-ct": "0fh", "x-kpsdk-r": "1-AA"})
        clock = [1000.0]
        posts = [] if posts is None else posts

        class _SlowSession:
            async def post(self, *args, **kwargs):
                posts.append(kwargs["timeout"])
                clock[0] += elapsed
                return response

        async def session(*args):
            return _SlowSession()

        async def limiter_wait(*args, **kwargs):
            clock[0] += wait

        monkeypatch.delenv("TZ", raising=False)
        monkeypatch.setattr(grs.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(grs, "_get_search_session", session)
        monkeypatch.setattr(grs.costco_limiter, "wait", limiter_wait)
        config = parse_site_config(PAGE, "ca")
        with pytest.raises(grs.GrsError) as caught:
            await grs.search(
                config, LOCATION, query="laptop", timeout=timeout, browser_solver=object(),
                requested_timeout=requested_timeout,
            )
        return str(caught.value)

    async def test_a_refusal_that_used_up_the_timeout_says_to_raise_it(self, monkeypatch):
        # In the Linux image a signed search takes 15-25 s; at fetch's 10 s
        # default wafer gave up on the browser and handed back the unsigned
        # 429, which used to be reported as refused "even when ... signed".
        message = await self._signed_search(monkeypatch, timeout=10, elapsed=9.2)
        assert "did not finish within the 10s timeout" in message
        assert "timeout=60" in message
        assert "even when" not in message

    async def test_a_refusal_with_time_left_does_not_claim_a_signed_call(self, monkeypatch):
        # wafer hands back the same unsigned 429 when the browser fails before
        # signing anything: right after a cancelled solve, the next one failed
        # at once with TargetClosedError and was reported as refused "even when
        # the site's own page signed it" (2026-11-04).
        message = await self._signed_search(monkeypatch, timeout=90, elapsed=19.5)
        assert "the browser, which has the site's own page sign it, did not get it through" in message
        assert "even when" not in message
        assert "timeout" not in message

    async def test_the_out_of_time_message_names_the_callers_timeout(self, monkeypatch):
        # search_costco hands each call what is left of the caller's budget.
        message = await self._signed_search(monkeypatch, timeout=7.4, elapsed=7.0, requested_timeout=10)
        assert "did not finish within the 10s timeout" in message

    async def test_the_limiters_wait_comes_out_of_the_budget(self, monkeypatch):
        posts = []
        await self._signed_search(monkeypatch, timeout=10, elapsed=1.0, wait=2.5, posts=posts)
        assert posts == [7.5]

    async def test_a_budget_spent_waiting_sends_nothing(self, monkeypatch):
        posts = []
        message = await self._signed_search(monkeypatch, timeout=2, elapsed=1.0, wait=2.5, posts=posts)
        assert posts == []
        assert "used up the 2s timeout" in message
        assert "timeout=60" in message

    @pytest.mark.parametrize(
        ("tz", "named"),
        [
            ("UTC", True),
            ("Etc/UTC", True),
            (":UTC", True),
            ("", True),
            ("America/Toronto", False),
            ("Europe/London", False),
            (None, False),
        ],
    )
    def test_a_refused_signed_call_names_utc_only_when_tz_does(self, monkeypatch, tz, named):
        # From the Linux image Kasada refused even the page's own signed call
        # with the browser on UTC, and accepted it on a named zone (2026-11-03).
        # With no TZ, wafer runs a browser that would inherit a UTC system clock
        # on Europe/London, so only a TZ naming UTC leaves it there.
        if tz is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", tz)
        message = grs._kasada_refusal(429, had_browser=True)
        assert "did not get it through" in message
        assert ("set TZ" in message) is named

    async def test_plain_rate_limit_is_not_called_kasada(self, monkeypatch):
        response = _Response(429, {"content-type": "text/plain", "retry-after": "30"})

        with pytest.raises(grs.GrsError) as caught:
            await self._search(monkeypatch, response)

        assert str(caught.value) == "Costco's catalogue search returned HTTP 429"


class TestServiceRequestsCarryNoCookies:
    """The site's page calls ecom-api and gdx-api cross-origin with fetch()'s
    default credentials, so neither carries a cookie (captured 2026-11-03).
    Once a browser had signed a costco.com search, the shared session and
    cache_dir held the page's consent cookie ``tcm`` (Domain=.costco.com, JSON
    value, expiring 2027), and ecom-api refused every location read with 403."""

    async def test_location_reads_use_the_cookie_free_session(self, monkeypatch):
        used = []

        class Session:
            def __init__(self, name):
                self.name = name

            async def get(self, url, headers=None, timeout=None):
                used.append(self.name)

                class R:
                    status_code = 200

                    @staticmethod
                    def json():
                        return {}

                return R()

        monkeypatch.setattr(grs, "_get_session", lambda: _resolve(Session("page")))
        monkeypatch.setattr(grs, "_get_search_session", lambda *a: _resolve(Session("search")))
        monkeypatch.setattr(grs, "_get_service_session", lambda: _resolve(Session("service")))
        await grs._get_json("https://ecom-api.costco.com/core/warehouse-locator/v1/salesLocations.json", {}, timeout=5)
        assert used == ["service"]

    async def test_the_search_post_uses_the_search_session_with_the_solver(self, monkeypatch):
        config = parse_site_config(PAGE, "ca")
        solver = object()
        offered = []

        async def search_session(browser_solver=None):
            offered.append(browser_solver)
            return _Session(_Response(200, {}, {"searchResult": {"results": [], "totalSize": 0}}))

        async def no_wait(*args, **kwargs):
            return None

        monkeypatch.setattr(grs, "_get_search_session", search_session)
        monkeypatch.setattr(grs.costco_limiter, "wait", no_wait)
        await grs.search(config, LOCATION, query="laptop", timeout=5, browser_solver=solver)
        assert offered == [solver]

    async def test_neither_service_session_shares_the_cache(self, monkeypatch):
        from fetchaller.costco import api

        built = []

        class FakeSession:
            def __init__(self, **kwargs):
                built.append(kwargs)

        monkeypatch.setattr(api.wafer, "AsyncSession", FakeSession)
        await api.close_session()
        solver = object()
        try:
            await api._get_service_session()
            await api._get_search_session()
            await api._get_search_session(solver)  # rebuilt once a solver is offered
            await api._get_search_session(solver)  # and then kept
            await api._get_session()
        finally:
            await api.close_session()

        service, search_cold, search_solver, page = built
        assert "cache_dir" not in service and "browser_solver" not in service
        assert "cache_dir" not in search_cold and search_cold["browser_solver"] is None
        assert "cache_dir" not in search_solver and search_solver["browser_solver"] is solver
        assert "cache_dir" in page  # pages keep the shared cache
