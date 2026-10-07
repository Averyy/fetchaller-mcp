"""Tests for The Home Depot: homedepot.com through its gateway, homedepot.ca
through its page state.

Most of what can go wrong here renders something plausible rather than failing:
a product skeleton with every field null, a review page whose statistics belong
to a sibling variant, a sale shown without its was-price, a search that the
site redirects rendered as an empty result, a row with no online price shown
as "unpriced". Each test below pins one of those.
"""

import json

import pytest
import wafer

from fetchaller.homedepot import ca, com, page
from fetchaller.homedepot.render import (
    ca_price_line,
    ca_reviews_from_seo,
    com_fulfillment_lines,
    com_price_line,
    render_ca_listing,
    render_ca_product,
    render_com_listing,
    render_com_product,
    render_com_reviews,
    render_com_store,
)
from fetchaller.homedepot.urls import ComTarget, ca_target, com_target, is_homedepot, with_param


class _NoWait:
    async def wait(self, extra_delay: float = 0.0) -> None:
        return None


@pytest.fixture(autouse=True)
def _no_spacing(monkeypatch):
    from fetchaller import ratelimit

    monkeypatch.setattr(ratelimit, "homedepot_com_limiter", _NoWait())
    monkeypatch.setattr(ratelimit, "homedepot_ca_limiter", _NoWait())
    monkeypatch.setattr(ca, "_last_page_at", 0.0)


# ---------------------------------------------------------------------------
# URL classification
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _gateway_hold_released():
    """A refusal held in one test must not leak into the next."""
    com._release()
    com._hold_started = 0.0
    yield
    com._release()
    com._hold_started = 0.0


class TestComUrls:
    def test_product(self):
        t = com_target("https://www.homedepot.com/p/DEWALT-20V-MAX-Drill-DCD771C2/204279858")
        assert t == ComTarget(kind="product", item_id="204279858")

    def test_bare_product_id(self):
        assert com_target("https://www.homedepot.com/p/204279858").item_id == "204279858"

    def test_reviews_page_number(self):
        t = com_target("https://www.homedepot.com/p/reviews/DEWALT-Drill/204279858/3")
        assert (t.kind, t.item_id, t.review_page) == ("reviews", "204279858", 3)

    def test_reviews_without_page_is_page_one(self):
        t = com_target("https://www.homedepot.com/p/reviews/DEWALT-Drill/204279858")
        assert t.review_page == 1

    def test_store(self):
        t = com_target("https://www.homedepot.com/l/Cumberland/GA/Atlanta/30339/121")
        assert (t.kind, t.store_id, t.store_zip) == ("store", "121", "30339")

    def test_search_with_paging_sort_and_price(self):
        t = com_target(
            "https://www.homedepot.com/s/cordless%20drill?NCNI-5&Nao=48&sortby=price&sortorder=desc"
            "&lowerBound=50&upperbound=150"
        )
        assert t.kind == "listing"
        assert t.keyword == "cordless drill"
        assert t.start_index == 48
        assert (t.sort_field, t.sort_order) == ("PRICE", "desc")
        assert (t.lower_bound, t.upper_bound) == (50, 150)

    def test_browse_with_keyword(self):
        t = com_target("https://www.homedepot.com/b/Tools/N-5yc1vZc1xy/Ntk-elasticplus/Ntt-drill?NCNI-5")
        assert (t.nav_param, t.keyword) == ("5yc1vZc1xy", "drill")

    def test_unknown_sort_has_no_field(self):
        t = com_target("https://www.homedepot.com/s/drill?sortby=deliverydate")
        assert t.sort_by == "deliverydate"
        assert t.sort_field is None

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.homedepot.com/",
            "https://www.homedepot.com/c/Return_Policy",
            "https://www.homedepot.com/p/questions/DEWALT-Drill/204279858",
            "https://www.homedepot.com/b/Tools",
            "https://www.homedepot.ca/search?q=drill",
        ],
    )
    def test_not_read_here(self, url):
        assert com_target(url) is None


