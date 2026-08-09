"""Tests for ui.com: store, tech specs, and installation guides.

Two invariants carry most of the weight here.

The store's HTML has a price on it but no specifications — those render from
``__NEXT_DATA__`` behind a "Technical" tab — so every assertion about a spec
value is an assertion that the payload was read rather than the markup.

Installation guides ship as JavaScript assets whose wording is outlined vector
art, and ``dl.ui.com`` answers 200 with an app shell for a slug that has no
guide. So "no guide here" must be distinguishable from "an empty guide", and
the pages have to survive a round trip through the parser to be reproducible.
"""

import pytest

from fetchaller.ubiquiti.api import (
    ROUTE_SPECS_PRODUCT,
    ROUTE_STORE_CATEGORY,
    ROUTE_STORE_COLLECTION_PRODUCT,
    ROUTE_STORE_PRODUCT,
    guide_slug,
    is_ubiquiti,
    is_ui_guide,
    is_ui_store,
    is_ui_techspecs,
    parse_next_data,
    store_category_products,
    store_product,
    techspecs_product,
)
from fetchaller.ubiquiti.guide import (
    GuideNotFoundError,
    asset_url,
    discover,
    page_svg,
    parse_asset,
    read_page,
)
from fetchaller.ubiquiti.manual import safe_slug
from fetchaller.ubiquiti.render import (
    availability,
    price_line,
    render_category,
    render_guide,
    render_product,
    render_techspecs_category,
    spec_badges,
    spec_sections,
)

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------


class TestUrlDetection:
    @pytest.mark.parametrize(
        "url",
        [
            "https://store.ui.com/us/en/category/wifi-wall",
            "https://ca.store.ui.com/ca/en/category/wifi-wall/products/u7-pro-wall",
            "https://eu.store.ui.com/eu/en/category/wifi-wall",
        ],
    )
    def test_store_hosts(self, url):
        assert is_ui_store(url)
        assert is_ubiquiti(url)

    def test_techspecs(self):
        assert is_ui_techspecs("https://techspecs.ui.com/unifi/wifi/u7-pro-wall")
        assert not is_ui_store("https://techspecs.ui.com/unifi/wifi")

    @pytest.mark.parametrize(
        "url",
        [
            "https://ui.com/qig/u7-pro-wall",
            "https://dl.ui.com/qig/u7-pro-wall/",
            "https://dl.ui.com/qig/u6-pro/#index",
        ],
    )
    def test_guides(self, url):
        assert is_ui_guide(url)
        assert is_ubiquiti(url)

    def test_guide_root_without_slug_is_not_a_guide(self):
        assert not is_ui_guide("https://dl.ui.com/qig/")

    def test_other_ui_pages_are_left_alone(self):
        # The blog, help centre and downloads portal render fine as HTML.
        assert not is_ubiquiti("https://ui.com/download/unifi")
        assert not is_ubiquiti("https://help.ui.com/hc/en-us/articles/123")

    def test_lookalike_domains_are_rejected(self):
        assert not is_ubiquiti("https://store.ui.com.evil.example/us/en/category/x")
        assert not is_ubiquiti("https://notui.com/qig/u7-pro-wall")
        assert not is_ui_store("https://store.ui.com.attacker.net/")

    def test_guide_slug(self):
        assert guide_slug("https://ui.com/qig/u7-pro-wall") == "u7-pro-wall"
        assert guide_slug("https://dl.ui.com/qig/u6-pro/#index") == "u6-pro"
        assert guide_slug("https://ca.store.ui.com/ca/en") is None


# ---------------------------------------------------------------------------
# __NEXT_DATA__ extraction
# ---------------------------------------------------------------------------


def _page(route, page_props):
    return {"page": route, "props": {"pageProps": page_props}}


