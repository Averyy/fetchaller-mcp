"""Verify that CSS selectors and postprocessors are dispatched for the correct sites.

Each test passes site-specific HTML through clean_html() or html_to_markdown()
and asserts that the correct site-specific cleanup was applied. If _detect_site()
breaks for any site, these tests fail — even though isolated postprocessor tests
still pass.

This catches the class of bug where detection returns the wrong key but the
per-module unit tests (which don't go through detection) still pass.
"""


from fetchaller.content.html import _html_to_markdown_sync, clean_html

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wrap(body_content: str, head: str = "") -> str:
    """Wrap body content in a full HTML document."""
    return f"<html><head>{head}</head><body>{body_content}</body></html>"


# ---------------------------------------------------------------------------
# CSS selector dispatch — clean_html() must remove site-specific elements
# ---------------------------------------------------------------------------


class TestSelectorDispatch:
    """For each site: pass HTML with a known CSS-targeted element, assert it's removed."""

    def test_amazon_sponsored_removed(self):
        html = _wrap(
            '<div id="sp_detail">sponsored products junk</div>'
            '<div id="feature-bullets"><ul><li>Great product</li></ul></div>',
        )
        soup, site = clean_html(html, url="https://www.amazon.ca/dp/B0D1XD1ZV3")
        assert site == "amazon"
        assert "sponsored products junk" not in soup.get_text()
        assert "Great product" in soup.get_text()

    def test_amazon_footer_removed(self):
        html = _wrap(
            '<div id="feature-bullets"><ul><li>Product feature</li></ul></div>'
            '<div id="navFooter">footer links</div>',
        )
        soup, site = clean_html(html, url="https://www.amazon.com/dp/B123")
        assert site == "amazon"
        assert "footer links" not in soup.get_text()
        assert "Product feature" in soup.get_text()

    def test_amazon_variant_swatches_removed(self):
        html = _wrap(
            '<div id="variation_color_name"><img src="swatch.jpg" />Green</div>'
            '<div id="productOverview_feature_div">| Colour | Green |</div>',
        )
        soup, site = clean_html(html, url="https://www.amazon.ca/dp/B078W5SYLR")
        assert site == "amazon"
        assert "swatch.jpg" not in str(soup)
        assert "Colour" in soup.get_text()

    def test_amazon_breadcrumbs_removed(self):
        html = _wrap(
            '<div id="wayfinding-breadcrumbs_feature_div">Pet Supplies > Dogs</div>'
            '<div id="productTitle">LED Dog Collar</div>',
        )
        soup, site = clean_html(html, url="https://www.amazon.ca/dp/B078W5SYLR")
        assert site == "amazon"
        assert "Pet Supplies" not in soup.get_text()
        assert "LED Dog Collar" in soup.get_text()

    def test_amazon_forms_removed(self):
        """Forms (lower price, sign-in) are removed by soup cleanup."""
        html = _wrap(
            '<p>Product info</p>'
            '<form id="priceForm"><input name="price"/><button>Submit</button></form>',
        )
        soup, site = clean_html(html, url="https://www.amazon.ca/dp/B078W5SYLR")
        assert site == "amazon"
        assert "Submit" not in soup.get_text()
        assert "Product info" in soup.get_text()

    def test_amazon_sspa_links_removed(self):
        """Sponsored /sspa/click links are removed by soup cleanup."""
        html = _wrap(
            '<p>Product info</p>'
            '<a href="/sspa/click?ie=UTF8&spc=abc">Sponsored Product</a>',
        )
        soup, site = clean_html(html, url="https://www.amazon.ca/dp/B078W5SYLR")
        assert site == "amazon"
        assert "Sponsored Product" not in soup.get_text()
        assert "Product info" in soup.get_text()

    def test_reddit_sidebar_removed(self):
        html = _wrap('<div class="side">sidebar junk</div><div class="content">real post</div>')
        soup, site = clean_html(html, is_reddit=True)
        assert site == "reddit"
        assert "sidebar junk" not in soup.get_text()
        assert "real post" in soup.get_text()

    def test_hackernews_yclinks_removed(self):
        html = _wrap(
            '<span class="yclinks">hn nav links</span><p>story content</p>',
        )
        soup, site = clean_html(html, url="https://news.ycombinator.com/")
        assert site == "hackernews"
        assert "hn nav links" not in soup.get_text()
        assert "story content" in soup.get_text()

    def test_github_appheader_removed(self):
        html = _wrap(
            '<div class="AppHeader">github header junk</div><div class="markdown-body">readme content</div>',
        )
        soup, site = clean_html(html, url="https://github.com/owner/repo")
        assert site == "github"
        assert "github header junk" not in soup.get_text()
        assert "readme content" in soup.get_text()

    def test_huggingface_mainheader_removed(self):
        html = _wrap(
            '<div data-target="MainHeader">hf header junk</div><div>model card content</div>',
        )
        soup, site = clean_html(html, url="https://huggingface.co/org/model")
        assert site == "huggingface"
        assert "hf header junk" not in soup.get_text()
        assert "model card content" in soup.get_text()

    def test_redflagdeals_navbar_removed(self):
        html = _wrap(
            '<div class="main_nav_bar">rfd nav bar</div><div class="post">deal content</div>',
        )
        soup, site = clean_html(html, url="https://forums.redflagdeals.com/deal-123/")
        assert site == "redflagdeals"
        assert "rfd nav bar" not in soup.get_text()
        assert "deal content" in soup.get_text()

    def test_stackoverflow_sidebar_removed(self):
        html = _wrap(
            '<div id="sidebar">so sidebar junk</div><div id="question">question content</div>',
        )
        soup, site = clean_html(html, url="https://stackoverflow.com/questions/1")
        assert site == "stackoverflow"
        assert "so sidebar junk" not in soup.get_text()
        assert "question content" in soup.get_text()

    def test_medium_signup_button_removed(self):
        html = _wrap(
            '<button data-testid="headerSignUpButton">Sign up</button><article>article content</article>',
        )
        soup, site = clean_html(html, url="https://medium.com/@user/article")
        assert site == "medium"
        assert soup.find(attrs={"data-testid": "headerSignUpButton"}) is None
        assert "article content" in soup.get_text()

    def test_craigslist_leftbar_removed(self):
        html = _wrap(
            '<div id="leftbar">search filters junk</div>'
            '<div>Actual listing content</div>',
        )
        soup, site = clean_html(html, url="https://vancouver.craigslist.org/van/ele/d/post/123.html")
        assert site == "craigslist"
        assert "search filters junk" not in soup.get_text()
        assert "Actual listing content" in soup.get_text()

    def test_ebay_header_removed(self):
        html = _wrap(
            '<div id="gh-top">ebay header junk</div>'
            '<div>Product content</div>',
        )
        soup, site = clean_html(html, url="https://www.ebay.com/itm/123456789")
        assert site == "ebay"
        assert "ebay header junk" not in soup.get_text()
        assert "Product content" in soup.get_text()

    def test_kijiji_header_removed(self):
        html = _wrap(
            '<div id="MainHeader">kijiji header junk</div>'
            '<div>Listing content</div>',
        )
        soup, site = clean_html(html, url="https://www.kijiji.ca/v-cell-phone/ottawa/iphone/123")
        assert site == "kijiji"
        assert "kijiji header junk" not in soup.get_text()
        assert "Listing content" in soup.get_text()

    def test_digikey_header_removed(self):
        html = _wrap(
            '<div id="header">digikey header junk</div>'
            '<div>Part details content</div>',
        )
        soup, site = clean_html(html, url="https://www.digikey.com/en/products/detail/part/12345")
        assert site == "digikey"
        assert "digikey header junk" not in soup.get_text()
        assert "Part details content" in soup.get_text()

    def test_mouser_header_removed(self):
        html = _wrap(
            '<div id="header">mouser header junk</div>'
            '<div>Part details content</div>',
        )
        soup, site = clean_html(html, url="https://www.mouser.com/ProductDetail/12345")
        assert site == "mouser"
        assert "mouser header junk" not in soup.get_text()
        assert "Part details content" in soup.get_text()

    def test_molex_header_removed(self):
        html = _wrap(
            '<div class="cmp-header">molex header junk</div>'
            '<div>Product content</div>',
        )
        soup, site = clean_html(html, url="https://www.molex.com/en-us/products/part-detail/430250408")
        assert site == "molex"
        assert "molex header junk" not in soup.get_text()
        assert "Product content" in soup.get_text()

    def test_soylent_announcement_banner_removed(self):
        html = _wrap(
            '<div id="shopify-section-announcement-banner">free shipping social links</div>'
            '<div>Product content</div>',
        )
        soup, site = clean_html(html, url="https://www.soylent.ca/products/soylent-drink")
        assert site == "soylent"
        assert "free shipping social links" not in soup.get_text()
        assert "Product content" in soup.get_text()

    def test_soylent_footer_removed(self):
        html = _wrap(
            '<div>Product content</div>'
            '<div id="shopify-section-footer">footer links newsletter</div>',
        )
        soup, site = clean_html(html, url="https://www.soylent.ca/products/soylent-drink")
        assert site == "soylent"
        assert "footer links newsletter" not in soup.get_text()
        assert "Product content" in soup.get_text()

    def test_soylent_rebuy_cart_removed(self):
        html = _wrap(
            '<div>Product content</div>'
            '<script id="rebuy-cart-template" type="text/template">rebuy cart flyout</script>',
        )
        soup, site = clean_html(html, url="https://www.soylent.ca/products/soylent-drink")
        assert site == "soylent"
        assert "rebuy cart flyout" not in str(soup)
        assert "Product content" in soup.get_text()

    def test_wikipedia_editsection_removed(self):
        html = _wrap(
            '<span class="mw-editsection">[edit]</span><p>encyclopedia content</p>',
        )
        soup, site = clean_html(html, url="https://en.wikipedia.org/wiki/Python")
        assert site == "wikipedia"
        assert soup.find(class_="mw-editsection") is None
        assert "encyclopedia content" in soup.get_text()

    def test_forum_xenforo_pnav_removed(self):
        """Unknown domain with XenForo HTML markers → forum site key, .p-nav removed."""
        html = '<html id="XF"><head></head><body><div class="p-nav">forum nav</div><p>thread content</p></body></html>'
        soup, site = clean_html(html, url="https://unknown-forum.example.com/threads/1")
        assert site == "forum"
        assert "forum nav" not in soup.get_text()
        assert "thread content" in soup.get_text()

    def test_generic_does_not_remove_site_specific_elements(self):
        """On a generic page, site-specific selectors must NOT be applied."""
        html = _wrap(
            '<div id="sidebar">sidebar content</div>'
            '<div class="AppHeader">header content</div>'
            '<span class="yclinks">yclinks content</span>'
            '<p>main content</p>',
        )
        soup, site = clean_html(html, url="https://example.com/page")
        assert site is None
        text = soup.get_text()
        # Generic junk selectors may remove .sidebar (it's in generic list)
        # but AppHeader and yclinks are site-specific and should be preserved
        assert "header content" in text
        assert "yclinks content" in text
        assert "main content" in text