class TestCaUrls:
    def test_product_english_and_french(self):
        assert ca_target("https://www.homedepot.ca/product/m12-drill/1001918582").code == "1001918582"
        fr = ca_target("https://www.homedepot.ca/produit/m12-perceuse/1001918582")
        assert (fr.kind, fr.lang) == ("product", "fr")

    def test_search(self):
        t = ca_target("https://www.homedepot.ca/search?q=hammer%20drill%20bit&page=2&sort=price-asc&filter=9wh")
        assert (t.kind, t.query, t.page, t.sort, t.filter) == ("search", "hammer drill bit", 2, "price-asc", "9wh")

    def test_french_search(self):
        t = ca_target("https://www.homedepot.ca/rechercher?q=perceuse")
        assert (t.kind, t.lang) == ("search", "fr")

    def test_category_and_refinement(self):
        url = "https://www.homedepot.ca/en/home/categories/tools/power-tools/drills/drill-drivers.html/f/milwaukee-tool/boj-9wh"
        assert ca_target(url).kind == "category"

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.homedepot.ca/",
            "https://www.homedepot.ca/en/home.html",
            "https://www.homedepot.ca/search",
            "https://www.homedepot.com/s/drill",
        ],
    )
    def test_not_read_here(self, url):
        assert ca_target(url) is None

    def test_is_homedepot(self):
        assert is_homedepot("https://homedepot.ca/x")
        assert is_homedepot("https://www.homedepot.com/x")
        assert not is_homedepot("https://www.homedepot.com.evil.example/x")


def test_with_param_replaces_only_the_named_parameter():
    url = "https://www.homedepot.com/s/drill?NCNI-5&sortby=price&Nao=24"
    out = with_param(url, "Nao", 48)
    assert "Nao=48" in out and "Nao=24" not in out
    assert "sortby=price" in out and "NCNI-5" in out


# ---------------------------------------------------------------------------
# homedepot.com pricing and fulfilment
# ---------------------------------------------------------------------------


class TestComPrice:
    def test_original_equal_to_value_is_not_a_sale(self):
        assert com_price_line({"value": 119.0, "original": 119.0}) == "$119.00"

    def test_sale_names_the_was_price(self):
        line = com_price_line({"value": 179.0, "original": 239.0, "promotion": {"percentageOff": 25}})
        assert line == "$179.00 (was $239.00, save $60.00 (25%))"

    def test_hidden_price_is_said_not_zeroed(self):
        line = com_price_line({"value": None, "message": "See Lower Price in Cart"})
        assert line == "not shown on the page (See Lower Price in Cart)"
        assert "not shown" in com_price_line({"value": 10.0}, {"hidePrice": True})

    def test_starting_at_qualifies_the_number(self):
        assert com_price_line({"value": 29.98, "message": "Starting at"}) == "Starting at $29.98"

    def test_unit_price(self):
        line = com_price_line({"value": 18.78, "alternate": {"unit": {"value": 0.01, "caseUnitOfMeasure": "nail"}}})
        assert line == "$18.78 · $0.01 per nail"


FULFILLMENT = {
    "backordered": False,
    "excludedShipStates": "AK,HI",
    "fulfillmentOptions": [
        {
            "type": "pickup",
            "fulfillable": True,
            "services": [
                {
                    "type": "bopis",
                    "locations": [
                        {
                            "isAnchor": True,
                            "locationId": "121",
                            "storeName": "Cumberland",
                            "state": "GA",
                            "inventory": {"quantity": 17, "isInStock": True},
                        }
                    ],
                }
            ],
        },
        {
            "type": "delivery",
            "fulfillable": True,
            "services": [
                {"type": "sth", "hasFreeShipping": True, "deliveryDates": {"startDate": "2026-10-05", "endDate": "2026-10-07"}}
            ],
        },
    ],
}


def test_fulfillment_lines():
    lines = com_fulfillment_lines(FULFILLMENT)
    assert lines == [
        "- Pickup in store (Cumberland, GA #121): 17 in stock",
        "- Ship to home: arrives 2026-10-05 – 2026-10-07, free",
        "- Does not ship to: AK, HI",
    ]


PRODUCT = {
    "itemId": "204279858",
    "identifiers": {
        "brandName": "DEWALT",
        "productLabel": "20V MAX Drill",
        "modelNumber": "DCD771C2",
        "canonicalUrl": "/p/DEWALT-20V-MAX-Drill-DCD771C2/204279858",
    },
    "details": {
        "description": "A drill.",
        "descriptiveAttributes": [
            {"name": "Bullet01", "value": "Compact", "bulleted": True},
            {"name": "Bullet22", "value": '<a href="https://www.homedepot.com/b/DEWALT/N-5yc1vZ4j2">Shop All</a>', "bulleted": True},
        ],
    },
    "specificationGroup": [
        {"specTitle": "Details", "specifications": [{"specName": "Chuck Size (In.)", "specValue": "1/2 In."}]}
    ],
    "pricing": {"value": 119.0, "original": None},
    "reviews": {"ratingsReviews": {"averageRating": "4.6237", "totalReviews": "11399"}},
    "fulfillment": FULFILLMENT,
    "taxonomy": {"breadCrumbs": [{"label": "Tools"}, {"label": "Drills"}]},
    "info": {"returnable": "90-Day"},
}


