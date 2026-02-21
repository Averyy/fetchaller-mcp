"""Unit tests for Molex content module.

Tests is_molex() URL detection, extract_molex_jsonld() for JSON-LD extraction,
and postprocess_molex() regex patterns.
"""

from bs4 import BeautifulSoup

from fetchaller.content.molex import extract_molex_jsonld, is_molex, postprocess_molex


class TestIsMolex:
    """URL detection for Molex domains."""

    def test_molex_com(self):
        assert is_molex("https://www.molex.com/en-us/products/part-detail/430250408")

    def test_molex_no_www(self):
        assert is_molex("https://molex.com/en-us/products/part-detail/430250408")

    def test_not_molex(self):
        assert not is_molex("https://example.com/molex")

    def test_not_fake_molex(self):
        assert not is_molex("https://notmolex.com/page")


class TestExtractMolexJsonld:
    """JSON-LD extraction from Molex product pages."""

    def test_extracts_product_with_specs(self):
        html = """<html><body>
        <script type="application/ld+json">
        {"@type": "Product",
         "name": "Micro-Fit 3.0 Receptacle Housing, Dual Row, 4 Circuits",
         "sku": "430250408", "mpn": "430250408",
         "brand": {"@type": "Brand", "name": "Molex"},
         "offers": {"@type": "Offer",
                    "availability": "https://schema.org/InStock"},
         "additionalProperty": [
           {"@type": "PropertyValue", "name": "Application", "value": "Power, Wire-to-Board"},
           {"@type": "PropertyValue", "name": "Circuits Maximum", "value": "4.0"},
           {"@type": "PropertyValue", "name": "Pitch Mating Interface", "value": "3.00mm"},
           {"@type": "PropertyValue", "name": "Temperature Range Operating", "value": "-40° to +105°C"}
         ]}
        </script>
        <p>Limited Information Available</p>
        </body></html>"""
        soup = BeautifulSoup(html, "lxml")
        extract_molex_jsonld(soup)
        marker = soup.find(id="molex-jsonld-marker")
        assert marker is not None
        text = marker.string
        assert "**Product:** Micro-Fit 3.0 Receptacle Housing" in text
        assert "**SKU:** 430250408" in text
        assert "**Brand:** Molex" in text
        assert "**Availability:** InStock" in text
        assert "- **Application:** Power, Wire-to-Board" in text
        assert "- **Circuits Maximum:** 4.0" in text
        assert "- **Pitch Mating Interface:** 3.00mm" in text
        assert "- **Temperature Range Operating:** -40° to +105°C" in text

    def test_skips_compliance_properties(self):
        html = """<html><body>
        <script type="application/ld+json">
        {"@type": "Product", "name": "Test Part", "sku": "123",
         "additionalProperty": [
           {"@type": "PropertyValue", "name": "Application", "value": "Power"},
           {"@type": "PropertyValue", "name": "Ro Hs Display Name", "value": "EU RoHS"},
           {"@type": "PropertyValue", "name": "Ro Hs Status", "value": "Compliant per EU 2015/863"},
           {"@type": "PropertyValue", "name": "Reach Display Name", "value": "REACH SVHC"},
           {"@type": "PropertyValue", "name": "Reach Status", "value": "Not Contained"}
         ]}
        </script>
        </body></html>"""
        soup = BeautifulSoup(html, "lxml")
        extract_molex_jsonld(soup)
        marker = soup.find(id="molex-jsonld-marker")
        assert marker is not None
        text = marker.string
        assert "Application" in text
        assert "Ro Hs" not in text
        assert "Reach" not in text

    def test_handles_price(self):
        html = """<html><body>
        <script type="application/ld+json">
        {"@type": "Product", "name": "Test",
         "offers": {"price": "1.50", "priceCurrency": "USD"}}
        </script>
        </body></html>"""
        soup = BeautifulSoup(html, "lxml")
        extract_molex_jsonld(soup)
        marker = soup.find(id="molex-jsonld-marker")
        assert marker is not None
        assert "**Price:** USD 1.50" in marker.string

    def test_includes_description_when_different_from_name(self):
        html = """<html><body>
        <script type="application/ld+json">
        {"@type": "Product",
         "name": "Micro-Fit 3.0",
         "description": "Receptacle Housing, Dual Row, 4 Circuits, UL 94V-0"}
        </script>
        </body></html>"""
        soup = BeautifulSoup(html, "lxml")
        extract_molex_jsonld(soup)
        marker = soup.find(id="molex-jsonld-marker")
        text = marker.string
        assert "**Description:** Receptacle Housing" in text

    def test_omits_description_when_same_as_name(self):
        html = """<html><body>
        <script type="application/ld+json">
        {"@type": "Product",
         "name": "Micro-Fit 3.0",
         "description": "Micro-Fit 3.0"}
        </script>
        </body></html>"""
        soup = BeautifulSoup(html, "lxml")
        extract_molex_jsonld(soup)
        marker = soup.find(id="molex-jsonld-marker")
        text = marker.string
        assert "**Description:**" not in text

    def test_ignores_non_product(self):
        html = """<html><body>
        <script type="application/ld+json">
        {"@type": "BreadcrumbList", "itemListElement": []}
        </script>
        </body></html>"""
        soup = BeautifulSoup(html, "lxml")
        extract_molex_jsonld(soup)
        marker = soup.find(id="molex-jsonld-marker")
        assert marker is None

    def test_handles_invalid_json(self):
        html = """<html><body>
        <script type="application/ld+json">not valid json</script>
        </body></html>"""
        soup = BeautifulSoup(html, "lxml")
        extract_molex_jsonld(soup)
        marker = soup.find(id="molex-jsonld-marker")
        assert marker is None


