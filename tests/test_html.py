"""Tests for HTML processing (clean_html + html_to_markdown)."""

import asyncio
import json
import multiprocessing
import time
from unittest.mock import mock_open, patch

import pytest

from fetchaller.content.html import (
    _MAX_HTML_INPUT_BYTES,
    _MAX_HTML_INPUT_CHARS,
    _PROCESS_MEMORY_LIMIT,
    _VIRTUAL_APPLE_PROCESS_MEMORY_LIMIT,
    HtmlProcessingError,
    _worker_address_space_limit,
    clean_html,
    html_to_markdown,
    validate_html_input_size,
)

# --- clean_html ---


class TestCleanHtml:
    """Tests for HTML cleanup before conversion."""

    def test_removes_scripts_and_styles(self):
        html = "<div><script>alert(1)</script><style>.x{}</style><p>text</p></div>"
        soup, _ = clean_html(html)
        assert soup.find("script") is None
        assert soup.find("style") is None
        assert soup.find("p").text == "text"

    def test_rejects_oversized_bytes_before_decode(self):
        with pytest.raises(
            HtmlProcessingError,
            match=rf"{_MAX_HTML_INPUT_BYTES + 1} bytes",
        ):
            validate_html_input_size(b"x" * (_MAX_HTML_INPUT_BYTES + 1))

    def test_virtual_apple_worker_limit_accounts_for_rosetta_mappings(self):
        with (
            patch("fetchaller.content.html.sys.platform", "linux"),
            patch(
                "builtins.open",
                mock_open(read_data="vendor_id : VirtualApple\n"),
            ),
        ):
            assert _worker_address_space_limit() == _VIRTUAL_APPLE_PROCESS_MEMORY_LIMIT

    def test_native_linux_worker_keeps_one_gib_address_limit(self):
        with (
            patch("fetchaller.content.html.sys.platform", "linux"),
            patch(
                "builtins.open",
                mock_open(read_data="vendor_id : GenuineIntel\n"),
            ),
        ):
            assert _worker_address_space_limit() == _PROCESS_MEMORY_LIMIT

    def test_removes_nav_footer_iframe(self):
        html = '<div><nav>nav</nav><footer>foot</footer><iframe src="x"></iframe><p>keep</p></div>'
        soup, _ = clean_html(html)
        assert "nav" not in soup.text
        assert "foot" not in soup.text
        assert soup.find("iframe") is None

    def test_removes_noscript(self):
        html = "<div><noscript>Please enable JS</noscript><p>content</p></div>"
        soup, _ = clean_html(html)
        assert "enable JS" not in soup.text
        assert "content" in soup.text

    def test_removes_svg(self):
        html = '<div><svg><circle r="5"/></svg><p>text</p></div>'
        soup, _ = clean_html(html)
        assert soup.find("svg") is None
        assert "text" in soup.text

    def test_removes_role_navigation(self):
        html = '<div role="navigation">skip</div><p>keep</p>'
        soup, _ = clean_html(html)
        assert "skip" not in soup.text

    def test_removes_role_banner(self):
        html = '<div role="banner">banner</div><p>keep</p>'
        soup, _ = clean_html(html)
        assert "banner" not in soup.text

    def test_removes_role_contentinfo(self):
        html = '<div role="contentinfo">info</div><p>keep</p>'
        soup, _ = clean_html(html)
        assert "info" not in soup.text

    def test_removes_junk_classes(self):
        html = '<div class="nav">nav</div><div class="ads">ad</div><p>keep</p>'
        soup, _ = clean_html(html)
        assert "nav" not in soup.text
        assert "ad" not in soup.text
        assert "keep" in soup.text

    def test_reddit_selectors(self):
        html = '<div class="side">sidebar</div><div class="content">post</div>'
        soup, _ = clean_html(html, is_reddit=True)
        assert "sidebar" not in soup.text
        assert "post" in soup.text

    def test_reddit_selectors_not_applied_by_default(self):
        html = '<div class="side">sidebar</div>'
        soup, _ = clean_html(html, is_reddit=False)
        assert "sidebar" in soup.text

    def test_reddit_removes_footer_parent(self):
        html = '<div class="footer-parent">foot</div><p>keep</p>'
        soup, _ = clean_html(html, is_reddit=True)
        assert "foot" not in soup.text

    def test_preserves_main_content(self):
        html = "<article><h1>Title</h1><p>Paragraph one.</p><p>Paragraph two.</p></article>"
        soup, _ = clean_html(html)
        assert "Title" in soup.text
        assert "Paragraph one" in soup.text
        assert "Paragraph two" in soup.text

    # C1: Expanded junk selectors

    def test_removes_role_complementary(self):
        html = '<div role="complementary">sidebar</div><p>keep</p>'
        soup, _ = clean_html(html)
        assert "sidebar" not in soup.text

    def test_removes_role_search(self):
        html = '<div role="search"><input/></div><p>keep</p>'
        soup, _ = clean_html(html)
        assert soup.find("[role='search']") is None

    def test_removes_role_dialog(self):
        html = '<div role="dialog">popup</div><p>keep</p>'
        soup, _ = clean_html(html)
        assert "popup" not in soup.text

    def test_preserves_aria_hidden_content(self):
        """aria-hidden elements are kept — they often contain real content on JS-rendered pages."""
        html = '<div aria-hidden="true">accordion panel text</div><p>visible</p>'
        soup, _ = clean_html(html)
        assert "accordion panel text" in soup.text
        assert "visible" in soup.text

    def test_preserves_hidden_attribute_content(self):
        """[hidden] elements are kept — JS frameworks use them for content revealed after hydration."""
        html = "<div hidden>hydrated content</div><p>visible</p>"
        soup, _ = clean_html(html)
        assert "hydrated content" in soup.text

    def test_removes_cookie_banner(self):
        html = '<div class="cookie-banner">Accept cookies</div><p>content</p>'
        soup, _ = clean_html(html)
        assert "cookies" not in soup.text

    def test_removes_modal(self):
        html = '<div class="modal">Sign up!</div><p>content</p>'
        soup, _ = clean_html(html)
        assert "Sign up" not in soup.text

    def test_removes_social_sharing(self):
        html = '<div class="social-links">Share on Twitter</div><p>content</p>'
        soup, _ = clean_html(html)
        assert "Share" not in soup.text

    def test_removes_breadcrumbs(self):
        html = '<div class="breadcrumb">Home > Section</div><p>content</p>'
        soup, _ = clean_html(html)
        assert "Home > Section" not in soup.text

    # C5: Wikipedia selectors

    def test_wikipedia_removes_edit_sections(self):
        html = '<h2>History <span class="mw-editsection">[edit]</span></h2><p>content</p>'
        soup, _ = clean_html(html, url="https://en.wikipedia.org/wiki/Python")
        assert "[edit]" not in soup.text
        assert "History" in soup.text
        assert "content" in soup.text

    def test_wikipedia_removes_navbox(self):
        html = '<div class="navbox">Navigation box</div><p>content</p>'
        soup, _ = clean_html(html, url="https://en.wikipedia.org/wiki/Python")
        assert "Navigation box" not in soup.text

    def test_wikipedia_removes_reflist(self):
        html = '<div class="reflist">References list</div><p>content</p>'
        soup, _ = clean_html(html, url="https://en.wikipedia.org/wiki/Python")
        assert "References" not in soup.text

    def test_wikipedia_removes_toc(self):
        html = '<div id="toc">Table of contents</div><p>content</p>'
        soup, _ = clean_html(html, url="https://en.wikipedia.org/wiki/Python")
        assert "Table of contents" not in soup.text

    def test_wikipedia_removes_catlinks(self):
        html = '<div class="catlinks">Categories: Foo</div><p>content</p>'
        soup, _ = clean_html(html, url="https://en.wikipedia.org/wiki/Python")
        assert "Categories" not in soup.text

    def test_wikipedia_selectors_not_applied_to_other_sites(self):
        html = '<div class="reflist">References</div><p>content</p>'
        soup, _ = clean_html(html, url="https://example.com/page")
        assert "References" in soup.text

    def test_wikipedia_detects_subdomains(self):
        html = '<div class="navbox">Nav</div><p>content</p>'
        soup, _ = clean_html(html, url="https://de.wikipedia.org/wiki/Python")
        assert "Nav" not in soup.text

    # C2: Lazy image recovery

    def test_lazy_image_data_src(self):
        html = '<img data-src="https://example.com/real.jpg" src="data:image/gif;base64,R0lGODlhAQAB"/>'
        soup, _ = clean_html(html)
        img = soup.find("img")
        # After lazy fix + data URI strip, should have resolved src or be gone
        # Since the real src is absolute, it stays
        assert img is None or "data:" not in img.get("src", "")

    def test_lazy_image_no_src(self):
        html = '<img data-src="https://example.com/real.jpg"/>'
        soup, _ = clean_html(html)
        img = soup.find("img")
        assert img is not None
        assert img["src"] == "https://example.com/real.jpg"

    def test_lazy_image_real_src_untouched(self):
        html = '<img src="https://example.com/real.jpg"/>'
        soup, _ = clean_html(html)
        img = soup.find("img")
        assert img is not None
        assert img["src"] == "https://example.com/real.jpg"

    # C3: URL resolution

    def test_does_not_resolve_href(self):
        """Link hrefs are left as-is to save tokens — LLMs have the base URL."""
        html = '<a href="/about">About</a>'
        soup, _ = clean_html(html, url="https://example.com/page")
        link = soup.find("a")
        assert link["href"] == "/about"

    def test_resolves_relative_img_src(self):
        html = '<img src="/img/photo.jpg"/>'
        soup, _ = clean_html(html, url="https://example.com/page")
        img = soup.find("img")
        assert img["src"] == "https://example.com/img/photo.jpg"

    def test_no_url_skips_resolution(self):
        html = '<a href="/about">About</a>'
        soup, _ = clean_html(html)  # No url param
        link = soup.find("a")
        assert link["href"] == "/about"  # Stays relative

    # C4: Data URI stripping

    def test_strips_data_uri_image(self):
        html = '<img src="data:image/png;base64,iVBOR..."/><p>content</p>'
        soup, _ = clean_html(html)
        assert soup.find("img") is None

    def test_data_uri_preserves_useful_alt(self):
        html = '<img src="data:image/png;base64,iVBOR..." alt="Diagram of system"/><p>content</p>'
        soup, _ = clean_html(html)
        assert soup.find("img") is None
        assert "Diagram of system" in soup.text

    def test_data_uri_discards_generic_alt(self):
        html = '<img src="data:image/png;base64,iVBOR..." alt="icon"/>'
        soup, _ = clean_html(html)
        assert soup.find("img") is None
        assert "icon" not in soup.text

    def test_real_image_not_stripped(self):
        html = '<img src="https://example.com/photo.jpg" alt="Photo"/>'
        soup, _ = clean_html(html)
        img = soup.find("img")
        assert img is not None
        assert img["src"] == "https://example.com/photo.jpg"

    # C2-C4 integration: lazy + resolve + strip work together

    def test_lazy_then_resolve_then_strip(self):
        """Image with data: src and data-src="/real.jpg" should become absolute URL."""
        html = '<img src="data:image/gif;base64,R0lGOD" data-src="/real.jpg"/>'
        soup, _ = clean_html(html, url="https://example.com/page")
        img = soup.find("img")
        assert img is not None
        assert img["src"] == "https://example.com/real.jpg"