def test_product_names_its_store_and_keeps_link_bullets():
    out = render_com_product(PRODUCT, "https://www.homedepot.com/p/204279858")
    assert "**Store:** #121 Cumberland, GA (no store selected)" in out
    assert "- [Shop All](https://www.homedepot.com/b/DEWALT/N-5yc1vZ4j2)" in out
    assert "- Chuck Size (In.): 1/2 In." in out
    assert "★4.6/5 from 11,399 reviews" in out
    assert "/p/reviews/DEWALT-20V-MAX-Drill-DCD771C2/204279858/1" in out


def _listing_model(**overrides):
    model = {
        "searchReport": {"keyword": "drill", "totalProducts": 30, "sortBy": "bestmatch", "startIndex": 0},
        "metadata": {"stores": {"storeId": "121", "storeName": "Cumberland"}},
        "taxonomy": {
            "breadCrumbs": [
                {"label": "Tools", "dimensionName": "Category"},
                {"label": "$50 - $150", "dimensionName": "Price"},
            ]
        },
        "appliedDimensions": [{"label": "Price", "refinements": [{"label": "$50 - $150"}]}],
        "dimensions": [{"label": "Brand", "refinements": [{"label": "DEWALT", "recordCount": "9", "url": "/b/x"}]}],
        "products": [
            {
                "itemId": "1",
                "identifiers": {"brandName": "RYOBI", "productLabel": "Drill", "canonicalUrl": "/p/ryobi/1"},
                "pricing": {"value": 49.97},
                "info": {"isSponsored": True},
            }
        ],
    }
    model.update(overrides)
    return model


class TestComListing:
    def test_sponsored_rows_are_labelled(self):
        target = com_target("https://www.homedepot.com/b/Tools/N-5yc1vZc1xy")
        out = render_com_listing(_listing_model(), target, "https://www.homedepot.com/b/Tools/N-5yc1vZc1xy")
        assert "· Sponsored" in out

    def test_breadcrumb_keeps_categories_and_filters_are_listed_separately(self):
        target = com_target("https://www.homedepot.com/b/Tools/N-5yc1vZc1xy")
        out = render_com_listing(_listing_model(), target, "https://www.homedepot.com/b/Tools/N-5yc1vZc1xy")
        assert "**Category:** Tools\n" in out
        assert "**Filters applied:** Price: $50 - $150" in out

    def test_next_page_moves_nao_by_a_full_page(self):
        url = "https://www.homedepot.com/s/drill"
        out = render_com_listing(_listing_model(), com_target(url), url)
        assert "Next page: https://www.homedepot.com/s/drill?Nao=24" in out

    def test_unknown_sort_is_said(self):
        url = "https://www.homedepot.com/s/drill?sortby=deliverydate"
        out = render_com_listing(_listing_model(), com_target(url), url)
        assert "`sortby=deliverydate` is not a sort homedepot.com offers" in out

    def test_redirect_is_disclosed(self):
        url = "https://www.homedepot.com/s/dewalt"
        out = render_com_listing(
            _listing_model(), com_target(url), url, redirected_to="https://www.homedepot.com/b/DEWALT/N-5yc1vZ4j2"
        )
        assert 'sends the search "dewalt" to https://www.homedepot.com/b/DEWALT/N-5yc1vZ4j2' in out


REVIEWS = {
    "TotalResults": 11399,
    "Includes": {
        "Products": {
            # The page's lead product, not the URL's: reading this as the
            # product's statistics reported 4.7 from 7,519.
            "store": {"Id": "308959619", "FilteredReviewStatistics": {"AverageOverallRating": "4.7", "TotalReviewCount": 7519}},
            "items": [
                {
                    "Id": "204279858",
                    "FilteredReviewStatistics": {
                        "AverageOverallRating": "4.6237",
                        "TotalReviewCount": 11399,
                        "RecommendedCount": 2401,
                        "NotRecommendedCount": 286,
                        "RatingDistribution": [{"RatingValue": 1, "Count": 416}, {"RatingValue": 5, "Count": 9076}],
                    },
                }
            ],
        }
    },
    "Results": [
        {"ProductId": "308959619", "Rating": 4, "UserNickname": "RICHARD", "SubmissionTime": "2026-09-24T00:00:00", "BadgesOrder": ["verifiedPurchaser"]},
        {"ProductId": "204279858", "Rating": 5, "ReviewText": "Great", "UserNickname": "Ann", "BadgesOrder": ["earlyReviewerIncentive"]},
    ],
}


