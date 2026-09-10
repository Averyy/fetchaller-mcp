"""Unit tests for the Vacuum Wars content module.

Covers URL detection, the comparison-tool dataset extraction (which is the
only machine-readable copy of Vacuum Wars' lab results), variant collapsing,
and the leaderboard card de-duplication.
"""

import json

from bs4 import BeautifulSoup

from fetchaller.content.vacuumwars import (
    SELECTORS_LIST,
    extract_compare_products,
    is_compare_url,
    is_vacuumwars,
    postprocess_vacuumwars,
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

COMPARE_URL = "https://vacuumwars.com/compare/robot-vacuums/"


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


class TestCompareFailureIsReported:
    def test_missing_dataset_is_announced_not_rendered_as_empty(self):
        soup = BeautifulSoup(
            "<html><body><div>No products found.</div></body></html>", "html.parser"
        )
        extract_compare_products(soup, COMPARE_URL)
        out = postprocess_vacuumwars(soup.get_text())
        assert "could not be read" in out

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
