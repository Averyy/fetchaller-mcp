"""Unit tests for Mouser regex postprocessor.

Tests postprocess_mouser() in isolation to verify each pattern works correctly,
and is_mouser() for URL detection.
"""

from fetchaller.content.mouser import is_mouser, postprocess_mouser


class TestIsMouser:
    """URL detection for Mouser domains."""

    def test_mouser_com(self):
        assert is_mouser("https://www.mouser.com/ProductDetail/12345")

    def test_mouser_ca(self):
        assert is_mouser("https://www.mouser.ca/ProductDetail/12345")

    def test_mouser_co_uk(self):
        assert is_mouser("https://www.mouser.co.uk/ProductDetail/12345")

    def test_mouser_de(self):
        assert is_mouser("https://www.mouser.de/ProductDetail/12345")

    def test_mouser_no_www(self):
        assert is_mouser("https://mouser.com/ProductDetail/12345")

    def test_mouser_jp(self):
        assert is_mouser("https://www.mouser.jp/ProductDetail/12345")

    def test_not_mouser(self):
        assert not is_mouser("https://example.com/mouser")

    def test_not_fake_mouser(self):
        assert not is_mouser("https://notmouser.com/page")


class TestPostprocessMouser:
    """Regex postprocessor patterns."""

    def test_removes_sign_in(self):
        md = "Sign In\nCreate Account\nProduct info"
        result = postprocess_mouser(md)
        assert "Sign In" not in result
        assert "Create Account" not in result
        assert "Product info" in result

    def test_removes_add_to_cart(self):
        md = "Part details\nAdd to Cart\nAdd to BOM\nMore info"
        result = postprocess_mouser(md)
        assert "Add to Cart" not in result
        assert "Add to BOM" not in result
        assert "Part details" in result

    def test_removes_compare_buttons(self):
        md = "Part info\nCompare\nAdd to Compare\nSpecs"
        result = postprocess_mouser(md)
        assert "\nCompare\n" not in result
        assert "Add to Compare" not in result
        assert "Specs" in result

    def test_removes_filter_buttons(self):
        md = "Results\nApply Filters\nClear Filters\nParts list"
        result = postprocess_mouser(md)
        assert "Apply Filters" not in result
        assert "Clear Filters" not in result
        assert "Parts list" in result

    def test_removes_eda_download(self):
        md = "Part info\nEDA / CAD Models\nDownload\nSpecs"
        result = postprocess_mouser(md)
        assert "EDA / CAD Models" not in result
        assert "\nDownload\n" not in result
        assert "Specs" in result

    def test_removes_free_shipping(self):
        md = "Free shipping on orders over $50\nProduct details"
        result = postprocess_mouser(md)
        assert "Free shipping" not in result
        assert "Product details" in result

    def test_preserves_part_details(self):
        md = (
            "# ESP32-S3-WROOM-1\n\n"
            "Wi-Fi + Bluetooth LE SoC Module\n\n"
            "| Parameter | Value |\n"
            "| --- | --- |\n"
            "| Package | Module |\n"
            "| Price | $3.20 |"
        )
        result = postprocess_mouser(md)
        assert "ESP32-S3-WROOM-1" in result
        assert "Wi-Fi + Bluetooth" in result
        assert "Module" in result
        assert "$3.20" in result