def test_reviews_use_the_urls_own_statistics_and_mark_sibling_variants():
    target = com_target("https://www.homedepot.com/p/reviews/DEWALT-Drill/204279858/2")
    out = render_com_reviews({"identifiers": {"brandName": "DEWALT", "productLabel": "Drill"}}, REVIEWS, target, "https://www.homedepot.com/p/reviews/DEWALT-Drill/204279858/2")
    assert "★4.6/5 from 11,399 reviews" in out
    assert "7,519" not in out
    assert "5★ 9,076 | 1★ 416" in out
    assert "RICHARD · 2026-09-24 · review of variant Internet # 308959619 · Verified purchaser" in out
    assert "Ann ·  · Early Reviewer Incentive" not in out  # empty date is dropped, not printed
    assert "Ann · Early Reviewer Incentive" in out
    assert "Next page: https://www.homedepot.com/p/reviews/DEWALT-Drill/204279858/3" in out


def test_store_renders_services_as_present_or_absent():
    store = {
        "storeId": "0121",
        "name": "Cumberland",
        "address": {"street": "2450 Cumberland Pkwy", "city": "Atlanta", "state": "GA", "postalCode": "30339"},
        "services": {"toolRental": True, "propane": False},
        "storeHours": {"monday": {"open": "6:00", "close": "22:00"}},
    }
    out = render_com_store(store, "https://www.homedepot.com/l/x/GA/Atlanta/30339/121")
    assert "- Tool rental: ✓" in out
    assert "- Propane exchange: —" in out
    assert "- Monday: 6:00–22:00" in out


# ---------------------------------------------------------------------------
# homedepot.com gateway
# ---------------------------------------------------------------------------


def _gateway(payload, status=200):
    class _Response:
        status_code = status

        def json(self):
            return payload

    class _Session:
        def __init__(self):
            self.calls = []

        async def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return _Response()

    return _Session()


async def _resolve(value):
    return value


