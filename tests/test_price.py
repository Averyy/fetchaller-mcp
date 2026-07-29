"""Contract tests for the strict price validator and the smoke-test price gate.

``has_positive_price`` guards every product renderer against reporting prose,
placeholders, or non-positive values as a price. The smoke gate reuses it on
composed display lines, so both directions are pinned here: real prices must be
accepted in the exact shapes the renderers emit, and prose with digits must
never pass.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from fetchaller.content._price import has_positive_price

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from smoke_test import _has_real_price, _price_line_parts  # noqa: E402


class TestHasPositivePrice:
    @pytest.mark.parametrize(
        "value",
        [
            "$4.81",
            "US $4.81",
            "C$ 6.91",
            "AU $12",
            "€9,99",
            "£1,234.50",
            "USD 3.00",
            "$2.50-3.00",
            "$1.20 per piece",
            "from $5.00",
            "$0.99 / 10 pieces".replace(" / ", " per "),
        ],
    )
    def test_accepts_currency_marked_positive_prices(self, value):
        assert has_positive_price(value, require_currency=True) is True

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "   ",
            "Contact supplier",
            "Minimum order 100 pieces",
            "100 pieces available",
            "Negotiable",
            "$0",
            "$0.00",
            "-$5.00",
            "$-5.00",
            "4.81",  # no currency marker
            "$1e5",  # exponent notation is not a rendered price
            "$5.00 shipping not included",  # prose word outside the allowlist
        ],
    )
    def test_rejects_prose_placeholders_and_nonpositive(self, value):
        assert has_positive_price(value, require_currency=True) is False

    def test_rejects_oversized_input(self):
        assert has_positive_price("$1" + " " * 300, require_currency=True) is False

    @pytest.mark.parametrize("value", [True, False])
    def test_rejects_booleans(self, value):
        assert has_positive_price(value, require_currency=False) is False

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (4.81, True),
            (1, True),
            (0, False),
            (-1, False),
            (float("inf"), False),
            (float("nan"), False),
            ("4.81", True),
            ("0", False),
        ],
    )
    def test_raw_numeric_fields_without_currency(self, value, expected):
        assert has_positive_price(value, require_currency=False) is expected

    def test_raw_numbers_still_rejected_when_currency_required(self):
        assert has_positive_price(4.81, require_currency=True) is False
        assert has_positive_price(5, require_currency=True) is False


class TestSmokeGatePriceLines:
    """The gate sees composed lines, not single fields (see `_price_line_parts`)."""

    @pytest.mark.parametrize(
        ("line", "prefix"),
        [
            # aliexpress/product.py and content/aliexpress.py sale composition
            ("Price: $4.81 (was $12.14)", "Price:"),
            ("Price: $4.81 (was $12.14) -60%", "Price:"),
            ("   Price: $4.81 (was $12.14)", "Price:"),
            ("Price: $4.81", "Price:"),
            # alibaba/product.py per-unit composition
            ("**Price:** US $2.50-3.00 per pieces", "**Price:**"),
            # facebook_marketplace/listing.py strikethrough composition
            ("**Price:** $40 ~~$60~~", "**Price:**"),
            # kijiji/api.py reduced-price composition
            ("**Price:** C$ 6.91 (was $9.00)", "**Price:**"),
        ],
    )
    def test_accepts_every_composed_render_format(self, line, prefix):
        assert _has_real_price(line, prefix) is True

    @pytest.mark.parametrize(
        "line",
        [
            "Price: Minimum order 100 pieces",
            "Price: Contact supplier",
            "Price: 100 pieces available",
            "Price: $0.00",
            "Price: -$5.00",
            "Price: (was $12.14)".replace("(was $12.14)", "see listing"),
        ],
    )
    def test_still_rejects_prose_and_nonpositive_lines(self, line):
        assert _has_real_price(line, "Price:") is False

    def test_discount_only_line_is_not_a_price(self):
        assert _has_real_price("Price: -60%", "Price:") is False

    def test_parts_recover_both_sale_and_original(self):
        parts = _price_line_parts("$4.81 (was $12.14) -60%")
        assert "$12.14" in parts
        assert "$4.81" in parts

    def test_gate_scans_every_matching_line(self):
        text = "Price: Contact supplier\nPrice: $4.81 (was $12.14)\n"
        assert _has_real_price(text, "Price:") is True
