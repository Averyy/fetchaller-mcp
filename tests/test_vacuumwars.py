"""Unit tests for the Vacuum Wars content module.

Covers URL detection, the comparison-tool dataset extraction (which is the
only machine-readable copy of Vacuum Wars' lab results), variant collapsing,
and the leaderboard card de-duplication.
"""

import json

from bs4 import BeautifulSoup

from fetchaller.content.vacuumwars import (
    LEAN_URL,
    SELECTORS_LIST,
    extract_compare_products,
    has_dataset,
    is_compare_url,
    is_vacuumwars,
    postprocess_vacuumwars,
    route_compare_url,
    strip_vacuumwars_junk,
)

TESTED = {
    "id": 1,
    "name": "Dreame X60 Max Ultra Complete",
    "slug": "dreame_x60_max_ultra_complete",
    "brand": "Dreame",
    "price": 1614.99,
    "vacuum_wars_score_stars": "4.18",
    "vw_mop_score_stars": "3.24",
    "obstacle_avoidance_score_stars": "4.49",
    "overall_navigation_score_stars": "2.76",
    "overall_pet_score_stars": "4.91",
    "carpet_deep_clean_test": 89,
    "flattened_pet_hair_pickup_test_2_inches_5": 100,
    "hair_tangle_test_seven_inches": 0,
    "crevice_pickup_test": 1,
    "suction_test_kpa": 0.71,
    "airflow_test_cfm": 16,
    "sf_per_charge": 951,
    "official_suction_power": 35000,
    "official_battery_life": 180,
    "robot_height_inches": 3.13,
    "threshold_height_mm": 88,
    "internal_dustbin_capacity_ml": 235,
    "navigation_type": "VersaLift dToF Lidar",
    "mop_pad_type": "2 Spinning Pads",
    "self_emptying_bin": "1",
    "mop_self_washing": "1",
    "obstacle_avoidance": "1",
    "matter_compatible": "1",
}

# Same tested robot, different shell and a higher price.
VARIANT = dict(TESTED, id=2, name="Dreame X60 Max Ultra Complete (White)", price=1699.99)

# Listed, never put through the lab: specs only, no measurements at all.
UNTESTED = {
    "id": 3,
    "name": "Airrobo P30",
    "slug": "airrobo_p30",
    "brand": "Airrobo",
    "price": 148.19,
    "official_suction_power": 3000,
    "official_battery_life": 100,
    "robot_height_inches": 3.0,
    "threshold_height_mm": 15,
    "navigation_type": "Gyroscope",
    "self_emptying_bin": "0",
    "obstacle_avoidance": "0",
    "matter_compatible": "0",
}

# Two listings the dataset gives the same name and different hardware. The
# lab never touched either, so every measurement is null and a merge rule that
# looked only at measurements would fuse them into one row.
SAME_NAME_A = {
    "id": 4,
    "name": "OKP L1",
    "slug": "okp_l1",
    "brand": "OKP",
    "price": 91.82,
    "official_suction_power": 1400,
    "official_battery_life": 150,
    "obstacle_avoidance": "1",
}
SAME_NAME_B = dict(
    SAME_NAME_A,
    id=5,
    name="OKP L1 (White)",
    price=99.99,
    official_suction_power=4000,
    official_battery_life=180,
    obstacle_avoidance="0",
)

# Identical hardware, typed inconsistently by the site: 40% of the numeric
# spec fields arrive as an int on one listing and a string on the next.
TYPED_INT = dict(UNTESTED, id=6, name="Bagotte BL20", slug="bagotte_bl20_a")
TYPED_STR = dict(
    UNTESTED,
    id=7,
    name="Bagotte BL20 (Gray)",
    slug="bagotte_bl20_b",
    official_suction_power="3000",
    official_battery_life="100",
    robot_height_inches="3.0",
    threshold_height_mm="15",
)
TYPED_INT["name"] = "Bagotte BL20"
TYPED_INT["brand"] = TYPED_STR["brand"] = "Bagotte"

COMPARE_URL = "https://vacuumwars.com/compare/robot-vacuums/"

# The comparison tool's shell, dataset absent. The app's own root Alpine
# component is what tells it apart from an ordinary article.
_SHELL_HTML = (
    "<html><body><div x-data='fetchData'>"
    "<div>No products found.</div></div></body></html>"
)


def _compare_page(products) -> BeautifulSoup:
    html = (
        "<html><body><div class='vwf-wrapper'>No products found.</div>"
        "<script>window.vwProducts = " + json.dumps(products) + ";</script>"
        "</body></html>"
    )
    return BeautifulSoup(html, "html.parser")