class TestGateway:
    async def test_unknown_item_is_an_error(self, monkeypatch):
        session = _gateway({"errors": [{"message": "ItemId: 999 not found"}], "data": {"product": None}})
        monkeypatch.setattr(com, "get_session", lambda: _resolve(session))
        with pytest.raises(com.GatewayError, match="not found"):
            await com.fetch_product("999", timeout=5)

    async def test_store_ids_compare_without_zero_padding(self, monkeypatch):
        session = _gateway({"data": {"storeSearch": {"stores": [{"storeId": "0121", "name": "Cumberland"}]}}})
        monkeypatch.setattr(com, "get_session", lambda: _resolve(session))
        store, note = await com.fetch_store("121", "30339", timeout=5)
        assert store["name"] == "Cumberland" and note is None

    async def test_relocated_store_follows_the_sites_own_label(self, monkeypatch):
        stores = [{"storeId": "6140", "name": "Midtown Manhattan (Relo 6177)"}]
        session = _gateway({"data": {"storeSearch": {"stores": stores}}})
        monkeypatch.setattr(com, "get_session", lambda: _resolve(session))
        store, note = await com.fetch_store("6177", "10022", timeout=5)
        assert store["storeId"] == "6140"
        assert "Store #6177 has relocated" in note

    async def test_keyword_redirect_is_followed_with_the_callers_paging(self, monkeypatch):
        calls = []

        async def fake_listing(target, *, timeout, nav_param=None, keyword=None):
            calls.append((nav_param, keyword, target.start_index))
            if nav_param is None:
                return {"searchReport": None, "metadata": {"searchRedirect": "/b/DEWALT/N-5yc1vZ4j2?NCNI-5"}, "products": []}
            return _listing_model(searchReport={"totalProducts": 2977, "sortBy": "topsellers", "startIndex": 24})

        monkeypatch.setattr(com, "fetch_listing", fake_listing)
        result = await page.get_homedepot("https://www.homedepot.com/s/dewalt?Nao=24", timeout=5)
        assert calls == [(None, None, 24), ("5yc1vZ4j2", "", 24)]
        assert "sends the search \"dewalt\" to https://www.homedepot.com/b/DEWALT/N-5yc1vZ4j2" in result["content"]

    async def test_unknown_category_is_an_error_not_an_empty_listing(self, monkeypatch):
        async def fake_listing(target, *, timeout, nav_param=None, keyword=None):
            return {"searchReport": None, "metadata": {}, "products": []}

        monkeypatch.setattr(com, "fetch_listing", fake_listing)
        result = await page.get_homedepot("https://www.homedepot.com/b/Nope/N-5yc1vZzzzz", timeout=5)
        assert "no listing for category N-5yc1vZzzzz" in result["error"]

    async def test_pages_it_does_not_read_fall_through(self):
        assert await page.get_homedepot("https://www.homedepot.com/c/Return_Policy", timeout=5) is None

    async def test_an_edge_refusal_says_who_refused(self, monkeypatch):
        """Akamai's edge answers 206 + "Generic errors"; that must not read as a partial result."""

        class _Refused:
            status_code = 206
            headers = {"server": "AkamaiGHost", "content-type": "application/json"}

            def json(self):
                return {"data": {"GenericError": None}, "error": [{"message": "Generic errors"}]}

        class _Session:
            async def post(self, *args, **kwargs):
                return _Refused()

        monkeypatch.setattr(com, "get_session", lambda: _resolve(_Session()))
        with pytest.raises(com.GatewayError) as excinfo:
            await com.fetch_product("204279858", timeout=5)
        assert str(excinfo.value) == (
            "homedepot.com's data gateway returned HTTP 206 from its Akamai edge: Generic errors. "
            "Not calling it again for 10 minutes"
        )

    async def test_a_refusal_holds_the_gateway_and_answered_reads_release_it(self, monkeypatch):
        """Reads during a hold fail at once; the probe after it doubles or releases the hold."""
        posts = []
        answers = []

        class _Refused:
            status_code = 206
            headers = {"server": "AkamaiGHost"}

            def json(self):
                return {"error": [{"message": "Generic errors"}]}

        class _Answered:
            status_code = 200
            headers = {}

            def json(self):
                return {"data": {"product": {"itemId": "1"}}}

        class _Session:
            async def post(self, *args, **kwargs):
                posts.append(1)
                return answers.pop(0)

        monkeypatch.setattr(com, "get_session", lambda: _resolve(_Session()))
        clock = [1000.0]
        monkeypatch.setattr(com, "_clock", lambda: clock[0])

        answers.append(_Refused())
        with pytest.raises(com.GatewayError, match="10 minutes"):
            await com.fetch_product("1", timeout=5)
        with pytest.raises(com.GatewayError, match="not calling it again for 10 minutes"):
            await com.fetch_product("1", timeout=5)
        assert len(posts) == 1  # the held read never reached the gateway

        clock[0] += 601
        answers.append(_Refused())
        with pytest.raises(com.GatewayError, match="20 minutes"):
            await com.fetch_product("1", timeout=5)
        assert len(posts) == 2

        clock[0] += 1201
        answers.append(_Answered())
        await com.fetch_product("1", timeout=5)
        assert (com._hold_until, com._hold_length) == (0.0, 0.0)

    async def test_the_hold_stops_doubling_at_its_cap(self, monkeypatch):
        clock = [0.0]
        monkeypatch.setattr(com, "_clock", lambda: clock[0])
        for _ in range(6):
            held = com._hold()
            clock[0] += held + 1  # each probe comes after the hold ran out
        assert held == com._REFUSAL_HOLD_MAX

    async def test_a_burst_of_refusals_in_flight_is_one_hold(self, monkeypatch):
        """Reads already sent when the edge starts refusing join one hold.

        Each doubling it would have jumped straight to the 30-minute cap, and a
        read sent before the hold began must not release it by answering.
        """
        clock = [100.0]
        monkeypatch.setattr(com, "_clock", lambda: clock[0])
        sent_before = clock[0]
        assert com._hold() == com._REFUSAL_HOLD
        clock[0] += 1
        assert com._hold() == com._REFUSAL_HOLD - 1  # joined, not doubled
        assert com._hold_length == com._REFUSAL_HOLD
        clock[0] += 1
        com._release(sent_before - 1)  # an older in-flight read answered
        assert com._hold_until > clock[0]
        com._release(clock[0])  # the probe, sent after the hold began
        assert com._hold_until == 0.0

    async def test_a_non_json_failure_keeps_the_bare_status(self, monkeypatch):
        class _Broken:
            status_code = 503
            headers = {}

            def json(self):
                raise ValueError("not json")

        class _Session:
            async def post(self, *args, **kwargs):
                return _Broken()

        monkeypatch.setattr(com, "get_session", lambda: _resolve(_Session()))
        with pytest.raises(com.GatewayError, match=r"^homedepot.com's data gateway returned HTTP 503$"):
            await com.fetch_product("204279858", timeout=5)
        assert com._hold_until == 0.0  # a gateway error is not an edge refusal


# ---------------------------------------------------------------------------
# homedepot.ca
# ---------------------------------------------------------------------------


def _state_html(state: dict) -> str:
    return f'<html><script id="hdca-state" type="application/json">{json.dumps(state)}</script></html>'