class TestNextData:
    def test_parse_next_data(self):
        html = '<html><script id="__NEXT_DATA__" type="application/json">{"page":"/x"}</script></html>'
        assert parse_next_data(html) == {"page": "/x"}

    def test_parse_next_data_missing(self):
        assert parse_next_data("<html><body>nothing</body></html>") is None

    def test_parse_next_data_invalid_json(self):
        html = '<script id="__NEXT_DATA__" type="application/json">{oops</script>'
        assert parse_next_data(html) is None

    def test_store_product_prefers_the_url_s_product(self):
        """A shared payload lists siblings; position must not decide."""
        data = _page(
            ROUTE_STORE_PRODUCT,
            {
                "currentProductId": "b",
                "collection": {"products": [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}]},
            },
        )
        assert store_product(data)["name"] == "B"

    def test_store_product_falls_back_to_first(self):
        data = _page(
            ROUTE_STORE_PRODUCT,
            {"collection": {"products": [{"id": "a", "name": "A"}]}},
        )
        assert store_product(data)["name"] == "A"

    def test_store_product_absent(self):
        assert store_product(_page(ROUTE_STORE_PRODUCT, {})) is None

    def test_category_groups_skip_empty_subcategories(self):
        data = _page(
            ROUTE_STORE_CATEGORY,
            {
                "subCategories": [
                    {"id": "wifi-wall", "products": [{"name": "U7"}]},
                    {"id": "wifi-empty", "products": []},
                ]
            },
        )
        assert store_category_products(data) == [("wifi-wall", [{"name": "U7"}])]

    def test_techspecs_product(self):
        data = _page(ROUTE_SPECS_PRODUCT, {"product": {"name": "U7 Pro Wall"}})
        assert techspecs_product(data)["name"] == "U7 Pro Wall"

    def test_collection_routes_are_product_routes(self):
        """Door access and cameras file products under a collection."""
        data = _page(
            ROUTE_STORE_COLLECTION_PRODUCT,
            {
                "currentProductId": "x",
                "collection": {"products": [{"id": "x", "displaySku": "UA-G3-Pro"}]},
            },
        )
        assert store_product(data)["displaySku"] == "UA-G3-Pro"


class TestSoft404:
    """The store answers 200 and renders its home page for an unknown category."""

    @pytest.mark.asyncio
    async def test_unknown_category_is_reported_not_rendered(self, monkeypatch):
        from fetchaller.ubiquiti import api as ubiquiti_api

        shell = (
            '<script id="__NEXT_DATA__" type="application/json">'
            '{"page":"/[store]/[language]","props":{"pageProps":{"whatsNewCards":[]}}}</script>'
        )

        class _Response:
            status_code = 200
            text = shell
            url = "https://ca.store.ui.com/ca/en/category/all-cameras"

        class _Session:
            async def get(self, url, **kwargs):
                return _Response()

        monkeypatch.setattr(ubiquiti_api, "get_session", lambda: _future(_Session()))
        with pytest.raises(LookupError, match="no such category"):
            await ubiquiti_api.fetch_page("https://ca.store.ui.com/ca/en/category/all-cameras")

    @pytest.mark.asyncio
    async def test_the_real_store_home_page_still_falls_through(self, monkeypatch):
        """Only a URL that *claims* a category is a soft 404."""
        from fetchaller.ubiquiti import api as ubiquiti_api

        shell = (
            '<script id="__NEXT_DATA__" type="application/json">'
            '{"page":"/[store]/[language]","props":{"pageProps":{}}}</script>'
        )

        class _Response:
            status_code = 200
            text = shell
            url = "https://ca.store.ui.com/ca/en"

        class _Session:
            async def get(self, url, **kwargs):
                return _Response()

        monkeypatch.setattr(ubiquiti_api, "get_session", lambda: _future(_Session()))
        assert await ubiquiti_api.fetch_page("https://ca.store.ui.com/ca/en") is None


# ---------------------------------------------------------------------------
# Specification rendering
# ---------------------------------------------------------------------------


def _text(label, value, note=None, parent=None, feature_id=None):
    return {
        "__typename": "SpecificationEntitySectionFeatureEntryText",
        "value": value,
        "note": note,
        "feature": {"id": feature_id or label, "label": label, "parentId": parent, "note": None},
    }


def _flag(label, present, note=None):
    return {
        "__typename": "SpecificationEntitySectionFeatureEntryFlag",
        "flag": "True" if present else "Empty",
        "note": note,
        "feature": {"id": label, "label": label, "parentId": None, "note": None},
    }


def _group(label):
    return {
        "__typename": "SpecificationEntitySectionFeatureGroup",
        "feature": {"id": label, "label": label, "parentId": None, "note": None},
    }


def _spec(*sections):
    return {"sections": list(sections)}


def _section(label, features, kind="Standard"):
    return {"section": {"label": label, "type": kind}, "features": features}