# --- html_to_markdown ---


class TestHtmlToMarkdown:
    """Tests for full HTML to markdown conversion."""

    async def test_basic_conversion(self):
        html = "<html><head><title>Test</title></head><body><h1>Hello</h1><p>World</p></body></html>"
        md, title = await html_to_markdown(html)
        assert title == "Test"
        assert "Hello" in md
        assert "World" in md

    async def test_title_extraction(self):
        html = "<html><head><title>My Page</title></head><body><p>Content</p></body></html>"
        md, title = await html_to_markdown(html)
        assert title == "My Page"

    async def test_title_prepended_when_no_h1(self):
        html = "<html><head><title>My Page</title></head><body><p>Content</p></body></html>"
        md, title = await html_to_markdown(html)
        assert md.startswith("# My Page")

    async def test_title_not_duplicated_with_h1(self):
        html = "<html><head><title>My Page</title></head><body><h1>My Page</h1><p>Content</p></body></html>"
        md, title = await html_to_markdown(html)
        assert md.count("# My Page") == 1

    async def test_no_title(self):
        html = "<html><body><p>Just content</p></body></html>"
        md, title = await html_to_markdown(html)
        assert title is None
        assert "Just content" in md

    @pytest.mark.parametrize(
        "malformed_product",
        [
            {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": "Useful Product",
                "additionalProperty": [{"name": 42, "value": "x"}],
            },
            {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": "Useful Product",
                "offers": {
                    "price": "9.99",
                    "priceCurrency": "USD",
                    "availability": ["not", "a", "scalar"],
                },
            },
            {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": {"nested": "bad"},
                "brand": ["bad"],
                "description": {"nested": "bad"},
                "sku": [],
                "mpn": {},
                "offers": [{"price": float("inf")}],
                "additionalProperty": "not-a-list",
            },
            {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": "Useful Product",
                "additionalProperty": None,
            },
            {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": "Useful Product",
                "additionalProperty": 42,
            },
            {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": "Useful Product",
                "additionalProperty": True,
            },
            {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": "Useful Product",
                "additionalProperty": 3.14,
            },
        ],
    )
    async def test_malformed_jsonld_product_does_not_hide_visible_content(self, malformed_product):
        html = (
            "<html><body><h1>Visible page</h1><p>Useful content</p>"
            '<script type="application/ld+json">' + json.dumps(malformed_product) + "</script></body></html>"
        )

        markdown, _ = await html_to_markdown(html)

        assert "# Visible page" in markdown
        assert "Useful content" in markdown

    async def test_excessive_newlines_collapsed(self):
        html = "<body><p>A</p>" + "<br>" * 20 + "<p>B</p></body>"
        md, _ = await html_to_markdown(html)
        assert "\n\n\n\n" not in md

    async def test_empty_html(self):
        md, title = await html_to_markdown("")
        assert title is None
        assert md == "" or md.strip() == ""

    async def test_rejects_oversized_input_before_parser_spawn(self):
        with pytest.raises(HtmlProcessingError, match="too large"):
            await html_to_markdown("x" * (_MAX_HTML_INPUT_CHARS + 1))
        cleanup_deadline = time.monotonic() + 2
        while (
            any(child.name == "fetchaller-html-parser" for child in multiprocessing.active_children())
            and time.monotonic() < cleanup_deadline
        ):
            await asyncio.sleep(0.01)
        assert not any(child.name == "fetchaller-html-parser" for child in multiprocessing.active_children())

    async def test_output_bound_is_explicit(self):
        html = "<html><body><p>" + ("content " * 1_000) + "</p></body></html>"
        markdown, _ = await html_to_markdown(
            html,
            max_output_chars=160,
        )
        assert len(markdown) <= 160
        assert markdown.endswith("[HTML conversion truncated at the safe processing limit]")

    async def test_timeout_reaps_worker_and_releases_capacity(self):
        results = await asyncio.gather(
            *(
                html_to_markdown(
                    f"<html><body><p>timed out {index}</p></body></html>",
                    timeout=0.001,
                )
                for index in range(2)
            ),
            return_exceptions=True,
        )
        assert all(isinstance(result, HtmlProcessingError) and "timed out" in str(result) for result in results)
        cleanup_deadline = time.monotonic() + 2
        while (
            any(child.name == "fetchaller-html-parser" for child in multiprocessing.active_children())
            and time.monotonic() < cleanup_deadline
        ):
            await asyncio.sleep(0.01)
        assert not any(child.name == "fetchaller-html-parser" for child in multiprocessing.active_children())

        markdown, _ = await html_to_markdown(
            "<html><body><p>after timeout</p></body></html>",
        )
        assert markdown == "after timeout"

    async def test_slow_process_start_does_not_block_timeout_or_event_loop(self):
        loop_progressed = asyncio.Event()

        async def mark_progress():
            await asyncio.sleep(0.005)
            loop_progressed.set()

        def slow_start(*_args):
            time.sleep(0.2)
            return True

        progress_task = asyncio.create_task(mark_progress())
        started = time.monotonic()
        with patch(
            "fetchaller.content.html._start_html_process",
            side_effect=slow_start,
        ):
            with pytest.raises(HtmlProcessingError, match="timed out"):
                await html_to_markdown(
                    "<html><body><p>slow start</p></body></html>",
                    timeout=0.02,
                )
        elapsed = time.monotonic() - started
        await progress_task

        assert elapsed < 0.1
        assert loop_progressed.is_set()

    async def test_cancellation_reaps_worker_and_releases_capacity(self):
        html = "<html><body>" + ("<p>content</p>" * 100_000) + "</body></html>"
        tasks = [asyncio.create_task(html_to_markdown(html, timeout=30)) for _ in range(2)]
        await asyncio.sleep(0)
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(isinstance(result, asyncio.CancelledError) for result in results)

        cleanup_deadline = time.monotonic() + 2
        while (
            any(child.name == "fetchaller-html-parser" for child in multiprocessing.active_children())
            and time.monotonic() < cleanup_deadline
        ):
            await asyncio.sleep(0.01)
        assert not any(child.name == "fetchaller-html-parser" for child in multiprocessing.active_children())
        markdown, _ = await html_to_markdown(
            "<html><body><p>after cancellation</p></body></html>",
        )
        assert markdown == "after cancellation"

    async def test_heading_styles_atx(self):
        html = "<body><h1>H1</h1><h2>H2</h2><h3>H3</h3></body>"
        md, _ = await html_to_markdown(html)
        assert "# H1" in md
        assert "## H2" in md
        assert "### H3" in md

    async def test_links_preserved(self):
        html = '<body><a href="https://example.com">Link</a></body>'
        md, _ = await html_to_markdown(html)
        assert "https://example.com" in md
        assert "Link" in md

    async def test_code_blocks_preserved(self):
        html = "<body><pre><code>def foo():\n    return 42</code></pre></body>"
        md, _ = await html_to_markdown(html)
        assert "def foo():" in md
        assert "return 42" in md

    async def test_lists_preserved(self):
        html = "<body><ul><li>One</li><li>Two</li><li>Three</li></ul></body>"
        md, _ = await html_to_markdown(html)
        assert "One" in md
        assert "Two" in md
        assert "Three" in md

    async def test_reddit_mode(self):
        html = '<html><body><div class="side">sidebar</div><div class="content"><p>post</p></div></body></html>'
        md, _ = await html_to_markdown(html, is_reddit=True)
        assert "sidebar" not in md
        assert "post" in md

    async def test_junk_removed_from_output(self):
        html = "<html><body><nav>nav stuff</nav><script>evil()</script><p>real content</p></body></html>"
        md, _ = await html_to_markdown(html)
        assert "nav stuff" not in md
        assert "evil" not in md
        assert "real content" in md

    async def test_url_param_resolves_images(self):
        html = '<html><body><img src="/photo.jpg" alt="pic"/></body></html>'
        md, _ = await html_to_markdown(html, url="https://example.com/page")
        assert "https://example.com/photo.jpg" in md

    async def test_wikipedia_cleanup(self):
        html = """<html><body>
            <h2>History <span class="mw-editsection">[edit]</span></h2>
            <p>Article content here.</p>
            <div class="reflist">References...</div>
            <div class="navbox">Navigation...</div>
        </body></html>"""
        md, _ = await html_to_markdown(html, url="https://en.wikipedia.org/wiki/Test")
        assert "Article content" in md
        assert "[edit]" not in md
        assert "References..." not in md
        assert "Navigation..." not in md