def test_not_found_skeleton_is_not_a_product():
    skeleton = {"product-1001398390": {"code": None, "name": None, "price": {"value": None}}}
    assert ca.state_product(ca.parse_state(_state_html(skeleton)), "1001398390") is None


def test_state_listing_needs_a_search_report():
    assert ca.state_listing({"aemContent-tools": {"aemContent": {}, "plpData": None}}) is None
    found = ca.state_listing({"aemContent-x": {"aemContent": {"navTitle": "X"}, "plpData": {"searchReport": {}}}})
    assert found[1]["navTitle"] == "X"


class TestCaPrice:
    def test_sale(self):
        line = ca_price_line(
            {
                "displayPrice": {"currencyIso": "CAD", "value": 1598.0, "unitOfMeasureCode": "EA", "unitOfMeasure": "each"},
                "wasprice": {"value": 1695.0},
                "savingsAmount": {"value": 97.0},
                "percentSaving": "6%",
            }
        )
        assert line == "$1,598.00 CAD (was $1,695.00, save $97.00 (6%))"

    def test_french_each_is_not_a_unit(self):
        line = ca_price_line({"displayPrice": {"value": 198.0, "unitOfMeasureCode": "EA", "unitOfMeasure": "chaque"}})
        assert line == "$198.00 CAD"

    def test_real_units_are_kept(self):
        line = ca_price_line({"displayPrice": {"value": 4.5, "unitOfMeasureCode": "FT", "unitOfMeasure": "linear foot"}})
        assert line == "$4.50 CAD /linear foot"

    def test_missing_price_is_no_online_price(self):
        assert ca_price_line({"productStatus": "OU"}) == "no online price"


CA_PRODUCT = {
    "code": "1000730797",
    "name": "1/2-inch D-Handle Drill",
    "manufacturer": "Milwaukee Tool",
    "modelNumber": "1001-1",
    "price": {"currencyIso": "CAD", "value": 299},
    "stock": {"stockLevelStatus": "outOfStock", "stockLevel": 6},
    "breadCrumbs": [
        {"categoryId": None, "title": "Home"},
        {"categoryId": "l1-tools", "title": "Tools"},
        {"categoryId": None, "title": "1000730797"},
    ],
    "classifications": [
        {
            "name": "Categories",
            "features": [
                {"name": "Assembled Weight (in lbs)", "featureUnit": {"symbol": "Pounds"}, "featureValues": [{"value": "1.58"}]},
                {"name": "Battery Voltage", "featureUnit": {"symbol": "V"}, "featureValues": [{"value": "12"}]},
            ],
        }
    ],
    "urls": {"fr": "https://www.homedepot.ca/produit/x/1000730797"},
}


class TestCaProduct:
    def test_status_ou_is_not_quoted_as_a_price(self):
        out = render_ca_product(
            CA_PRODUCT,
            "https://www.homedepot.ca/product/x/1000730797",
            localized={"optimizedPrice": {"productStatus": "OU"}},
        )
        assert "**Price:** no online price" in out
        assert "(status OU)" in out
        assert "$299.00 CAD as the catalogue price" in out

    def test_unreadable_price_service_is_said(self):
        out = render_ca_product(CA_PRODUCT, "https://www.homedepot.ca/product/x/1000730797", localized=None)
        assert "**Price:** $299.00 CAD" in out
        assert "any sale or was-price is not shown" in out

    def test_breadcrumb_drops_home_and_the_product_itself(self):
        out = render_ca_product(CA_PRODUCT, "https://www.homedepot.ca/product/x/1000730797", localized={})
        assert "**Category:** Tools\n" in out

    def test_units_are_not_stated_twice(self):
        out = render_ca_product(CA_PRODUCT, "https://www.homedepot.ca/product/x/1000730797", localized={})
        assert "- Assembled Weight (in lbs): 1.58\n" in out
        assert "- Battery Voltage: 12 V" in out

    def test_only_online_stock_is_reported(self):
        out = render_ca_product(
            CA_PRODUCT,
            "https://www.homedepot.ca/product/x/1000730797",
            localized={"stock": {"stockLevelStatus": "backOrder"}, "storeStock": {"stockLevelStatus": "outOfStock"}},
        )
        assert "**Availability:** backordered" in out