def _render(products, url=COMPARE_URL) -> str:
    soup = _compare_page(products)
    extract_compare_products(soup, url)
    # Scripts are removed by CSS selectors before markdownify; the marker the
    # extractor injected is what survives into the markdown.
    return postprocess_vacuumwars(soup.get_text())


class TestUrlDetection:
    def test_bare_host(self):
        assert is_vacuumwars("https://vacuumwars.com/dreame-d30-ultra-review/")

    def test_www_host(self):
        assert is_vacuumwars("https://www.vacuumwars.com/")

    def test_other_host(self):
        assert not is_vacuumwars("https://example.com/vacuumwars.com/")

    def test_lookalike_host(self):
        assert not is_vacuumwars("https://notvacuumwars.com/compare/")

    def test_compare_path(self):
        assert is_compare_url(COMPARE_URL)

    def test_compare_path_no_slash(self):
        assert is_compare_url("https://vacuumwars.com/compare")

    def test_review_path_is_not_compare(self):
        assert not is_compare_url("https://vacuumwars.com/dreame-d30-ultra-review/")

    def test_lookalike_path_is_not_compare(self):
        assert not is_compare_url("https://vacuumwars.com/compare-robot-vacuums/")

    def test_missing_url(self):
        assert not is_compare_url(None)


class TestCompareExtraction:
    def test_renders_tested_robot_with_scores(self):
        out = _render([TESTED])
        assert "Dreame X60 Max Ultra Complete" in out
        assert "4.18" in out
        assert "$1,614.99" in out

    def test_reports_tested_and_untested_counts_separately(self):
        out = _render([TESTED, UNTESTED])
        assert "1 lab-tested" in out
        assert "1 listed with manufacturer specs only" in out

    def test_untested_robot_is_listed_but_not_ranked(self):
        out = _render([TESTED, UNTESTED])
        ranked = out.split("## Specifications")[0]
        assert "Airrobo P30" not in ranked
        assert "Airrobo P30" in out

    def test_untested_score_renders_as_dash_not_blank(self):
        # "never tested" and "scored nothing" must not look alike.
        out = _render([UNTESTED])
        spec_row = [ln for ln in out.splitlines() if "Airrobo P30" in ln][-1]
        assert "| - |" in spec_row

    def test_ranks_by_vacuum_wars_score(self):
        low = dict(TESTED, id=9, name="Slow Bot", slug="slow", vacuum_wars_score_stars="2.10")
        out = _render([low, TESTED])
        assert out.index("Dreame X60 Max Ultra Complete") < out.index("Slow Bot")

    def test_booleans_render_as_words(self):
        out = _render([TESTED])
        assert "yes" in out
        out2 = _render([UNTESTED])
        assert "no" in out2

    def test_category_comes_from_url(self):
        assert "robot vacuums" in _render([TESTED])

    def test_pipes_in_values_do_not_break_the_table(self):
        odd = dict(TESTED, id=4, navigation_type="dToF | Lidar")
        out = _render([odd])
        row = [ln for ln in out.splitlines() if ln.startswith("| Dreame")][-1]
        # One cell per column: the stray pipe must not add a column.
        assert row.count("|") == out.splitlines()[out.splitlines().index(row) - 1].count("|")


class TestVariantCollapsing:
    def test_colour_variants_collapse_to_one_row(self):
        out = _render([TESTED, VARIANT])
        ranked = [ln for ln in out.splitlines() if ln.startswith("| 1 |")]
        assert len(ranked) == 1
        assert "(2 listings)" in out

    def test_differing_prices_report_the_cheapest_as_from(self):
        out = _render([TESTED, VARIANT])
        assert "from $1,614.99" in out
        assert "$1,699.99" not in out.split("## Specifications")[0]

    def test_identical_prices_are_not_labelled_from(self):
        same = dict(VARIANT, price=1614.99)
        out = _render([TESTED, same])
        assert "from $" not in out

    def test_listing_total_still_counts_every_variant(self):
        out = _render([TESTED, VARIANT])
        assert "1 models (2 listings" in out

    def test_different_scores_do_not_collapse(self):
        rescored = dict(VARIANT, vacuum_wars_score_stars="3.10")
        out = _render([TESTED, rescored])
        assert "(2 listings)" not in out
        assert "2 models (2 listings" in out

    def test_untested_listings_do_not_collapse_on_name_alone(self):
        # Three quarters of the catalogue has no measurement at all, so a rule
        # keyed only on measurements is degenerate there and merges anything
        # sharing a name. These two are different hardware.
        out = _render([SAME_NAME_A, SAME_NAME_B])
        assert "2 models (2 listings" in out
        assert "(2 listings)" not in out

    def test_a_split_keeps_the_names_that_tell_the_rows_apart(self):
        out = _render([SAME_NAME_A, SAME_NAME_B])
        assert "OKP L1 (White)" in out

    def test_a_merge_still_shortens_to_the_base_name(self):
        out = _render([TESTED, VARIANT])
        assert "Dreame X60 Max Ultra Complete (2 listings)" in out
        assert "(White)" not in out

    def test_int_and_string_spellings_of_one_spec_still_merge(self):
        # The site types the same field both ways; comparing raw values would
        # split colour variants that are in fact identical.
        out = _render([TYPED_INT, TYPED_STR])
        assert "1 models (2 listings" in out

    def test_a_merged_row_never_hides_a_printed_spec(self):
        quieter = dict(VARIANT, official_suction_power=8000)
        out = _render([TESTED, quieter])
        assert "2 models (2 listings" in out
        assert "35,000" in out
        assert "8,000" in out


