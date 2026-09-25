"""Facebook Marketplace: the request form, the withheld-results guard, and
reading a Marketplace page's own search.

Shapes are trimmed from live pages and GraphQL answers captured 2026-09-25
(/marketplace/toronto/search?query=bicycle, /marketplace/vancouver/search?
query=kayak&minPrice=100&maxPrice=800, /marketplace/toronto/).
"""

import json

import pytest

from fetchaller.facebook_marketplace import graphql as g
from fetchaller.facebook_marketplace import page as fb_page
from fetchaller.facebook_marketplace import search as fb_search

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _cursor(c2c_it, b2c_it=0):
    return json.dumps({"pg": 0, "b2c": {"br": "", "it": b2c_it}, "c2c": {"br": "Abp", "it": c2c_it}})


def _search_listing(listing_id, title, price, city):
    return {
        "node": {
            "__typename": "MarketplaceFeedListingStoryObject",
            "listing": {
                "id": listing_id,
                "marketplace_listing_title": title,
                "listing_price": {"formatted_amount": price},
                "location": {"reverse_geocode": {"city": city}},
            },
        }
    }


def _search_data(edges, it, city="Toronto"):
    return {
        "data": {
            "marketplace_search": {
                "feed_units": {"edges": edges, "page_info": {"end_cursor": _cursor(it), "has_next_page": True}}
            },
            "viewer": {"buy_location": {"buy_location": {"location": {"reverse_geocode": {"city": city}}}}},
        },
        "extensions": {"is_final": True},
    }


def _search_variables(lat, lng, slug, lower=0, upper=214748364700):
    return {
        "buyLocation": {"latitude": lat, "longitude": lng},
        "count": 24,
        "params": {
            "bqf": {"callsite": "COMMERCE_MKTPLACE_SEO_USERS", "query": "kayak"},
            "browse_request_params": {
                "filter_location_latitude": lat,
                "filter_location_longitude": lng,
                "filter_price_lower_bound": lower,
                "filter_price_upper_bound": upper,
                "filter_radius_km": 65,
            },
        },
        "topicPageParams": {"location_id": slug, "url": None},
    }


def _html(preloaders, streams):
    parts = [
        '<html><head></head><body><script type="application/json" data-sjs>'
        + json.dumps({"require": [["CometPlatformRootClient", "initialize", [], [{"expectedPreloaders": preloaders}]]]})
        + "</script>"
    ]
    for preloader_id, result, complete in streams:
        payload = {"__bbox": {"complete": complete, "result": result}}
        parts.append(
            '<script type="application/json" data-sjs>'
            + json.dumps({"require": [["ScheduledServerJS", "handle", None, [{"__bbox": {"require": [
                ["RelayPrefetchedStreamCache", "next", [], [preloader_id, payload]]
            ]}}]]]}, separators=(",", ":"))
            + "</script>"
        )
    return "".join(parts) + "</body></html>"


def _search_page_html(data, lat=49.2819, lng=-123.1204, slug="vancouver", lower=10000, upper=80000):
    preloaders = [
        {"actorID": None, "preloaderID": "adp_useCometLogInFormQueryRelayPreloader_1",
         "queryID": "9703687583048540", "variables": {"source": "DESKTOP"}, "queryName": "useCometLogInFormQuery"},
        {"actorID": "0", "preloaderID": "adp_CometMarketplaceSearchContentContainerQueryRelayPreloader_2",
         "queryID": "27517490627932547", "variables": _search_variables(lat, lng, slug, lower, upper),
         "queryName": fb_page.SEARCH_QUERY_NAME},
    ]
    streams = [
        ("adp_useCometLogInFormQueryRelayPreloader_1", {"data": {"login_data": {}}}, True),
        ("adp_CometMarketplaceSearchContentContainerQueryRelayPreloader_2", data, True),
    ]
    return _html(preloaders, streams)