def test_seo_reviews_keep_title_and_author_apart():
    markup = """
    <div itemprop="review" itemscope>
      <span itemprop="reviewRating" itemscope>Rated <span itemprop="ratingValue">5</span></span>
      <span itemprop="author" itemscope><span itemprop="name">Ktauh</span></span>
      <span itemprop="name">Great all purpose house tool</span>
      <span itemprop="description">Does it all.</span>
      <meta itemprop="datePublished" content="2026-05-18" />
    </div>"""
    (review,) = ca_reviews_from_seo(markup)
    assert review == {
        "rating": "5",
        "title": "Great all purpose house tool",
        "body": "Does it all.",
        "author": "Ktauh",
        "date": "2026-05-18",
    }


def test_ca_listing_counts_pages_from_the_report():
    plp = {
        "searchReport": {"totalProducts": 58, "pageSize": 40, "startIndex": 2},
        "sorts": [{"code": "price-asc", "name": "Price - Low to High", "selected": True}],
        "facets": [
            {
                "name": "Brand Name",
                "facetValues": [
                    {"name": "Milwaukee Tool", "selected": True, "filterKey": "boj"},
                    {"name": "Bosch", "count": 44, "selected": False, "filterKey": "boj-9wh-oj2"},
                ],
            }
        ],
        "products": [{"code": "1000730797", "name": "D-Handle Drill", "brand": "Milwaukee Tool", "pricing": {}}],
    }
    out = render_ca_listing(plp, "https://www.homedepot.ca/en/home/categories/x.html?page=2", title="Drill/Drivers", is_search=False)
    assert "**58 products** · page 2 of 2 (40 per page) · sorted by Price - Low to High" in out
    assert "**Filters applied:** Brand Name: Milwaukee Tool" in out
    assert "41. **Milwaukee Tool** D-Handle Drill — no online price" in out
    assert "Bosch (44) [boj-9wh-oj2]" in out
    assert "Next page" not in out


def test_redirect_target_stays_on_site():
    assert ca.redirect_target({"searchReport": {"keywordRedirectUrl": "/en/home/categories/drills.html?searchterm=drill"}}) == (
        "https://www.homedepot.ca/en/home/categories/drills.html?searchterm=drill"
    )
    assert ca.redirect_target({"searchReport": {"keywordRedirectUrl": "https://evil.example/x"}}) is None


class _Resp:
    def __init__(self, status=200, text="", headers=None, payload=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}
        self._payload = payload

    def json(self):
        return self._payload


class TestCaSequence:
    async def test_service_calls_follow_a_page_and_carry_it_as_referer(self, monkeypatch):
        calls = []

        class _Session:
            async def get(self, url, headers=None, timeout=None):
                calls.append((url, (headers or {}).get("Referer")))
                if "/api/" in url:
                    return _Resp(payload=[{"productId": "1", "optimizedPrice": {}}])
                return _Resp(text="<html></html>")

        monkeypatch.setattr(ca, "get_session", lambda: _resolve(_Session()))
        page_url = "https://www.homedepot.ca/product/x/1"
        rows = await ca.fetch_localized(["1"], lang="en", page_url=page_url, timeout=5)
        assert rows == {"1": {"productId": "1", "optimizedPrice": {}}}
        assert calls[0] == (page_url, None)
        assert calls[1][1] == page_url

    async def test_a_challenge_reloads_the_page_and_retries_once(self, monkeypatch):
        attempts = {"api": 0}

        class _Session:
            async def get(self, url, headers=None, timeout=None):
                if "/api/" in url:
                    attempts["api"] += 1
                    if attempts["api"] == 1:
                        raise wafer.ChallengeDetected("akamai", url, 403)
                    return _Resp(payload=[])
                return _Resp(text="<html></html>")

        monkeypatch.setattr(ca, "get_session", lambda: _resolve(_Session()))
        assert await ca.fetch_localized(["1"], lang="en", page_url="https://www.homedepot.ca/p", timeout=5) == {}
        assert attempts["api"] == 2

    async def test_unanswered_price_service_is_none_not_empty(self, monkeypatch):
        class _Session:
            async def get(self, url, headers=None, timeout=None):
                if "/api/" in url:
                    raise wafer.ChallengeDetected("akamai", url, 403)
                return _Resp(text="<html></html>")

        monkeypatch.setattr(ca, "get_session", lambda: _resolve(_Session()))
        assert await ca.fetch_localized(["1"], lang="en", page_url="https://www.homedepot.ca/p", timeout=5) is None

    async def test_off_site_redirect_is_not_followed(self, monkeypatch):
        class _Session:
            async def get(self, url, headers=None, timeout=None):
                return _Resp(status=301, headers={"location": "http://169.254.169.254/latest"})

        monkeypatch.setattr(ca, "get_session", lambda: _resolve(_Session()))
        with pytest.raises(ca.CaReadError, match="off-site"):
            await ca.fetch_page_state("https://www.homedepot.ca/product/x/1", timeout=5)

    async def test_search_redirect_reads_the_category_with_the_callers_page(self, monkeypatch):
        seen = []
        category = {
            "aemContent-drills": {
                "aemContent": {"navTitle": "Drills"},
                "plpData": {"searchReport": {"totalProducts": 993, "pageSize": 40, "startIndex": 2}, "products": []},
            }
        }

        class _Session:
            async def get(self, url, headers=None, timeout=None):
                seen.append(url)
                if "/api/search/" in url:
                    return _Resp(payload={"searchReport": {"keywordRedirectUrl": "/en/home/categories/drills.html?searchterm=drill"}})
                return _Resp(text=_state_html(category))

        monkeypatch.setattr(ca, "get_session", lambda: _resolve(_Session()))
        result = await page.get_homedepot("https://www.homedepot.ca/search?q=drill&page=2", timeout=5)
        assert seen[-1] == "https://www.homedepot.ca/en/home/categories/drills.html?searchterm=drill&page=2"
        assert 'homedepot.ca sends the search "drill" to its Drills category' in result["content"]

    async def test_department_page_without_a_listing_falls_through(self, monkeypatch):
        class _Session:
            async def get(self, url, headers=None, timeout=None):
                return _Resp(text=_state_html({"nav-nodes-data": {}}))

        monkeypatch.setattr(ca, "get_session", lambda: _resolve(_Session()))
        assert await page.get_homedepot("https://www.homedepot.ca/en/home/categories/tools.html", timeout=5) is None