class TestCompareFailureIsReported:
    def test_missing_dataset_is_announced_not_rendered_as_empty(self):
        soup = BeautifulSoup(_SHELL_HTML, "html.parser")
        extract_compare_products(soup, COMPARE_URL)
        out = postprocess_vacuumwars(soup.get_text())
        assert "could not be read" in out

    def test_empty_state_text_alone_still_reports_the_failure(self):
        # Alpine's attribute is the primary signal; the empty state is the
        # backstop, so a markup change cannot turn a failed read back into a
        # board that looks merely empty.
        soup = BeautifulSoup(
            "<html><body><div>No products found.</div>"
            "<div>No brand found.</div></body></html>",
            "html.parser",
        )
        extract_compare_products(soup, COMPARE_URL)
        assert "could not be read" in postprocess_vacuumwars(soup.get_text())

    def test_ordinary_article_under_compare_is_left_alone(self):
        # vacuumwars.com/compare/ is not the tool -- it is a WordPress article
        # announcing it. Warning that the dataset "could not be read" there
        # invents a failure on a page that rendered perfectly.
        soup = BeautifulSoup(
            "<html><body><h1>Try RobotVacs.com</h1>"
            "<p>Our new comparison tool is live.</p></body></html>",
            "html.parser",
        )
        extract_compare_products(soup, "https://vacuumwars.com/compare/")
        assert soup.find(id="vacuumwars-compare-marker") is None
        assert "could not be read" not in postprocess_vacuumwars(soup.get_text())

    def test_review_page_is_never_replaced_by_the_dataset(self):
        soup = _compare_page([TESTED])
        extract_compare_products(soup, "https://vacuumwars.com/dreame-d30-ultra-review/")
        assert soup.find(id="vacuumwars-compare-marker") is None


class TestCardDeduplication:
    def test_collapsed_row_is_in_the_selector_list(self):
        assert ".vwx-row" in SELECTORS_LIST
        assert ".vwx-price-date" in SELECTORS_LIST
        assert ".vwx-chip-more" in SELECTORS_LIST

    def test_feature_chips_are_separated(self):
        soup = BeautifulSoup(
            "<div class='vwx-feat'>"
            "<span class='vwx-chip'>Obstacle avoidance</span>"
            "<span class='vwx-chip'>Pad mop</span>"
            "<span class='vwx-chip'>Self-emptying dock</span>"
            "</div>",
            "html.parser",
        )
        strip_vacuumwars_junk(soup)
        text = soup.get_text()
        assert "Obstacle avoidance; Pad mop; Self-emptying dock" == text
        assert not text.endswith("; ")

    def test_accordion_caret_button_is_dropped(self):
        soup = BeautifulSoup("<div><button>▼</button><p>Verdict</p></div>", "html.parser")
        strip_vacuumwars_junk(soup)
        assert "▼" not in soup.get_text()
        assert "Verdict" in soup.get_text()

    def test_real_buttons_survive(self):
        soup = BeautifulSoup("<div><button>See it at Amazon</button></div>", "html.parser")
        strip_vacuumwars_junk(soup)
        assert "See it at Amazon" in soup.get_text()