class TestSpecSections:
    def test_text_and_note(self):
        out = spec_sections(_spec(_section("Overview", [_text("WiFi Standard", "WiFi 7", "With 6 GHz support")])))
        assert "### Overview" in out
        assert "- WiFi Standard: WiFi 7 (With 6 GHz support)" in out

    def test_flags_render_presence_and_absence(self):
        """Absent must be visible: 'no 6 GHz radio' differs from 'unstated'."""
        out = spec_sections(_spec(_section("Features", [_flag("Zero-Wait DFS", False), _flag("Band Steering", True)])))
        assert "- Zero-Wait DFS: —" in out
        assert "- Band Steering: ✓" in out

    def test_groups_nest_children_by_parent_id(self):
        out = spec_sections(
            _spec(
                _section(
                    "Performance",
                    [
                        _group("MIMO"),
                        _text("6 GHz", "2 x 2", parent="MIMO", feature_id="mimo-6"),
                        _text("Uplink", "2.5 GbE"),
                    ],
                )
            )
        )
        assert "- **MIMO**" in out
        assert "  - 6 GHz: 2 x 2" in out
        # A row that is not part of the group stays at the top level.
        assert "- Uplink: 2.5 GbE" in out

    def test_multiline_values_stay_readable(self):
        out = spec_sections(
            _spec(_section("Hardware", [_text("Operating Frequency", "US/CA:\n2400 - 2472 MHz\n5150 - 5250 MHz")]))
        )
        assert "- Operating Frequency:" in out
        assert "  US/CA:" in out
        assert "  2400 - 2472 MHz" in out

    def test_compare_grid_is_skipped_on_a_product(self):
        """The Overview-typed grid duplicates the visible Overview section."""
        spec = _spec(
            _section("Grid overview", [_text("WiFi7", "WiFi 7")], kind="Overview"),
            _section("Overview", [_text("Spatial Streams", "6")]),
        )
        out = spec_sections(spec)
        assert "### Grid overview" not in out
        assert "- Spatial Streams: 6" in out

    def test_badges_read_only_the_grid(self):
        spec = _spec(
            _section("Grid overview", [_text("WiFi7", "WiFi 7"), _flag("6 GHz Radio", True)], kind="Overview"),
            _section("Overview", [_text("Spatial Streams", "6")]),
        )
        assert spec_badges(spec) == ["WiFi 7", "6 GHz Radio"]

    def test_empty_spec_renders_nothing(self):
        assert spec_sections({}) == []
        assert spec_sections(None) == []


# ---------------------------------------------------------------------------
# Price and availability
# ---------------------------------------------------------------------------


class TestPriceLine:
    def test_surcharge_is_named_and_the_charged_price_leads(self):
        product = {
            "minDisplayPrice": {"amount": 26900, "currency": "CAD"},
            "minDisplayPriceWithSurcharges": {"amount": 29000, "currency": "CAD"},
            "productSurchargeTypes": ["Memory"],
        }
        assert price_line(product) == "290.00 CAD (base 269.00 CAD + Memory surcharge)"

    def test_no_surcharge_is_a_bare_price(self):
        product = {
            "minDisplayPrice": {"amount": 19900, "currency": "CAD"},
            "minDisplayPriceWithSurcharges": {"amount": 19900, "currency": "CAD"},
        }
        assert price_line(product) == "199.00 CAD"

    def test_sale_shows_the_former_price(self):
        product = {
            "minDisplayPriceWithSurcharges": {"amount": 15000, "currency": "USD"},
            "minDisplayRegularPrice": {"amount": 19900, "currency": "USD"},
        }
        assert "was 199.00 USD" in price_line(product)

    def test_zero_decimal_currency_is_not_divided_by_100(self):
        """jp.store.ui.com prices in whole yen: 36391 is ¥36,391, not ¥363.91."""
        product = {
            "minDisplayPrice": {"amount": 33727, "currency": "JPY"},
            "minDisplayPriceWithSurcharges": {"amount": 36391, "currency": "JPY"},
            "productSurchargeTypes": ["Memory"],
        }
        assert price_line(product) == "36,391 JPY (base 33,727 JPY + Memory surcharge)"

    def test_three_decimal_currency(self):
        product = {"minDisplayPriceWithSurcharges": {"amount": 123456, "currency": "KWD"}}
        assert price_line(product) == "123.456 KWD"

    def test_multi_variant_price_says_it_is_the_lowest(self):
        """minDisplayPrice is the minimum; bare, it reads as the price."""
        product = {
            "minDisplayPriceWithSurcharges": {"amount": 3900, "currency": "CAD"},
            "variants": [
                {"sku": "A", "displayPriceWithSurcharges": {"amount": 3900, "currency": "CAD"}},
                {"sku": "B", "displayPriceWithSurcharges": {"amount": 18900, "currency": "CAD"}},
            ],
        }
        assert price_line(product) == "from 39.00 CAD"

    def test_single_variant_price_is_not_hedged(self):
        product = {
            "minDisplayPriceWithSurcharges": {"amount": 3900, "currency": "CAD"},
            "variants": [{"sku": "A", "displayPriceWithSurcharges": {"amount": 3900, "currency": "CAD"}}],
        }
        assert price_line(product) == "39.00 CAD"

    def test_missing_price(self):
        assert price_line({}) == "Price not published"