def _browse_page_html():
    feed_id = "adp_MarketplaceCometBrowseFeedLightContainerQueryRelayPreloader_3"
    preloaders = [
        {"actorID": "0", "preloaderID": feed_id, "queryID": "28053535904279798",
         "variables": {"buyLocation": {"latitude": 43.648, "longitude": -79.3872}, "count": 1, "radius": 65000},
         "queryName": "MarketplaceCometBrowseFeedLightContainerQuery"},
    ]
    top_picks = {
        "__typename": "MarketplaceFeedTopPicksUnit",
        "marketplace_listings": [
            {"id": "1719357285801027", "marketplace_listing_title": "White Chest Freezer",
             "formatted_price": {"text": "CA$60"}, "is_pending": False, "is_sold": False,
             "location": {"reverse_geocode": {"city": "Grimsby",
                                              "city_page": {"display_name": "Grimsby, Ontario"}}}},
            {"id": "919207764402712", "marketplace_listing_title": "Doggy Playpen",
             "formatted_price": {"text": "CA$30"}, "is_pending": True,
             "location": {"reverse_geocode": {"city": "Bowmanville"}}},
        ],
        "viewer": {"marketplace_feed_stories": {"buy_location": {"display_name": "Toronto, Ontario"}}},
    }

    def general(listing_id, title, currency, amount):
        return {
            "label": "MarketplaceCometBrowseFeedLight_dataConnection$stream$MarketplaceBrowseFeedLight_marketplace_home_feed",
            "path": ["marketplace_home_feed", "edges", 1],
            "data": {"node": {
                "__typename": "MarketplaceFeedGeneralListingObject",
                "entity_id": listing_id,
                "data": {"title": title, "price": {"currency": currency, "amount_with_offset": amount}},
                "listing": {"id": listing_id},
                "strikethrough_price": None,
                "entity": {"location": {"reverse_geocode": {"city": "Mississauga",
                                                            "city_page": {"display_name": "Mississauga, Ontario"}}}},
            }},
        }

    streams = [
        (feed_id, {"data": {"marketplace_home_feed": {"edges": [{"node": top_picks}]}}}, False),
        (feed_id, general("1557919525617904", "Blink Cameras-$25 Only", "CAD", "2500"), False),
        (feed_id, general("555", "Rice cooker", "JPY", "3000"), False),
        # The same listing streamed twice renders once.
        (feed_id, general("1557919525617904", "Blink Cameras-$25 Only", "CAD", "2500"), False),
    ]
    return _html(preloaders, streams)


# ---------------------------------------------------------------------------
# GraphQL request form and body decoding
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status

    def raise_for_status(self):
        return None


class TestRequestForm:
    async def test_every_request_carries_the_comet_fields(self, monkeypatch):
        """Without __a and __comet_req the search answers 200 with its matches
        counted in the cursor and none of them served."""
        sent = {}

        class _Session:
            async def post(self, url, **kwargs):
                sent.update(kwargs["form"])
                return _Resp(json.dumps({"data": {}}))

        async def _session(seed=True):
            return _Session()

        async def _no_wait():
            return None

        monkeypatch.setattr(g, "_get_session", _session)
        monkeypatch.setattr(g.facebook_limiter, "wait", _no_wait)
        await g.graphql_request(g.DOC_ID_SEARCH, {"count": 24})

        assert sent["__a"] == "1"
        assert sent["__comet_req"] == "15"
        assert sent["doc_id"] == g.DOC_ID_SEARCH
        assert json.loads(sent["variables"]) == {"count": 24}


class TestDecodeBody:
    def test_single_document(self):
        assert g.decode_graphql_body('{"data": {"a": 1}}') == {"data": {"a": 1}}

    def test_deferred_parts_after_the_first_line_are_dropped(self):
        body = '{"data": {"a": 1}}\n{"label": "x$defer$y", "data": {"b": 2}}\n'
        assert g.decode_graphql_body(body) == {"data": {"a": 1}}

    def test_xssi_prefix(self):
        assert g.decode_graphql_body('for (;;);{"data": {}}') == {"data": {}}

    def test_not_an_object(self):
        with pytest.raises(ValueError):
            g.decode_graphql_body("[1, 2]")
        with pytest.raises(ValueError):
            g.decode_graphql_body("<html>")