# ---------------------------------------------------------------------------
# Postprocessor dispatch — html_to_markdown() must apply site-specific regex
# ---------------------------------------------------------------------------


class TestPostprocessorDispatch:
    """For each site with a postprocessor: pass HTML that produces a pattern
    only the correct postprocessor removes."""

    def test_amazon_feedback_block_removed(self):
        """Amazon postprocessor removes 'Report an issue' links."""
        html = _wrap(
            '<p>Great product features</p>'
            '<a href="?ref=dp#tellAmazon_feature_div">Report an issue with this product or seller</a>',
        )
        md, _ = _html_to_markdown_sync(html, url="https://www.amazon.ca/dp/B0D1XD1ZV3")
        assert "Report an issue" not in md
        assert "Great product features" in md

    def test_github_issue_in_repo_removed(self):
        """GitHub postprocessor removes '#123 In org/repo;' patterns."""
        # This pattern appears in GitHub issue list pages after markdownify
        html = _wrap(
            '<p>Some issue title</p>'
            '<p>#123\xa0In facebook/react;</p>'
            '<p>Another issue</p>',
        )
        md, _ = _html_to_markdown_sync(html, url="https://github.com/facebook/react/issues")
        assert "In facebook/react" not in md
        assert "Some issue title" in md

    def test_stackoverflow_answered_date_removed(self):
        """SO postprocessor removes 'answered Oct 23, 2008 at 22:21' lines."""
        html = _wrap(
            '<div id="answers">'
            '<p>The answer is 42.</p>'
            '<p>answered Oct 23, 2008 at 22:21</p>'
            '</div>',
        )
        md, _ = _html_to_markdown_sync(html, url="https://stackoverflow.com/questions/1")
        assert "answered Oct" not in md
        assert "The answer is 42" in md

    def test_redflagdeals_back_to_menu_removed(self):
        """RFD postprocessor removes 'Back to Menu' lines."""
        html = _wrap(
            '<p>Back to Menu</p>'
            '<div class="post"><p>Great deal on monitors!</p></div>',
        )
        md, _ = _html_to_markdown_sync(html, url="https://forums.redflagdeals.com/deal-123/")
        assert "Back to Menu" not in md
        assert "Great deal on monitors" in md

    def test_forum_xenforo_login_menu_removed(self):
        """Forum postprocessor removes XenForo 'Menu / Log in / Sign up' blocks."""
        html = """<html id="XF"><head></head><body>
<p>Menu</p>
<a href="/login/">Log in</a>
<hr>
<a href="/register/">Sign up</a>
<hr>
<p>Actual thread content here.</p>
</body></html>"""
        md, _ = _html_to_markdown_sync(html, url="https://unknown-forum.example.com/threads/1")
        assert "Log in" not in md
        assert "Sign up" not in md
        assert "Actual thread content" in md

    def test_craigslist_scam_warning_removed(self):
        """Craigslist postprocessor removes scam warning boilerplate."""
        html = _wrap(
            '<p>Great turntable for sale</p>'
            '<a href="https://www.craigslist.org/about/help/safety/scams/">'
            'Avoid scams, deal locally</a>',
        )
        md, _ = _html_to_markdown_sync(html, url="https://vancouver.craigslist.org/van/ele/d/post/123.html")
        assert "Avoid scams" not in md
        assert "turntable for sale" in md

    def test_ebay_jsonld_extracted(self):
        """eBay postprocessor extracts JSON-LD product data."""
        html = _wrap(
            '<script type="application/ld+json">'
            '{"@type": "Product", "brand": {"name": "Acme"}, '
            '"offers": {"price": "29.99", "priceCurrency": "USD"}}'
            '</script>'
            '<h1>Test Widget</h1>'
            '<p>Great product</p>',
        )
        md, _ = _html_to_markdown_sync(html, url="https://www.ebay.com/itm/123456789")
        assert "**Brand:** Acme" in md
        assert "**Price:** USD 29.99" in md
        assert "Great product" in md

    def test_kijiji_sponsored_removed(self):
        """Kijiji postprocessor removes 'Sponsored' labels."""
        html = _wrap(
            '<p>Great product listing</p>'
            '<p>Sponsored</p>'
            '<p>Another listing</p>',
        )
        md, _ = _html_to_markdown_sync(html, url="https://www.kijiji.ca/v-buy-sell/ottawa/item/123")
        assert "Sponsored" not in md
        assert "Great product listing" in md

    def test_digikey_add_to_cart_removed(self):
        """DigiKey postprocessor removes 'Add to Cart' buttons."""
        html = _wrap(
            '<p>STM32F103C8T6</p>'
            '<p>Add to Cart</p>'
            '<p>ARM Cortex-M3 72MHz</p>',
        )
        md, _ = _html_to_markdown_sync(html, url="https://www.digikey.com/en/products/detail/part/12345")
        assert "Add to Cart" not in md
        assert "STM32F103C8T6" in md

    def test_mouser_add_to_cart_removed(self):
        """Mouser postprocessor removes 'Add to Cart' buttons."""
        html = _wrap(
            '<p>ESP32-S3-WROOM-1</p>'
            '<p>Add to Cart</p>'
            '<p>Wi-Fi + BLE Module</p>',
        )
        md, _ = _html_to_markdown_sync(html, url="https://www.mouser.com/ProductDetail/12345")
        assert "Add to Cart" not in md
        assert "ESP32-S3-WROOM-1" in md

    def test_soylent_gsf_quantity_extracted(self):
        """Soylent postprocessor extracts exact quantity from gsf_conversion_data."""
        html = _wrap(
            '<h1>Soylent Drink</h1>'
            '<p>Product description</p>'
            '<script>gsf_conversion_data = {quantity : "150"};</script>',
        )
        md, _ = _html_to_markdown_sync(html, url="https://www.soylent.ca/products/soylent-drink")
        assert "**Stock: 150 units**" in md
        assert "__SOYLENT_" not in md

    def test_soylent_availability_extracted(self):
        """Soylent postprocessor extracts available:true/false from Shopify variant JSON."""
        html = _wrap(
            '<h1>Soylent Drink</h1>'
            '<p>Product description</p>'
            '<script>let variants = [{"id":123,"available":true,"name":"Soylent"}];</script>',
        )
        md, _ = _html_to_markdown_sync(html, url="https://www.soylent.com/products/soylent-drink")
        assert "**In stock**" in md
        assert "__SOYLENT_" not in md
        assert "Product description" in md

    def test_molex_jsonld_extracted(self):
        """Molex postprocessor extracts JSON-LD product specs."""
        html = _wrap(
            '<script type="application/ld+json">'
            '{"@type": "Product", "name": "Micro-Fit 3.0", "sku": "430250408", '
            '"brand": {"name": "Molex"}, '
            '"additionalProperty": ['
            '{"@type": "PropertyValue", "name": "Pitch", "value": "3.00mm"}'
            ']}'
            '</script>'
            '<h1>Connector Part</h1>'
            '<p>Limited Information Available</p>',
        )
        md, _ = _html_to_markdown_sync(html, url="https://www.molex.com/en-us/products/part-detail/430250408")
        assert "**Product:** Micro-Fit 3.0" in md
        assert "**SKU:** 430250408" in md
        assert "**Brand:** Molex" in md
        assert "- **Pitch:** 3.00mm" in md
        assert "Limited Information" not in md

    def test_generic_jsonld_product_extracted(self):
        """Generic JSON-LD fallback extracts Product data on unknown sites."""
        html = _wrap(
            '<script type="application/ld+json">'
            '{"@type": "Product", "name": "Widget Pro", '
            '"brand": "AcmeCorp", '
            '"offers": {"price": "49.99", "priceCurrency": "USD"}, '
            '"additionalProperty": ['
            '{"@type": "PropertyValue", "name": "Weight", "value": "250g"}'
            ']}'
            '</script>'
            '<h1>Widget Pro</h1>'
            '<p>Product page content</p>',
        )
        md, _ = _html_to_markdown_sync(html, url="https://www.unknownsite.com/product/123")
        assert "**Product:** Widget Pro" in md
        assert "**Brand:** AcmeCorp" in md
        assert "**Price:** USD 49.99" in md
        assert "- **Weight:** 250g" in md
        assert "Product page content" in md

    def test_generic_jsonld_ignored_for_non_product(self):
        """Generic JSON-LD fallback does NOT extract non-Product types."""
        html = _wrap(
            '<script type="application/ld+json">'
            '{"@type": "NewsArticle", "headline": "Breaking News"}'
            '</script>'
            '<h1>Article Title</h1>'
            '<p>Article content</p>',
        )
        md, _ = _html_to_markdown_sync(html, url="https://www.unknownsite.com/article/123")
        assert "**Product:**" not in md
        assert "Article content" in md

    def test_generic_jsonld_not_fired_for_known_site(self):
        """Generic JSON-LD fallback does NOT fire for sites with modules (eBay)."""
        html = _wrap(
            '<script type="application/ld+json">'
            '{"@type": "Product", "name": "Test Item", '
            '"brand": {"name": "TestBrand"}, '
            '"offers": {"price": "10.00", "priceCurrency": "USD"}}'
            '</script>'
            '<h1>Test Item</h1>',
        )
        md, _ = _html_to_markdown_sync(html, url="https://www.ebay.com/itm/123456789")
        # eBay's own extractor should handle this, not the generic one
        assert "__GENERIC_JSONLD__" not in md
        # eBay extractor should have worked
        assert "**Brand:** TestBrand" in md

    def test_generic_does_not_apply_stackoverflow_postprocessor(self):
        """On a generic page, SO-specific patterns must NOT be removed."""
        html = _wrap(
            '<p>The answer is 42.</p>'
            '<p>answered Oct 23, 2008 at 22:21</p>',
        )
        md, _ = _html_to_markdown_sync(html, url="https://example.com/page")
        # On a generic page, the SO postprocessor should NOT fire
        assert "answered Oct" in md

    def test_generic_does_not_apply_rfd_postprocessor(self):
        """On a generic page, RFD-specific 'Back to Menu' must NOT be removed."""
        html = _wrap('<p>Back to Menu</p><p>Other content</p>')
        md, _ = _html_to_markdown_sync(html, url="https://example.com/page")
        assert "Back to Menu" in md

    def test_generic_does_not_apply_github_postprocessor(self):
        """On a generic page, GitHub-specific '#123 In org/repo;' must NOT be removed."""
        html = _wrap('<p>#123\xa0In facebook/react;</p><p>Content</p>')
        md, _ = _html_to_markdown_sync(html, url="https://example.com/page")
        assert "In facebook/react" in md