class TestAvailability:
    def test_available(self):
        assert availability({"variants": [{"sku": "X", "status": "Available"}]}) == "Available"

    def test_sold_out_reports_dates(self):
        text = availability(
            {
                "variants": [
                    {"sku": "U6-Extender-US", "status": "SoldOut", "soldOutAt": "2026-08-08T00:00:00Z"}
                ]
            }
        )
        assert text == "SoldOut, since 2026-08-08, no restock date given"

    def test_restock_date_is_shown(self):
        text = availability({"variants": [{"sku": "A", "status": "SoldOut", "restockEtaAt": "2026-09-01T00:00:00Z"}]})
        assert "restock 2026-09-01" in text

    def test_multiple_variants_are_labelled(self):
        text = availability(
            {"variants": [{"sku": "A", "status": "Available"}, {"sku": "B", "status": "SoldOut"}]}
        )
        assert "A: Available" in text
        assert "B: SoldOut" in text

    def test_falls_back_to_product_status(self):
        assert availability({"status": "Available"}) == "Available"


# ---------------------------------------------------------------------------
# Page rendering
# ---------------------------------------------------------------------------


class TestRenderProduct:
    def _product(self):
        return {
            "title": "Access Point U7 Pro Wall",
            "displaySku": "U7-Pro-Wall",
            "family": "Unifi",
            "description": "<p>Wall-mounted WiFi 7 AP.</p><p><sub>Note. 6 GHz in some countries.</sub></p>",
            "keyFeatures": '<p><img src="x.svg"> WiFi 7 with 6 GHz support</p><p>6 spatial streams</p>',
            "minDisplayPrice": {"amount": 26900, "currency": "CAD"},
            "minDisplayPriceWithSurcharges": {"amount": 29000, "currency": "CAD"},
            "productSurchargeTypes": ["Memory"],
            "variants": [{"sku": "U7-Pro-Wall-US", "status": "Available"}],
            "technicalSpecification": _spec(_section("Overview", [_text("Spatial Streams", "6")])),
            "documents": [{"type": "InstallationGuide", "url": "https://ui.com/qig/u7-pro-wall"}],
        }

    def test_renders_price_specs_and_documents(self):
        out = render_product(self._product(), {"storeId": "ca"}, "https://ca.store.ui.com/x")
        assert "# Access Point U7 Pro Wall" in out
        assert "**SKU:** U7-Pro-Wall" in out
        assert "290.00 CAD (base 269.00 CAD + Memory surcharge)" in out
        assert "- Spatial Streams: 6" in out
        assert "- Installation Guide: https://ui.com/qig/u7-pro-wall" in out

    def test_description_html_is_flattened(self):
        out = render_product(self._product(), {}, "https://ca.store.ui.com/x")
        assert "<p>" not in out
        assert "Wall-mounted WiFi 7 AP." in out

    def test_key_features_lose_their_icons(self):
        out = render_product(self._product(), {}, "https://ca.store.ui.com/x")
        assert "- WiFi 7 with 6 GHz support" in out
        assert "x.svg" not in out

    def test_missing_specs_are_announced_not_hidden(self):
        product = self._product()
        product["technicalSpecification"] = {}
        out = render_product(product, {}, "https://ca.store.ui.com/x")
        assert "This page published none." in out