class TestPostprocessMolex:
    """Regex postprocessor patterns."""

    def test_removes_limited_information_message(self):
        md = "## Limited Information Available\nThis part is not formally published to our online part catalog. Limited information is available to display on molex.com.\nPart Number Found"
        result = postprocess_molex(md)
        assert "Limited Information" not in result
        assert "not formally published" not in result
        assert "Part Number Found" not in result

    def test_removes_nav_items(self):
        md = "Products & Services\nIndustries & Applications\nTrends & Insights\nAbout\nProduct content"
        result = postprocess_molex(md)
        assert "Products & Services" not in result
        assert "Industries & Applications" not in result
        assert "Trends & Insights" not in result
        assert "Product content" in result

    def test_removes_account_menu(self):
        md = "Account\nLogin\nRegister\n- Profile\n- Change Password\n- Sample History\nLog Out\nProduct content"
        result = postprocess_molex(md)
        assert "Account" not in result
        assert "Login" not in result
        assert "Register" not in result
        assert "Profile" not in result
        assert "Change Password" not in result
        assert "Sample History" not in result
        assert "Log Out" not in result
        assert "Product content" in result

    def test_removes_label_placeholder(self):
        md = "Some content\nLabel\nMore content"
        result = postprocess_molex(md)
        assert "\nLabel\n" not in result
        assert "Some content" in result
        assert "More content" in result

    def test_removes_skip_to_main_content(self):
        md = "[Skip to main content](#maincontent)\nProduct content"
        result = postprocess_molex(md)
        assert "Skip to main content" not in result
        assert "Product content" in result

    def test_jsonld_marker_placed_after_heading(self):
        md = "# Molex Part 430250408\n\n__MOLEX_JSONLD__**Product:** Micro-Fit 3.0\n**SKU:** 430250408__MOLEX_JSONLD__\n\nContent"
        result = postprocess_molex(md)
        assert "**Product:** Micro-Fit 3.0" in result
        assert "**SKU:** 430250408" in result
        assert "__MOLEX_JSONLD__" not in result
        heading_pos = result.index("# Molex Part")
        product_pos = result.index("**Product:** Micro-Fit")
        assert product_pos > heading_pos

    def test_removes_about_us_boilerplate(self):
        md = (
            "## About Us\n\n"
            "---\n\n"
            "Molex is a global electronics leader committed to making "
            "the world a better, more-connected place. With a presence "
            "in more than 38 countries, Molex enables transformative "
            "technology innovation in the consumer device, aerospace and "
            "defense, data center, cloud, telecommunications, transportation, "
            "industrial automation and healthcare industries. Through "
            "trusted customer and industry relationships, unrivaled "
            "engineering expertise, and product quality and reliability, "
            "Molex realizes the infinite potential of *Creating Connections for Life.*"
        )
        result = postprocess_molex(md)
        assert "global electronics leader" not in result

    def test_preserves_product_content(self):
        md = (
            "# Connector Part 430250408\n\n"
            "**Product:** Micro-Fit 3.0\n"
            "**Pitch:** 3.00mm\n"
        )
        result = postprocess_molex(md)
        assert "Connector Part 430250408" in result
        assert "Micro-Fit 3.0" in result
        assert "3.00mm" in result
