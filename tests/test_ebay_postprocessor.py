"""Unit tests for eBay content module.

Tests postprocess_ebay() regex patterns, is_ebay() URL detection,
and extract_ebay_jsonld() for JSON-LD extraction.
"""

from bs4 import BeautifulSoup

from fetchaller.content.ebay import extract_ebay_jsonld, is_ebay, postprocess_ebay


class TestIsEbay:
    """URL detection for eBay domains."""

    def test_ebay_com(self):
        assert is_ebay("https://www.ebay.com/itm/123456789")

    def test_ebay_ca(self):
        assert is_ebay("https://www.ebay.ca/itm/123456789")

    def test_ebay_co_uk(self):
        assert is_ebay("https://www.ebay.co.uk/itm/123456789")

    def test_ebay_de(self):
        assert is_ebay("https://www.ebay.de/itm/123456789")

    def test_ebay_com_au(self):
        assert is_ebay("https://www.ebay.com.au/itm/123456789")

    def test_ebay_fr(self):
        assert is_ebay("https://www.ebay.fr/itm/123456789")

    def test_ebay_no_www(self):
        assert is_ebay("https://ebay.com/itm/123456789")

    def test_not_ebay(self):
        assert not is_ebay("https://example.com/ebay")

    def test_not_fake_ebay(self):
        assert not is_ebay("https://notebay.com/page")


class TestExtractEbayJsonld:
    """JSON-LD extraction from eBay product pages."""

    def test_extracts_product_data(self):
        html = """<html><body>
        <script type="application/ld+json">
        {"@type": "Product", "name": "Test Widget",
         "brand": {"@type": "Brand", "name": "Acme"},
         "offers": {"@type": "Offer", "price": "29.99",
                    "priceCurrency": "USD",
                    "itemCondition": "https://schema.org/NewCondition",
                    "availability": "https://schema.org/InStock",
                    "seller": {"@type": "Organization", "name": "WidgetStore"}}}
        </script>
        <h1>Test Widget</h1>
        </body></html>"""
        soup = BeautifulSoup(html, "lxml")
        extract_ebay_jsonld(soup)
        marker = soup.find(id="ebay-jsonld-marker")
        assert marker is not None
        text = marker.string
        assert "**Brand:** Acme" in text
        assert "**Price:** USD 29.99" in text
        assert "**Condition:** New" in text
        assert "**Availability:** InStock" in text
        assert "**Seller:** WidgetStore" in text

    def test_handles_array_jsonld(self):
        html = """<html><body>
        <script type="application/ld+json">
        [{"@type": "BreadcrumbList"}, {"@type": "Product", "brand": "TestBrand",
          "offers": {"price": "10.00", "priceCurrency": "CAD"}}]
        </script>
        </body></html>"""
        soup = BeautifulSoup(html, "lxml")
        extract_ebay_jsonld(soup)
        marker = soup.find(id="ebay-jsonld-marker")
        assert marker is not None
        assert "**Brand:** TestBrand" in marker.string
        assert "**Price:** CAD 10.00" in marker.string

    def test_ignores_non_product_jsonld(self):
        html = """<html><body>
        <script type="application/ld+json">
        {"@type": "BreadcrumbList", "itemListElement": []}
        </script>
        </body></html>"""
        soup = BeautifulSoup(html, "lxml")
        extract_ebay_jsonld(soup)
        marker = soup.find(id="ebay-jsonld-marker")
        assert marker is None

    def test_handles_invalid_json(self):
        html = """<html><body>
        <script type="application/ld+json">not valid json</script>
        </body></html>"""
        soup = BeautifulSoup(html, "lxml")
        extract_ebay_jsonld(soup)
        marker = soup.find(id="ebay-jsonld-marker")
        assert marker is None