class TestRenderCategory:
    def _groups(self):
        return [
            (
                "wifi-wall",
                [
                    {
                        "shortTitle": "U7 Pro Wall",
                        "displaySku": "U7-Pro-Wall",
                        "slug": "u7-pro-wall",
                        "collectionSlug": "unifi-wifi-wall",
                        "shortDescription": "Wall-mounted WiFi 7 AP.",
                        "minDisplayPriceWithSurcharges": {"amount": 29000, "currency": "CAD"},
                        "variants": [{"sku": "A", "status": "Available"}],
                        "technicalSpecification": _spec(
                            _section("Grid", [_text("WiFi7", "WiFi 7"), _flag("6 GHz Radio", True)], kind="Overview")
                        ),
                    }
                ],
            ),
            ("wifi-outdoor", [{"name": "U7 Outdoor"}]),
        ]

    def test_renders_the_named_group_with_badges(self):
        out = render_category(
            self._groups(),
            {"queryParams": {"category": "wifi-wall"}},
            "https://ca.store.ui.com/ca/en/category/wifi-wall",
        )
        assert "# Ubiquiti Store — WiFi Wall" in out
        assert "1 product" in out
        assert "290.00 CAD" in out
        assert "WiFi 7, 6 GHz Radio" in out

    def test_sibling_groups_are_named_not_expanded(self):
        out = render_category(
            self._groups(),
            {"queryParams": {"category": "wifi-wall"}},
            "https://ca.store.ui.com/ca/en/category/wifi-wall",
        )
        assert "Sibling categories" in out
        assert "WiFi Outdoor (1)" in out
        assert "U7 Outdoor" not in out

    def test_techspecs_links_omit_the_stores_products_segment(self):
        """techspecs hangs a slug straight off its category; `products/` 404s."""
        out = render_techspecs_category(
            [("wifi-enterprise", [{"shortTitle": "E7 Audience", "name": "E7-Audience-US", "slug": "e7-audience-us"}])],
            "https://techspecs.ui.com/unifi/wifi",
        )
        assert "https://techspecs.ui.com/unifi/wifi/e7-audience-us" in out
        assert "/products/" not in out
        assert "# Ubiquiti Tech Specs — UniFi / WiFi" in out
        assert "## WiFi Enterprise (1)" in out

    def test_store_links_keep_the_products_segment(self):
        out = render_category(
            self._groups(),
            {"queryParams": {"category": "wifi-wall"}},
            "https://ca.store.ui.com/ca/en/category/wifi-wall",
        )
        assert "https://ca.store.ui.com/ca/en/category/wifi-wall/products/u7-pro-wall" in out

    def test_unknown_category_lists_everything(self):
        out = render_category(
            self._groups(),
            {"queryParams": {"category": "all-wifi"}},
            "https://ca.store.ui.com/ca/en/category/all-wifi",
        )
        assert "## WiFi Wall (1)" in out
        assert "## WiFi Outdoor (1)" in out


# ---------------------------------------------------------------------------
# Installation guides
# ---------------------------------------------------------------------------

_MODERN_SHELL = """
<html><head><title>U7 Pro Wall Installation Guide</title>
<meta name="description" content="How to install and set up U7 Pro Wall">
</head><body><h1 class="qig-title">U7 Pro Wall</h1>
<script>window.QIG_CONTEXT={ATTRIBUTE_KEY:{},STORE_NAME:"QIG_ASSETS",PACKAGE_VERSION:"0.4.0",
PAGE_TUPLES:[["index","index-e9pqUsSS.js"],["A","A-e9pqUsSS.js"]],INDEX_PAGE:"index"}</script>
</body></html>
"""

_LEGACY_SHELL = """
<html><head><style>[data-current-page=A] [data-page=A]{display:initial}
[data-current-page=B] [data-page=B]{display:initial}</style>
<title>U6 Professional Installation Guide</title></head>
<body><h1 class="qig-title">U6 Professional</h1>
<script src="runtime-33MUgxJ6.js"></script></body></html>
"""

_CATCHALL_SHELL = "<html><head></head><body><div id='__next'>app</div></body></html>"


class TestGuideDiscovery:
    def test_modern_runtime_declares_its_pages(self):
        found = discover(_MODERN_SHELL, "https://dl.ui.com/qig/u7-pro-wall/")
        assert found["title"] == "U7 Pro Wall"
        assert found["description"] == "How to install and set up U7 Pro Wall"
        assert found["pages"] == [("index", "index-e9pqUsSS.js"), ("A", "A-e9pqUsSS.js")]

    def test_legacy_runtime_is_recovered_from_css_and_build_hash(self):
        found = discover(_LEGACY_SHELL, "https://dl.ui.com/qig/u6-pro/")
        assert found["pages"] == [
            ("index", "index-33MUgxJ6.js"),
            ("A", "A-33MUgxJ6.js"),
            ("B", "B-33MUgxJ6.js"),
        ]

    def test_missing_guide_is_not_an_empty_guide(self):
        """dl.ui.com serves its app shell with HTTP 200 for an unknown slug."""
        with pytest.raises(GuideNotFoundError):
            discover(_CATCHALL_SHELL, "https://dl.ui.com/qig/nope/")

    def test_legacy_page_order_follows_the_stylesheet_not_the_alphabet(self):
        """`AA` sorts before `B`; the CSS order is the binding order."""
        shell = (
            "<html><head><style>[data-current-page=B] x{}[data-current-page=AA] y{}</style>"
            '<title>T</title></head><body><script src="runtime-h1.js"></script></body></html>'
        )
        assert [p for p, _f in discover(shell, "https://dl.ui.com/qig/x/")["pages"]] == ["index", "B", "AA"]

    def test_entities_in_a_title_are_decoded(self):
        shell = (
            "<html><head><title>U6 &amp; U7 &#8212; Guide</title>"
            '<script>window.QIG_CONTEXT={PAGE_TUPLES:[["index","i.js"]],INDEX_PAGE:"index"}</script>'
            "</head><body></body></html>"
        )
        assert discover(shell, "https://dl.ui.com/qig/x/")["title"] == "U6 & U7 — Guide"

    def test_base_gains_a_trailing_slash(self):
        found = discover(_MODERN_SHELL, "https://dl.ui.com/qig/u7-pro-wall")
        assert found["base"] == "https://dl.ui.com/qig/u7-pro-wall/"


