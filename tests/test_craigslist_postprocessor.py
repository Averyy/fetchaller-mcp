"""Unit tests for Craigslist regex postprocessor.

Tests postprocess_craigslist() in isolation to verify each pattern works correctly,
and is_craigslist() for URL detection.
"""

from fetchaller.content.craigslist import is_craigslist, postprocess_craigslist


class TestIsCraigslist:
    """URL detection for Craigslist domains."""

    def test_city_subdomain(self):
        assert is_craigslist("https://vancouver.craigslist.org/van/ele/d/post/7908238463.html")

    def test_another_city(self):
        assert is_craigslist("https://newyork.craigslist.org/search/sss")

    def test_bare_domain(self):
        assert is_craigslist("https://craigslist.org/about/sites")

    def test_www(self):
        assert is_craigslist("https://www.craigslist.org/about/help")

    def test_not_craigslist(self):
        assert not is_craigslist("https://example.com/craigslist")

    def test_not_fake_craigslist(self):
        assert not is_craigslist("https://notcraigslist.org/page")


class TestPostprocessCraigslist:
    """Regex postprocessor patterns."""

    def test_removes_cl_branding(self):
        md = "[CL](https://vancouver.craigslist.org/)\nContent here"
        result = postprocess_craigslist(md)
        assert "[CL]" not in result
        assert "Content here" in result

    def test_removes_nav_arrows(self):
        md = "◀ prev\n▲\nnext ▶\nActual content"
        result = postprocess_craigslist(md)
        assert "prev" not in result
        assert "next" not in result
        assert "Actual content" in result

    def test_removes_action_buttons(self):
        md = "reply\nfavorite\nhide\nunhide\nprint\nContent"
        result = postprocess_craigslist(md)
        assert "reply" not in result
        assert "favorite" not in result
        assert "hide" not in result
        assert "print" not in result
        assert "Content" in result

    def test_removes_scam_warning(self):
        md = (
            "Product description\n"
            "[Avoid scams, deal locally](https://www.craigslist.org/about/help/safety/scams/)\n"
            "*Beware wiring (e.g. Western Union), cashier checks, money orders, shipping.*\n"
            "More content"
        )
        result = postprocess_craigslist(md)
        assert "Avoid scams" not in result
        assert "Beware wiring" not in result
        assert "Product description" in result
        assert "More content" in result

    def test_removes_do_not_contact(self):
        md = "Listing\ndo NOT contact me with unsolicited services or offers\nMore"
        result = postprocess_craigslist(md)
        assert "do NOT contact" not in result
        assert "Listing" in result

    def test_removes_post_metadata(self):
        md = "Content\npost id: 7908238463\nposted: 2026-01-12 14:01\nupdated: 2026-02-16 05:06\nMore"
        result = postprocess_craigslist(md)
        assert "post id:" not in result
        assert "posted:" not in result
        assert "updated:" not in result
        assert "Content" in result

    def test_removes_loading_indicators(self):
        md = "loading\nreading\nwriting\nsaving\nsearching\nrefresh the page.\nReal content"
        result = postprocess_craigslist(md)
        assert "loading" not in result
        assert "refresh the page" not in result
        assert "Real content" in result

    def test_removes_qr_code_text(self):
        md = "Product info\nQR Code Link to This Post\nDescription"
        result = postprocess_craigslist(md)
        assert "QR Code" not in result
        assert "Product info" in result

    def test_removes_flag_symbols(self):
        md = "Content\n⚐\n⚑\n[flagged](https://www.craigslist.org/about/help/faqs/flagging)\nMore"
        result = postprocess_craigslist(md)
        assert "⚐" not in result
        assert "flagged" not in result
        assert "Content" in result

    def test_removes_gallery_nav(self):
        md = "‹\nimage 1 of 6\n›\nProduct description"
        result = postprocess_craigslist(md)
        assert "image 1 of 6" not in result
        assert "Product description" in result

    def test_preserves_listing_content(self):
        md = (
            "# Stanton ST 150 - $1,250 (West End)\n\n"
            "I'm selling a Stanton ST 150 turntable on very good condition.\n\n"
            "condition: excellent\n\n"
            "make / manufacturer: Stanton, Kenwood"
        )
        result = postprocess_craigslist(md)
        assert "Stanton ST 150" in result
        assert "$1,250" in result
        assert "very good condition" in result
        assert "excellent" in result

    def test_removes_best_of(self):
        md = "Content\n[♥ best of](https://post.craigslist.org/flag) [[?]]\nMore"
        result = postprocess_craigslist(md)
        assert "best of" not in result
        assert "Content" in result