class TestPostprocessEbay:
    """Regex postprocessor patterns."""

    def test_removes_sign_in(self):
        md = "Sign in\nRegister\nProduct info"
        result = postprocess_ebay(md)
        assert "Sign in" not in result
        assert "Register" not in result
        assert "Product info" in result

    def test_removes_report_item(self):
        md = "Product info\nReport this item\nMore content"
        result = postprocess_ebay(md)
        assert "Report this item" not in result
        assert "Product info" in result

    def test_removes_action_buttons(self):
        md = "Product info\nAdd to Watchlist\nAdd to cart\nBuy It Now\nMake Offer\nPlace bid\nMore"
        result = postprocess_ebay(md)
        assert "Add to Watchlist" not in result
        assert "Add to cart" not in result
        assert "Buy It Now" not in result
        assert "Make Offer" not in result
        assert "Place bid" not in result
        assert "Product info" in result

    def test_removes_ebay_home_breadcrumb(self):
        md = "eBay Home\nProduct title"
        result = postprocess_ebay(md)
        assert "eBay Home" not in result
        assert "Product title" in result

    def test_removes_sponsored(self):
        md = "Listing\nSponsored\nNext listing"
        result = postprocess_ebay(md)
        assert "\nSponsored\n" not in result
        assert "Listing" in result

    def test_removes_gallery_nav(self):
        md = "Picture 1 of 12\nOpens image gallery\nProduct content"
        result = postprocess_ebay(md)
        assert "Picture 1 of 12" not in result
        assert "Opens image gallery" not in result
        assert "Product content" in result

    def test_jsonld_marker_placed_after_heading(self):
        md = "# Cool Widget\n\n__EBAY_JSONLD__**Brand:** Acme\n**Price:** USD 29.99__EBAY_JSONLD__\n\nDescription"
        result = postprocess_ebay(md)
        assert "**Brand:** Acme" in result
        assert "**Price:** USD 29.99" in result
        assert "__EBAY_JSONLD__" not in result
        assert "Description" in result
        # Structured data should appear after heading
        heading_pos = result.index("# Cool Widget")
        brand_pos = result.index("**Brand:** Acme")
        assert brand_pos > heading_pos

    def test_preserves_product_content(self):
        md = (
            "# Vintage Camera\n\n"
            "Excellent condition, barely used.\n\n"
            "## Item specifics\n\n"
            "| Brand | Canon |\n| Model | AE-1 |"
        )
        result = postprocess_ebay(md)
        assert "Vintage Camera" in result
        assert "barely used" in result
        assert "Canon" in result
        assert "AE-1" in result

    # -------------------------------------------------------------------
    # Global Shipping Programme boilerplate (UK eBay)
    # -------------------------------------------------------------------

    def test_removes_gsp_boilerplate(self):
        md = (
            "# Product\n\n"
            "This item will be sent through eBay's Global Shipping Programme.\n\n"
            "Includes international tracking, simplified customs clearance, and no extra charges at delivery. [Learn more](url)\n\n"
            "This amount includes seller specified domestic postage charges as well.\n\n"
            "Located in: London"
        )
        result = postprocess_ebay(md)
        assert "Global Shipping Programme" not in result
        assert "international tracking" not in result
        assert "seller specified domestic" not in result
        assert "Located in: London" in result

    def test_removes_import_charges(self):
        md = "# Product\n\nImport charges:\n\nEst. £19.88 Amount confirmed at checkout\n\nDelivery:"
        result = postprocess_ebay(md)
        assert "Import charges:" not in result
        assert "£19.88" not in result
        assert "Delivery:" in result

    def test_removes_gsp_terms_link(self):
        md = "# Product\n\n[terms and conditions](https://pages.ebay.co.uk/shipping/globalshipping/buyer-tnc.html)\n\nContent"
        result = postprocess_ebay(md)
        assert "terms and conditions" not in result
        assert "Content" in result

    # -------------------------------------------------------------------
    # Payment boilerplate
    # -------------------------------------------------------------------

    def test_removes_payment_boilerplate(self):
        md = (
            "# Product\n\n"
            "Get more time to pay. [See payment information](url)\n\n"
            "International postage and import charges paid to Pitney Bowes Inc. [Learn more](url)\n\n"
            "## Item specifics"
        )
        result = postprocess_ebay(md)
        assert "more time to pay" not in result
        assert "Pitney Bowes" not in result
        assert "## Item specifics" in result

    # -------------------------------------------------------------------
    # Seller business information (UK/EU)
    # -------------------------------------------------------------------

    def test_removes_seller_business_info(self):
        md = (
            "# Product\n\n"
            "## Seller business information\n\n"
            "I certify that all my selling activities will comply with all EU laws and regulations.\n\n"
            "Seller contact information\n\n"
            "Registered as a business seller\n\n"
            "Business\n\n"
            "## Item specifics"
        )
        result = postprocess_ebay(md)
        assert "Seller business information" not in result
        assert "certify" not in result
        assert "Seller contact information" not in result
        assert "Registered as a business seller" not in result
        assert "## Item specifics" in result

    # -------------------------------------------------------------------
    # Sold listing summary
    # -------------------------------------------------------------------

    def test_removes_sold_date_uk_format(self):
        md = "# Product\n\nMon, 23 Feb, 11:13\n\nContent"
        result = postprocess_ebay(md)
        assert "Mon, 23 Feb" not in result
        assert "Content" in result

    def test_removes_sold_date_us_format(self):
        md = "# Product\n\nSat, Feb 22, 2025, 3:00 PM\n\nContent"
        result = postprocess_ebay(md)
        assert "Sat, Feb 22" not in result
        assert "Content" in result

    # -------------------------------------------------------------------
    # Standalone period removal (from ". See details")
    # -------------------------------------------------------------------

    def test_removes_standalone_period(self):
        md = "# Product\n\nLine before\n\n.\n\nLine after"
        result = postprocess_ebay(md)
        assert "\n.\n" not in result
        assert "Line before" in result
        assert "Line after" in result

    def test_removes_see_details_then_period(self):
        md = "# Product\n\n. See details\n\nLocated in: London"
        result = postprocess_ebay(md)
        assert "See details" not in result
        assert "\n.\n" not in result
        assert "Located in: London" in result

    # -------------------------------------------------------------------
    # Duplicate heading/title promotion
    # -------------------------------------------------------------------

    def test_promotes_heading_from_middle(self):
        """Sold listings have the heading in the middle after summary block."""
        md = (
            "__EBAY_JSONLD__**Brand:** Canon__EBAY_JSONLD__\n\n"
            "Sold\n\n"
            "# Canon AE-1\n\n"
            "Seller info"
        )
        result = postprocess_ebay(md)
        assert result.startswith("# Canon AE-1")
        assert "**Brand:** Canon" in result
        assert "Seller info" in result

    def test_removes_duplicate_heading_with_trailing_whitespace(self):
        md = "# Canon AE-1\n\n**Brand:** Canon\n\n# Canon AE-1 \n\nSeller info"
        result = postprocess_ebay(md)
        # Only one heading
        assert result.count("# Canon AE-1") == 1
        assert "Seller info" in result

    def test_removes_duplicate_plain_text_title(self):
        md = "# Canon AE-1\n\n**Brand:** Canon\n\nCanon AE-1\n\n£95.00\n\nSeller info"
        result = postprocess_ebay(md)
        # Plain text title removed, heading preserved
        lines = result.split("\n")
        title_lines = [line for line in lines if line.strip() == "Canon AE-1"]
        assert len(title_lines) == 0  # plain text removed
        assert "# Canon AE-1" in result  # heading kept

    # -------------------------------------------------------------------
    # Empty section header cleanup
    # -------------------------------------------------------------------

    def test_removes_empty_payments_header(self):
        md = "# Product\n\nReturns:\n\n30 days\n\nPayments:\n\n## Item specifics"
        result = postprocess_ebay(md)
        assert "Payments:" not in result
        assert "Returns:" in result

    def test_removes_empty_postage_header(self):
        md = "# Product\n\nPostage:\n\n## Item specifics"
        result = postprocess_ebay(md)
        assert "Postage:" not in result