class TestCareersLinkPreservation:
    """Nav/header/footer are stripped as junk — right for reading a page, wrong
    for the one link a crawler is usually there to find. A company's careers
    page is almost never linked from the body copy; it lives in the chrome."""

    def test_careers_link_survives_nav_removal(self):
        html = """
        <html><body>
          <nav><a href="/about">About</a><a href="/careers">Careers</a></nav>
          <main><p>Body copy.</p></main>
        </body></html>
        """
        soup, _ = clean_html(html, url="https://example.com/")

        assert soup.find("nav") is None
        text = soup.get_text()
        assert "Careers links (from site navigation)" in text
        assert "About" not in text

    def test_footer_and_header_are_searched_too(self):
        html = """
        <html><body>
          <header><a href="/jobs">Jobs</a></header>
          <footer><a href="/join-us">Join us</a></footer>
          <main><p>Body.</p></main>
        </body></html>
        """
        soup, _ = clean_html(html, url="https://example.com/")
        hrefs = [a["href"] for a in soup.find_all("a", href=True)]

        assert "https://example.com/jobs" in hrefs
        assert "https://example.com/join-us" in hrefs

    def test_links_are_made_absolute(self):
        """Unlike body links, these are kept precisely so the caller can go
        fetch them — by which point the page's base URL is gone."""
        html = '<html><body><nav><a href="/careers">Careers</a></nav><p>x</p></body></html>'
        soup, _ = clean_html(html, url="https://example.com/team/")

        anchor = soup.find("a", string="Careers")
        assert anchor["href"] == "https://example.com/careers"

    def test_base_tag_is_honoured(self):
        html = (
            '<html><head><base href="https://cdn.example.com/"></head>'
            '<body><nav><a href="careers">Careers</a></nav><p>x</p></body></html>'
        )
        soup, _ = clean_html(html, url="https://example.com/")

        assert soup.find("a", string="Careers")["href"] == "https://cdn.example.com/careers"

    def test_non_careers_nav_links_are_still_stripped(self):
        """Widening this to all nav links would re-add the hundreds of chrome
        links that stripping exists to remove."""
        html = """
        <html><body>
          <nav>
            <a href="/products">Products</a><a href="/pricing">Pricing</a>
            <a href="/docs">Docs</a><a href="/blog">Blog</a>
          </nav>
          <main><p>Body.</p></main>
        </body></html>
        """
        soup, _ = clean_html(html, url="https://example.com/")

        assert "Careers links" not in soup.get_text()
        assert soup.find_all("a") == []

    def test_same_page_anchors_are_not_preserved(self):
        """doveka.com's entire nav is '#'-anchors; there is nothing to follow."""
        html = (
            "<html><body><nav>"
            '<a href="#careers">Careers</a><a href="#mission">Mission</a>'
            "</nav><main><p>Body.</p></main></body></html>"
        )
        soup, _ = clean_html(html, url="https://example.com/")

        assert "Careers links" not in soup.get_text()

    @pytest.mark.parametrize("scheme", ["javascript:void(0)", "mailto:jobs@x.com", "tel:+15551234"])
    def test_non_navigable_schemes_are_not_preserved(self, scheme):
        html = f'<html><body><nav><a href="{scheme}">Careers</a></nav><p>x</p></body></html>'
        soup, _ = clean_html(html, url="https://example.com/")

        assert "Careers links" not in soup.get_text()

    def test_duplicates_are_collapsed(self):
        html = """
        <html><body>
          <nav><a href="/careers">Careers</a></nav>
          <footer><a href="/careers/">Careers</a><a href="/careers">Join us</a></footer>
          <main><p>x</p></main>
        </body></html>
        """
        soup, _ = clean_html(html, url="https://example.com/")

        assert soup.get_text().count("https://example.com/careers") <= 1
        assert len(soup.find_all("a")) == 1

    def test_output_is_capped(self):
        links = "".join(f'<a href="/careers-{n}">Careers {n}</a>' for n in range(30))
        html = f"<html><body><nav>{links}</nav><main><p>x</p></main></body></html>"
        soup, _ = clean_html(html, url="https://example.com/")

        assert len(soup.find_all("a")) <= 5

    def test_page_without_chrome_is_untouched(self):
        html = "<html><body><main><p>Just content.</p></main></body></html>"
        soup, _ = clean_html(html, url="https://example.com/")

        assert "Careers links" not in soup.get_text()

    def test_relative_and_absolute_forms_of_one_link_collapse(self):
        """Deduplicating before resolution let '/careers' and the absolute form
        through as two entries for one destination."""
        html = """
        <html><body>
          <nav><a href="/careers">Careers</a></nav>
          <footer><a href="https://example.com/careers">Careers</a></footer>
          <main><p>x</p></main>
        </body></html>
        """
        soup, _ = clean_html(html, url="https://example.com/")

        assert len(soup.find_all("a")) == 1

    def test_nav_nested_in_header_is_not_scanned_twice(self):
        """find_all returns the outer header AND the inner nav; descending into
        both walks the same anchors twice and burns the scan budget."""
        from bs4 import BeautifulSoup

        from fetchaller.content.html import _collect_careers_links

        html = """
        <html><body><header><nav>
          <a href="/careers">Careers</a>
        </nav></header><main><p>x</p></main></body></html>
        """
        links = _collect_careers_links(BeautifulSoup(html, "lxml"))

        assert links == [("Careers", "/careers")]

    def test_a_late_careers_link_survives_a_link_heavy_nav(self):
        from bs4 import BeautifulSoup

        from fetchaller.content.html import _collect_careers_links

        filler = "".join(f'<a href="/p{n}">Page {n}</a>' for n in range(200))
        html = (
            f"<html><body><header><nav>{filler}</nav></header>"
            f'<footer>{filler}<a href="/careers">Careers</a></footer>'
            "<main><p>x</p></main></body></html>"
        )
        links = _collect_careers_links(BeautifulSoup(html, "lxml"))

        assert links == [("Careers", "/careers")]