class TestAssetUrl:
    def test_resolves_against_the_guide_directory(self):
        assert (
            asset_url("https://dl.ui.com/qig/u7-pro-wall/", "A-x.js")
            == "https://dl.ui.com/qig/u7-pro-wall/A-x.js"
        )

    def test_refuses_to_leave_ui_com(self):
        with pytest.raises(ValueError, match="off ui.com"):
            asset_url("https://dl.ui.com/qig/x/", "https://evil.example/a.js")


def _asset(payload):
    """Wrap a literal the way the guide bundler does."""
    return '!function(t,s,c){const l=t.STORE_NAME}({STORE_NAME:"QIG_ASSETS"},(function(t){return t}),' + payload + ")"


class TestAssetParsing:
    def test_bare_identifier_keys_are_accepted(self):
        """The literal is JS, not JSON: `viewBox:` has no quotes."""
        tree = parse_asset(_asset('["A",["svg",{viewBox:"0 0 10 10","xmlns:xlink":"u"},[]]]'))
        assert tree[0] == "A"
        assert tree[1][1] == {"viewBox": "0 0 10 10", "xmlns:xlink": "u"}

    def test_text_nodes_and_numbers(self):
        tree = parse_asset(_asset('["A",["g",{opacity:0.5},[[0,"hello"]]]]'))
        assert tree[1][1]["opacity"] == 0.5
        assert tree[1][2][0] == [0, "hello"]

    def test_string_escapes(self):
        tree = parse_asset(_asset('["A",["g",{d:"a\\"b\\u0041"},[]]]'))
        assert tree[1][1]["d"] == 'a"bA'

    def test_hex_escapes(self):
        tree = parse_asset(_asset('["A",["g",{d:"\\x41\\x42"},[]]]'))
        assert tree[1][1]["d"] == "AB"

    def test_surrogate_pair_becomes_one_encodable_character(self):
        tree = parse_asset(_asset('["A",["g",{d:"\\ud83d\\ude00"},[]]]'))
        value = tree[1][1]["d"]
        assert value == "\U0001f600"
        value.encode("utf-8")  # must not raise

    def test_unpaired_surrogate_is_dropped_not_kept(self):
        tree = parse_asset(_asset('["A",["g",{d:"a\\ud800b"},[]]]'))
        tree[1][1]["d"].encode("utf-8")  # would raise if the surrogate survived

    def test_unparsable_asset_returns_none(self):
        assert parse_asset("not an asset at all") is None


class TestReadPage:
    def test_links_are_extracted_without_parsing_path_data(self):
        js = _asset('["A",["a",{href:"https://help.ui.com/x"},[]]]')
        texts, links = read_page(js)
        assert texts == []
        assert links == ["https://help.ui.com/x"]

    def test_illustrator_id_suffix_is_stripped_from_links(self):
        """u6-pro ships the same video URL with a different export id per page."""
        js = _asset('["A",["a",{href:"https://youtube.com/watch?v=Oja_00000111178441968_"},[]]]')
        _texts, links = read_page(js)
        assert links == ["https://youtube.com/watch?v=Oja"]

    def test_quoted_attribute_keys_still_yield_links(self):
        """`xlink:href` is never a bare identifier, so it is always quoted."""
        js = _asset('["A",["a",{"xlink:href":"https://help.ui.com/y"},[]]]')
        _texts, links = read_page(js)
        assert links == ["https://help.ui.com/y"]

    def test_a_real_text_layer_is_read_when_present(self):
        js = _asset('["A",["svg",{},[["text",{},[[0,"Step 1"]]]]]]')
        texts, _links = read_page(js)
        assert texts == ["Step 1"]

    def test_stylesheets_are_not_mistaken_for_content(self):
        js = _asset('["A",["svg",{},[["style",{},[[0,".st0{fill:red}"]]],["text",{},[[0,"Step 1"]]]]]]')
        texts, _links = read_page(js)
        assert texts == ["Step 1"]