class TestWithheld:
    def test_matched_but_not_served_is_an_error(self):
        data = _search_data([], it=12)
        message = g.withheld_listings_error(data, [])
        assert message.startswith("Facebook Marketplace matched 12 listings for this search but returned none")

    def test_genuine_empty_search_is_not(self):
        assert g.withheld_listings_error(_search_data([], it=0), []) is None

    def test_served_listings_are_not(self):
        data = _search_data([_search_listing("1", "Bike", "CA$35", "Toronto")], it=1)
        assert g.withheld_listings_error(data, g.parse_search_response(data)) is None

    def test_unreadable_cursor_raises_no_false_alarm(self):
        data = {"data": {"marketplace_search": {"feed_units": {"edges": [], "page_info": {"end_cursor": "opaque"}}}}}
        assert g.withheld_listings_error(data, []) is None
        assert g.matched_count({}) == 0


# ---------------------------------------------------------------------------
# Reading the page
# ---------------------------------------------------------------------------


class TestParseSearchPage:
    def test_search_page_carries_the_resolved_search_and_its_result(self):
        data = _search_data([_search_listing("1378398480414603", "Kayak", "CA$100", "Burnaby")], it=1, city="Vancouver")
        got = fb_page.parse_search_page(_search_page_html(data))

        # The slug resolved by Facebook, not a free-text geocode (Vancouver, WA).
        assert (got.latitude, got.longitude, got.location_id) == (49.2819, -123.1204, "vancouver")
        assert got.city == "Vancouver"
        assert got.radius_km == 65
        assert got.doc_id == "27517490627932547"
        assert got.variables["params"]["browse_request_params"]["filter_price_lower_bound"] == 10000
        assert [item["title"] for item in g.parse_search_response(got.result)] == ["Kayak"]
        assert got.browse is None

    def test_browse_page_reads_its_streamed_feed(self):
        got = fb_page.parse_search_page(_browse_page_html())

        assert got.result is None and got.doc_id == ""
        assert (got.latitude, got.longitude, got.radius_km) == (43.648, -79.3872, 65)
        assert got.city == "Toronto, Ontario"
        assert [(i["title"], i.get("price"), i.get("location"), i.get("status")) for i in got.browse] == [
            ("White Chest Freezer", "CA$60", "Grimsby, Ontario", None),
            ("Doggy Playpen", "CA$30", "Bowmanville", "Pending"),
            ("Blink Cameras-$25 Only", "CAD 25", "Mississauga, Ontario", None),
            # Unverified scale: said, not divided by 100.
            ("Rice cooker", "JPY 3000 (amount_with_offset, unscaled)", "Mississauga, Ontario", None),
        ]
        assert got.browse[0]["url"] == "https://www.facebook.com/marketplace/item/1719357285801027/"

    def test_a_page_with_no_marketplace_preloaders_is_none(self):
        assert fb_page.parse_search_page("<html><body>Log in to Facebook</body></html>") is None

    def test_page_url_is_the_desktop_page(self):
        assert fb_page._page_url("https://m.facebook.com/marketplace/toronto/search?query=desk") == \
            "https://www.facebook.com/marketplace/toronto/search?query=desk"


# ---------------------------------------------------------------------------
# The fetch path end to end (network faked at the page and GraphQL seams)
# ---------------------------------------------------------------------------