class TestPostprocess:
    def test_strips_affiliate_disclosure(self):
        md = (
            "# Review\n\nVacuum Wars is reader supported. When you make a purchase "
            "using links on our site we may earn a commission.\n[Details](x)\n\nBody\n"
        )
        out = postprocess_vacuumwars(md)
        assert "reader supported" not in out
        assert "Body" in out

    def test_strips_price_timestamp(self):
        md = "Price\n\n09/10/2026 06:49 pm GMT\n\nMore\n"
        out = postprocess_vacuumwars(md)
        assert "GMT" not in out
        assert "More" in out

    def test_strips_spa_empty_state(self):
        md = "Robot Vacuum Comparison\n\nNo products found.\n\nNo brand found\n\nEnd\n"
        out = postprocess_vacuumwars(md)
        assert "No products found" not in out
        assert "End" in out

    def test_keeps_ordinary_review_prose(self):
        md = "# Dreame D30 Ultra Review\n\nIt scored 84 in carpet deep clean.\n"
        out = postprocess_vacuumwars(md)
        assert "scored 84 in carpet deep clean" in out


class TestCompareFrontEndHost:
    """compare.vacuumwars.com serves the same app and the same dataset."""

    def test_host_is_recognised(self):
        assert is_vacuumwars("https://compare.vacuumwars.com/")

    def test_every_route_on_that_host_is_a_compare_page(self):
        # The tool is the whole site there, so path cannot decide.
        assert is_compare_url("https://compare.vacuumwars.com/")
        assert is_compare_url("https://compare.vacuumwars.com/embed/?product1=x")

    def test_dataset_renders_from_that_host(self):
        out = _render([TESTED], "https://compare.vacuumwars.com/")
        assert "Dreame X60 Max Ultra Complete" in out
        assert "4.18" in out

    def test_main_site_still_needs_the_compare_path(self):
        assert not is_compare_url("https://vacuumwars.com/dreame-d30-ultra-review/")


class TestVanityDomain:
    def test_robotvacs_is_recognised(self):
        # The site's own article calls the tool RobotVacs.com and links there,
        # so this is the URL a caller most likely arrives with. It has to be
        # recognised or the rate limiter, which keys off the URL as asked for,
        # never fires on the 2.6 MB render.
        assert is_vacuumwars("https://robotvacs.com/")
        assert is_vacuumwars("https://www.robotvacs.com/")

    def test_robotvacs_root_is_a_compare_path(self):
        # It 301s onto /compare/robot-vacuums/ today, so extraction normally
        # runs against the redirect target. This keeps the module working if
        # the redirect is ever dropped.
        assert is_compare_url("https://robotvacs.com/")


# A listing whose trailing parenthetical is a configuration, not a colour.
NO_DOCK = {
    "id": 8,
    "name": "Eufy L60 (No self-empty station)",
    "slug": "eufy_l60_no_dock",
    "brand": "Eufy",
    "price": 186.97,
    "flattened_pet_hair_pickup_test_2_inches_5": 86,
    "official_suction_power": 5000,
    "self_emptying_bin": "0",
}


class TestNamesKeepWhatTellsRowsApart:
    def test_a_lone_listing_keeps_its_parenthetical(self):
        # Nothing merged, so nothing earned the shortening. "Eufy L60" sits
        # beside "Eufy L60 with Self Empty Station" in the real catalogue, and
        # dropping the qualifier leaves the reader guessing which is which.
        out = _render([NO_DOCK])
        assert "Eufy L60 (No self-empty station)" in out

    def test_identical_names_are_not_shortened(self):
        twin = dict(NO_DOCK, id=9, slug="eufy_l60_no_dock_b")
        out = _render([NO_DOCK, twin])
        assert "Eufy L60 (No self-empty station) (2 listings)" in out

    def test_a_real_merge_still_shortens(self):
        out = _render([TESTED, VARIANT])
        assert "Dreame X60 Max Ultra Complete (2 listings)" in out


class TestRankMeansRank:
    def test_a_row_without_an_overall_score_is_not_numbered(self):
        # It has lab results, so it belongs in the tested table, but numbering
        # it would make its dataset position read as a placing.
        out = _render([TESTED, NO_DOCK])
        rows = [ln for ln in out.splitlines() if "Eufy L60" in ln]
        assert rows[0].startswith("| - |")

    def test_the_unranked_tail_is_declared(self):
        out = _render([TESTED, NO_DOCK])
        assert "no overall score" in out
        assert "Rows 1 to 1 are the ranking" in out

    def test_a_genuine_zero_still_ranks(self):
        # "scored zero" and "never scored" must not look alike.
        zeroed = dict(TESTED, id=10, name="Zero Bot", slug="zero",
                      vacuum_wars_score_stars=0)
        out = _render([TESTED, zeroed])
        row = [ln for ln in out.splitlines() if "Zero Bot" in ln][0]
        assert row.startswith("| 2 |")


class TestPriceHonesty:
    def test_an_unpriced_member_makes_the_row_say_from(self):
        # The row speaks for two listings and only one has a cached figure, so
        # a bare price would claim both cost it.
        unpriced = dict(VARIANT, price=None)
        out = _render([TESTED, unpriced])
        assert "from $1,614.99" in out