class TestPageSvg:
    def test_modern_wrapper(self):
        tree = parse_asset(_asset('["A",["div",{"data-page":"A"},[["svg",{viewBox:"0 0 1 1"},[]]]]]'))
        svg = page_svg(tree)
        assert svg.startswith('<?xml version="1.0" encoding="UTF-8"?><svg')
        assert 'viewBox="0 0 1 1"' in svg

    def test_legacy_top_level_svg(self):
        """Older guides register the <svg> directly, with no <div> wrapper."""
        tree = parse_asset(_asset('["index",["svg",{viewBox:"0 0 2 2"},[]]]'))
        assert 'viewBox="0 0 2 2"' in page_svg(tree)

    def test_class_styles_are_inlined(self):
        """MuPDF ignores CSS selectors; without inlining every shape renders black."""
        tree = parse_asset(
            _asset(
                '["A",["svg",{},[["style",{},[[0,".st0{fill:none;stroke:#006FFF}"]]],'
                '["path",{class:"st0",d:"M0 0"},[]]]]]'
            )
        )
        svg = page_svg(tree)
        assert 'style="fill:none;stroke:#006FFF;"' in svg

    def test_inline_style_wins_over_the_class(self):
        """Class declarations go first so the element's own style still overrides."""
        tree = parse_asset(
            _asset(
                '["A",["svg",{},[["style",{},[[0,".st0{fill:red}"]]],'
                '["path",{class:"st0",style:"fill:blue"},[]]]]]'
            )
        )
        svg = page_svg(tree)
        assert '<path class="st0" style="fill:red;fill:blue"/>' in svg

    def test_stylesheet_body_is_not_entity_escaped(self):
        tree = parse_asset(_asset('["A",["svg",{},[["style",{},[[0,".a > .b{fill:red}"]]]]]]'))
        assert ".a > .b{fill:red}" in page_svg(tree)

    def test_attribute_values_are_escaped(self):
        tree = parse_asset(_asset('["A",["svg",{title:"a<b&c"},[]]]'))
        assert 'title="a&lt;b&amp;c"' in page_svg(tree)

    def test_non_svg_payload(self):
        assert page_svg(["A", ["div", {}, []]]) is None
        assert page_svg(None) is None


class TestPaintReferences:
    """MuPDF implements no gradients: every `fill:url(#…)` would paint black."""

    def test_gradient_becomes_its_average_stop_colour(self):
        tree = parse_asset(
            _asset(
                '["A",["svg",{},[["defs",{},[["linearGradient",{id:"g"},['
                '["stop",{style:"stop-color:#000000"},[]],["stop",{style:"stop-color:#FFFFFF"},[]]]]]],'
                '["circle",{style:"fill:url(#g)"},[]]]]]'
            )
        )
        svg = page_svg(tree)
        assert "fill:#7F7F7F" in svg
        assert "fill:url(#g)" not in svg

    def test_stop_color_as_an_attribute(self):
        tree = parse_asset(
            _asset(
                '["A",["svg",{},[["linearGradient",{id:"g"},['
                '["stop",{"stop-color":"#FF0000"},[]]]],["path",{fill:"url(#g)"},[]]]]]'
            )
        )
        assert 'fill="#FF0000"' in page_svg(tree)

    def test_href_inheritance_is_followed(self):
        """Illustrator re-points at one gradient, leaving the referrer stopless."""
        tree = parse_asset(
            _asset(
                '["A",["svg",{},[["linearGradient",{id:"base"},[["stop",{style:"stop-color:#204060"},[]]]],'
                '["linearGradient",{id:"g","xlink:href":"#base"},[]],'
                '["circle",{style:"fill:url(#g)"},[]]]]]'
            )
        )
        assert "fill:#204060" in page_svg(tree)

    def test_dangling_reference_paints_none_per_spec(self):
        """These guides reference ids that exist nowhere; SVG 1.1 says `none`."""
        tree = parse_asset(_asset('["A",["svg",{},[["circle",{style:"fill:url(#ghost)"},[]]]]]'))
        svg = page_svg(tree)
        assert "fill:none" in svg
        assert "url(#ghost)" not in svg

    def test_defined_but_unsupported_paint_is_left_alone(self):
        """A real <pattern> is not ours to erase, even if MuPDF ignores it."""
        tree = parse_asset(
            _asset('["A",["svg",{},[["pattern",{id:"p"},[]],["circle",{style:"fill:url(#p)"},[]]]]]')
        )
        assert "fill:url(#p)" in page_svg(tree)

    def test_structural_references_are_never_rewritten(self):
        """clip-path/mask point at geometry, not paint."""
        tree = parse_asset(
            _asset('["A",["svg",{},[["circle",{style:"clip-path:url(#ghost);fill:url(#ghost)"},[]]]]]')
        )
        svg = page_svg(tree)
        assert "clip-path:url(#ghost)" in svg
        assert "fill:none" in svg

    def test_gradients_referenced_from_a_class_rule_are_flattened(self):
        tree = parse_asset(
            _asset(
                '["A",["svg",{},[["style",{},[[0,".st0{fill:url(#g)}"]]],'
                '["linearGradient",{id:"g"},[["stop",{style:"stop-color:#112233"},[]]]],'
                '["path",{class:"st0"},[]]]]]'
            )
        )
        svg = page_svg(tree)
        assert "fill:#112233" in svg
        assert "url(#g)" not in svg