# ---------------------------------------------------------------------------
# Review fixes (2026-10-07)
# ---------------------------------------------------------------------------


def test_overflowing_numbers_in_the_url_are_ignored_not_raised():
    target = com_target("https://www.homedepot.com/b/Tools/N-5yc1vZc298?Nao=inf&lowerbound=1e999")
    assert target is not None
    assert target.start_index == 0 and target.lower_bound is None


def test_a_search_keyword_that_starts_like_a_nav_value_stays_a_keyword():
    target = com_target("https://www.homedepot.com/s/N-95%20mask")
    assert target is not None
    assert target.keyword == "N-95 mask"
    assert target.nav_param is None


def test_a_missing_upper_price_bound_is_left_out_not_sent_as_zero():
    target = com_target("https://www.homedepot.com/b/Tools-Power-Tools-Drills/N-5yc1vZc27f?lowerbound=100")
    variables = com._search_variables(target, "5yc1vZc27f", None)
    assert variables["filter"] == {"rangefilter": "price", "lowerBound": 100}
    target = com_target("https://www.homedepot.com/b/Tools-Power-Tools-Drills/N-5yc1vZc27f?upperbound=50")
    assert com._search_variables(target, "5yc1vZc27f", None)["filter"] == {
        "rangefilter": "price", "lowerBound": 0, "upperBound": 50,
    }


def test_a_listing_row_does_not_offer_pickup_from_an_empty_shelf():
    from fetchaller.homedepot.render import _com_row_availability

    def product(inventory):
        return {"fulfillment": {"fulfillmentOptions": [{"fulfillable": True, "services": [
            {"type": "bopis", "locations": [{"isAnchor": True, "inventory": inventory}]},
        ]}]}}

    assert _com_row_availability(product({"quantity": 0, "isInStock": False})) == "pickup (out of stock at store)"
    assert _com_row_availability(product({"quantity": 7, "isInStock": True})) == "pickup (7 at store)"


def test_shopper_text_cannot_become_markdown_structure_or_unsafe_links():
    from fetchaller.homedepot.render import _text

    out = _text(
        '<p># Great drill</p><p>&gt; quoted</p><a href="javascript:alert(1)">bad</a> '
        '<a href="/b/X/N-1">Shop (all)</a> <a href="https://x.example/a(b)">p]x</a>'
    )
    assert out == "\\# Great drill\n\\> quoted\nbad [Shop (all)](/b/X/N-1) [p\\]x](https://x.example/a%28b%29)"


def test_a_not_found_product_is_never_replaced_by_another_product_on_the_page():
    skeleton = {"code": None, "name": None}
    sibling = {"code": "1000000001", "name": "Some Other Drill"}
    state = {"product-1001918582": skeleton, "product-1000000001": sibling}
    assert ca.state_product(state, "1001918582") is None
    # No entry for the code at all: only an unambiguous single product stands in.
    assert ca.state_product({"product-1000000001": sibling}, "999") == sibling
    assert ca.state_product({"product-1": sibling, "product-2": {"code": "2"}}, "999") is None