class TestMarkdownRoundTrip:
    def test_the_injected_block_survives_markdownify(self):
        # _render() above reads the marker straight off the soup, so the
        # marker regex, the blank-line sentinel and markdownify's escaping
        # have no coverage from it. This is the only test that runs the real
        # conversion path.
        from fetchaller.content.html import _html_to_markdown_sync

        html = (
            "<html><body><div x-data='fetchData'>No products found.</div>"
            "<script>window.vwProducts = " + json.dumps([TESTED, UNTESTED]) + ";"
            "</script></body></html>"
        )
        out = _html_to_markdown_sync(html, url=COMPARE_URL)
        if isinstance(out, tuple):
            out = out[0]
        assert "# Vacuum Wars comparison data: robot vacuums" in out
        # Blank lines restored, so the tables are not welded to the prose.
        assert "\n\n## Lab-tested" in out
        # Sentinels fully consumed, and markdownify did not escape the table.
        assert "__VACUUMWARS" in out is False or "__VACUUMWARS" not in out
        assert "\\_" not in out and "\\*" not in out
        assert "No products found" not in out
        rows = [ln for ln in out.splitlines() if ln.startswith("|")]
        assert all(ln.endswith("|") for ln in rows)


class TestShellDetectionIsNarrow:
    def test_a_generic_alpine_widget_is_not_the_app(self):
        # Most bindings on the real app page are generic disclosure widgets.
        # If the theme ever adopts Alpine for a menu, every article under
        # /compare/ would otherwise collect an invented failure warning.
        soup = BeautifulSoup(
            "<html><body><div x-data='{ open: false }'>"
            "<p>An article about robot vacuums.</p></div></body></html>",
            "html.parser",
        )
        extract_compare_products(soup, "https://vacuumwars.com/compare/")
        assert soup.find(id="vacuumwars-compare-marker") is None


class TestCompareRouting:
    """Only the URLs that are nothing but the tool may move hosts."""

    def test_the_wordpress_tool_url_routes(self):
        assert route_compare_url(COMPARE_URL) == LEAN_URL
        assert route_compare_url("https://vacuumwars.com/compare/robot-vacuums") == LEAN_URL
        assert route_compare_url("https://www.vacuumwars.com/compare/robot-vacuums/") == LEAN_URL

    def test_the_vanity_domain_routes(self):
        assert route_compare_url("https://robotvacs.com/") == LEAN_URL
        assert route_compare_url("https://www.robotvacs.com") == LEAN_URL

    def test_the_lean_host_is_already_there(self):
        assert route_compare_url(LEAN_URL) is None

    def test_settled_routes_do_not_move(self):
        # Each of these has behaviour that routing would break: a hard 404, a
        # reverse soft 404, an ordinary article, and a review page.
        for url in (
            "https://vacuumwars.com/compare/robot-vacuums/page/2/",
            "https://vacuumwars.com/compare/robot-vacuums/a-vs-b/",
            "https://vacuumwars.com/compare/",
            "https://vacuumwars.com/compare/cordless-vacuums/",
            "https://vacuumwars.com/dreame-d30-ultra-review/",
            "https://vacuumwars.com/",
        ):
            assert route_compare_url(url) is None, url

    def test_a_query_string_is_never_routed(self):
        # A query means the caller wanted something specific from that URL.
        assert route_compare_url(COMPARE_URL + "?brand=dreame") is None

    def test_lookalike_hosts_do_not_route(self):
        assert route_compare_url("https://notrobotvacs.com/") is None
        assert route_compare_url("https://evil.com/compare/robot-vacuums/") is None

    def test_missing_url(self):
        assert route_compare_url(None) is None


class TestDatasetDetection:
    def test_a_page_with_the_array_is_recognised(self):
        assert has_dataset("<script>window.vwProducts = [{}];</script>")

    def test_the_bare_shell_is_not(self):
        assert not has_dataset("<html><body>No products found.</body></html>")
        assert not has_dataset("")


class TestHeadingIsStableAcrossHosts:
    def test_the_lean_host_gets_the_same_heading_as_the_wordpress_path(self):
        # The lean host serves the tool at a path with no category in it, so
        # the <h1> fallback would title it "Robot Vacuum Comparison" there and
        # "robot vacuums" on the WordPress path. Every routed read would carry
        # that difference.
        assert "# Vacuum Wars comparison data: robot vacuums" in _render([TESTED])
        assert "# Vacuum Wars comparison data: robot vacuums" in _render([TESTED], url=LEAN_URL)