class TestRenderGuide:
    def _data(self, texts=()):
        return {
            "title": "U7 Pro Wall",
            "description": "How to install and set up U7 Pro Wall",
            "source": "https://dl.ui.com/qig/u7-pro-wall/",
            "dropped_pages": 0,
            "links": ["https://help.ui.com/hc/en-us/categories/123"],
            "pages": [
                {"id": "index", "svg": "<svg/>", "texts": list(texts), "links": []},
                {"id": "A", "svg": "<svg/>", "texts": [], "links": []},
            ],
        }

    def test_states_plainly_that_there_is_no_text(self):
        out = render_guide(self._data())
        assert "no machine-readable text" in out
        assert "2 pages: index, A" in out
        assert "https://help.ui.com/hc/en-us/categories/123" in out

    def test_a_text_layer_is_shown_when_it_exists(self):
        out = render_guide(self._data(texts=["Step 1"]))
        assert "- Step 1" in out
        assert "no machine-readable text" not in out

    def test_unreconstructed_pages_are_reported(self):
        data = self._data()
        data["pages"][1]["svg"] = None
        out = render_guide(data)
        assert "1 page(s) could not be reconstructed: A" in out


class TestFetchGuideResilience:
    """One unreachable asset must cost that page, never the whole guide."""

    @pytest.mark.asyncio
    async def test_a_failing_asset_does_not_cancel_its_siblings(self, monkeypatch):
        import wafer

        from fetchaller.ubiquiti import manual

        good = _asset('["A",["svg",{viewBox:"0 0 1 1"},[]]]')

        class _Response:
            def __init__(self, text, status=200):
                self.text = text
                self.status_code = status
                self.url = "https://dl.ui.com/qig/x/"

        class _Session:
            async def get(self, url, **kwargs):
                if url.endswith("/"):
                    return _Response(_MODERN_SHELL)
                if url.endswith("A-e9pqUsSS.js"):
                    raise wafer.WaferError("transport blew up")
                return _Response(good)

        monkeypatch.setattr(manual.api, "get_session", lambda: _future(_Session()))
        data = await manual.fetch_guide("https://dl.ui.com/qig/x/")
        by_id = {p["id"]: p for p in data["pages"]}
        assert by_id["index"]["svg"] is not None
        assert by_id["A"]["svg"] is None
        assert by_id["A"]["error"] == "WaferError"

    @pytest.mark.asyncio
    async def test_transport_failure_is_reported_not_raised(self, monkeypatch):
        import wafer

        from fetchaller.ubiquiti import manual

        class _Session:
            async def get(self, url, **kwargs):
                raise wafer.WaferError("nope")

        monkeypatch.setattr(manual.api, "get_session", lambda: _future(_Session()))
        result = await manual.download_manual("https://ui.com/qig/u7-pro-wall")
        assert "error" in result
        assert "WaferError" in result["error"]


def _future(value):
    import asyncio

    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    fut.set_result(value)
    return fut


class TestSafeSlug:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("u7-pro-wall", "u7-pro-wall"),
            ("../../etc/passwd", "etc-passwd"),
            ("U7 Pro Wall", "u7-pro-wall"),
            ("", "guide"),
            ("///", "guide"),
        ],
    )
    def test_never_escapes_its_directory(self, value, expected):
        result = safe_slug(value)
        assert result == expected
        assert "/" not in result and ".." not in result
