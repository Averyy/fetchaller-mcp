"""Unit tests for DigiKey regex postprocessor.

Tests postprocess_digikey() in isolation to verify each pattern works correctly,
and is_digikey() for URL detection.
"""

from fetchaller.content.digikey import is_digikey, postprocess_digikey


class TestIsDigikey:
    """URL detection for DigiKey domains."""

    def test_digikey_com(self):
        assert is_digikey("https://www.digikey.com/en/products/detail/part/12345")

    def test_digikey_ca(self):
        assert is_digikey("https://www.digikey.ca/en/products/detail/part/12345")

    def test_digikey_co_uk(self):
        assert is_digikey("https://www.digikey.co.uk/en/products/detail/part/12345")

    def test_digikey_de(self):
        assert is_digikey("https://www.digikey.de/en/products/detail/part/12345")

    def test_digikey_no_www(self):
        assert is_digikey("https://digikey.com/en/products/detail/part/12345")

    def test_digikey_jp(self):
        assert is_digikey("https://www.digikey.jp/en/products/detail/part/12345")

    def test_not_digikey(self):
        assert not is_digikey("https://example.com/digikey")

    def test_not_fake_digikey(self):
        assert not is_digikey("https://notdigikey.com/page")


class TestPostprocessDigikey:
    """Regex postprocessor patterns."""

    def test_removes_sign_in(self):
        md = "Sign In\nCreate Account\nProduct info"
        result = postprocess_digikey(md)
        assert "Sign In" not in result
        assert "Create Account" not in result
        assert "Product info" in result

    def test_removes_add_to_cart(self):
        md = "Part details\nAdd to Cart\nAdd to Order\nMore info"
        result = postprocess_digikey(md)
        assert "Add to Cart" not in result
        assert "Add to Order" not in result
        assert "Part details" in result

    def test_removes_compare_buttons(self):
        md = "Part info\nCompare\nAdd to Compare\nSpecs"
        result = postprocess_digikey(md)
        assert "\nCompare\n" not in result
        assert "Add to Compare" not in result
        assert "Specs" in result

    def test_removes_filter_buttons(self):
        md = "Results\nApply Filters\nClear Filters\nParts list"
        result = postprocess_digikey(md)
        assert "Apply Filters" not in result
        assert "Clear Filters" not in result
        assert "Parts list" in result

    def test_removes_contact_us(self):
        md = "Part info\nContact Us\nRequest Quote\nMore"
        result = postprocess_digikey(md)
        assert "Contact Us" not in result
        assert "Request Quote" not in result
        assert "Part info" in result

    def test_preserves_part_details(self):
        md = (
            "# STM32F103C8T6\n\n"
            "ARM Cortex-M3 72MHz 64KB Flash\n\n"
            "| Parameter | Value |\n"
            "| --- | --- |\n"
            "| Package | LQFP-48 |\n"
            "| Price | $2.50 |"
        )
        result = postprocess_digikey(md)
        assert "STM32F103C8T6" in result
        assert "ARM Cortex-M3" in result
        assert "LQFP-48" in result
        assert "$2.50" in result