class TestSearchMarketplaceUrl:
    @staticmethod
    def _install(monkeypatch, page, graphql_answer=None, geocode=None):
        calls = {"graphql": [], "geocode": []}

        async def _page(url):
            return page

        async def _graphql(doc_id, variables):
            calls["graphql"].append((doc_id, variables))
            return graphql_answer

        async def _geocode(text):
            calls["geocode"].append(text)
            return geocode

        monkeypatch.setattr(fb_search, "fetch_search_page", _page)
        monkeypatch.setattr(fb_search, "graphql_request", _graphql)
        monkeypatch.setattr(fb_search, "geocode_location", _geocode)
        return calls

    async def test_streamed_result_is_rendered_without_another_request(self, monkeypatch):
        data = _search_data([_search_listing("1", "Kayak", "CA$100", "Burnaby")], it=1, city="Vancouver")
        calls = self._install(monkeypatch, fb_page.parse_search_page(_search_page_html(data)))

        out = await fb_search.search_marketplace(
            "https://www.facebook.com/marketplace/vancouver/search?query=kayak&minPrice=100&maxPrice=800"
        )

        assert out["content"].startswith('Facebook Marketplace: "kayak" | Vancouver | 65km')
        assert "**Kayak**" in out["content"]
        assert calls == {"graphql": [], "geocode": []}

    async def test_search_the_page_ran_but_did_not_stream_is_replayed(self, monkeypatch):
        page = fb_page.SearchPage(latitude=1.0, longitude=2.0, city="Vancouver", radius_km=65,
                                  doc_id="27517490627932547", variables={"count": 24})
        answer = _search_data([_search_listing("1", "Kayak", "CA$100", "Burnaby")], it=1)
        calls = self._install(monkeypatch, page, graphql_answer=answer)

        out = await fb_search.search_marketplace("https://www.facebook.com/marketplace/vancouver/search?query=kayak")

        assert calls["graphql"] == [("27517490627932547", {"count": 24})]
        assert "**Kayak**" in out["content"]

    async def test_browse_page_renders_its_feed(self, monkeypatch):
        calls = self._install(monkeypatch, fb_page.parse_search_page(_browse_page_html()))
        out = await fb_search.search_marketplace("https://www.facebook.com/marketplace/toronto/")

        assert out["content"].startswith(
            "Facebook Marketplace: Toronto, Ontario | 65km | browse feed, first screen (4 listings, no search term)"
        )
        assert calls == {"graphql": [], "geocode": []}

    async def test_empty_browse_feed_is_said_not_rendered_as_an_empty_market(self, monkeypatch):
        self._install(monkeypatch, fb_page.SearchPage(latitude=1.0, longitude=2.0, city="Toronto", browse=[]))
        out = await fb_search.search_marketplace("https://www.facebook.com/marketplace/toronto/")
        assert "carried no listings" in out["error"]

    async def test_without_the_page_prices_are_whole_units_sent_as_cents(self, monkeypatch):
        answer = _search_data([_search_listing("1", "Kayak", "CA$100", "Burnaby")], it=1)
        calls = self._install(monkeypatch, None, graphql_answer=answer,
                              geocode={"latitude": 49.28, "longitude": -123.12, "name": "Vancouver, BC"})

        out = await fb_search.search_marketplace(
            "https://www.facebook.com/marketplace/vancouver/search?query=kayak&minPrice=100&maxPrice=800"
        )

        (doc_id, variables), = calls["graphql"]
        bounds = variables["params"]["browse_request_params"]
        assert (bounds["filter_price_lower_bound"], bounds["filter_price_upper_bound"]) == (10000, 80000)
        assert calls["geocode"] == ["vancouver"]
        assert "Vancouver, BC" in out["content"]

    async def test_withheld_answer_is_an_error(self, monkeypatch):
        self._install(monkeypatch, None, graphql_answer=_search_data([], it=12),
                      geocode={"latitude": 43.6, "longitude": -79.3, "name": "Toronto"})
        out = await fb_search.search_marketplace("https://www.facebook.com/marketplace/toronto/search?query=bicycle")
        assert "matched 12 listings" in out["error"]
