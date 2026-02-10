"""Tests for forum feed detection, URL transforms, parsing, and formatting."""

from bs4 import BeautifulSoup

from fetchaller.content.forums import (
    discover_feed_url,
    format_feed_as_markdown,
    is_discourse_html,
    is_forum_html,
    parse_feed,
    transform_forum_url,
)

# ---------------------------------------------------------------------------
# URL Transform Tests
# ---------------------------------------------------------------------------


class TestTransformForumUrl:
    """Tests for transform_forum_url."""

    # --- vBulletin listings ---

    def test_bimmerpost_listing(self):
        result = transform_forum_url(
            "https://u11.bimmerpost.com/forums/forumdisplay.php?f=944"
        )
        assert result.is_forum_feed
        assert result.forum_software == "vbulletin"
        assert "external.php" in result.url
        assert "forumids=944" in result.url
        assert "type=RSS2" in result.url

    def test_bimmerpost_subdomain_f30(self):
        result = transform_forum_url(
            "https://f30.bimmerpost.com/forums/forumdisplay.php?f=101"
        )
        assert result.is_forum_feed
        assert "forumids=101" in result.url

    # --- vBulletin threads (NOT transformed — external.php doesn't support thread RSS) ---

    def test_bimmerpost_thread_not_transformed(self):
        result = transform_forum_url(
            "https://u11.bimmerpost.com/forums/showthread.php?t=2045678"
        )
        assert not result.is_forum_feed
        assert result.forum_software == "vbulletin"
        assert result.url == "https://u11.bimmerpost.com/forums/showthread.php?t=2045678"

    def test_bimmerpost_thread_by_post_id_passthrough(self):
        result = transform_forum_url(
            "https://u11.bimmerpost.com/forums/showthread.php?p=12345678"
        )
        assert not result.is_forum_feed
        assert result.forum_software == "vbulletin"

    # --- XenForo listings ---

    def test_vwvortex_listing(self):
        result = transform_forum_url(
            "https://www.vwvortex.com/forums/golf-vii-r.5357/"
        )
        assert result.is_forum_feed
        assert result.forum_software == "xenforo"
        assert result.url == "https://www.vwvortex.com/forums/golf-vii-r.5357/index.rss"

    def test_golfmk7_listing_index_php(self):
        result = transform_forum_url(
            "https://www.golfmk7.com/forums/index.php?forums/golf-r-discussions.134/"
        )
        assert result.is_forum_feed
        assert result.forum_software == "xenforo"
        assert "golf-r-discussions.134/index.rss" in result.url

    def test_rdforum_listing(self):
        result = transform_forum_url(
            "https://www.rdforum.org/forums/radar-detectors-tech.rates.5/"
        )
        assert result.is_forum_feed
        assert result.forum_software == "xenforo"
        assert result.url == "https://www.rdforum.org/forums/radar-detectors-tech.rates.5/index.rss"

    # --- XenForo threads (NOT transformed — thread RSS disabled on most installs) ---

    def test_vwvortex_thread_not_transformed(self):
        url = "https://www.vwvortex.com/threads/what-did-you-do-to-your-golf-r-today.6926800/"
        result = transform_forum_url(url)
        assert not result.is_forum_feed
        assert result.forum_software == "xenforo"
        assert result.url == url

    def test_golfmk7_thread_index_php_not_transformed(self):
        url = "https://www.golfmk7.com/forums/index.php?threads/my-thread.12345/"
        result = transform_forum_url(url)
        assert not result.is_forum_feed
        assert result.forum_software == "xenforo"

    def test_xenforo_thread_with_page_not_transformed(self):
        url = "https://www.vwvortex.com/threads/some-thread.123/page-5"
        result = transform_forum_url(url)
        assert not result.is_forum_feed
        assert result.forum_software == "xenforo"

    # --- phpBB (RFD) ---

    def test_rfd_listing(self):
        result = transform_forum_url(
            "https://forums.redflagdeals.com/ongoing-deal-discussion-f129/"
        )
        assert result.is_forum_feed
        assert result.forum_software == "phpbb"
        assert result.url == "https://forums.redflagdeals.com/feed/forum/129"

    def test_rfd_thread_passthrough(self):
        """RFD thread URLs should NOT be transformed (handled by redflagdeals.py)."""
        url = "https://forums.redflagdeals.com/some-deal-t2660019.html"
        result = transform_forum_url(url)
        # Not a listing URL, so no transform
        assert not result.is_forum_feed
        assert result.forum_software == "phpbb"

    # --- Already-feed URLs ---

    def test_already_rss_passthrough(self):
        url = "https://www.vwvortex.com/forums/golf-vii-r.5357/index.rss"
        result = transform_forum_url(url)
        assert result.is_forum_feed
        assert result.url == url

    def test_already_feed_url_passthrough(self):
        url = "https://forums.redflagdeals.com/feed/forum/129"
        result = transform_forum_url(url)
        assert result.is_forum_feed
        assert result.url == url

    # --- Unknown URLs ---

    def test_unknown_url_passthrough(self):
        result = transform_forum_url("https://example.com/some-page")
        assert not result.is_forum_feed
        assert result.forum_software is None
        assert result.url == "https://example.com/some-page"

    def test_invalid_url_passthrough(self):
        result = transform_forum_url("not a url")
        assert not result.is_forum_feed


# ---------------------------------------------------------------------------
# Feed Parsing Tests
# ---------------------------------------------------------------------------

_RSS2_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:dc="http://purl.org/dc/elements/1.1/"
     xmlns:slash="http://purl.org/rss/1.0/modules/slash/"
     xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>Golf VII R</title>
    <link>https://www.vwvortex.com/forums/golf-vii-r.5357/</link>
    <description>Pair the Golf R with VWVortex</description>
    <item>
      <title>What did you do to your Golf R today?</title>
      <link>https://www.vwvortex.com/threads/what-did-you-do-to-your-golf-r-today.6926800/</link>
      <dc:creator>Alive By time</dc:creator>
      <pubDate>Mon, 10 Feb 2026 18:11:00 +0000</pubDate>
      <slash:comments>27945</slash:comments>
      <category>Golf R</category>
      <description>&lt;p&gt;Looking for intake recommendations for the MK7.5...&lt;/p&gt;</description>
    </item>
    <item>
      <title>MK7.5 R vs MK8 R</title>
      <link>https://www.vwvortex.com/threads/mk7-5-r-vs-mk8-r.123456/</link>
      <dc:creator>golfr_fan</dc:creator>
      <pubDate>Sun, 09 Feb 2026 14:22:00 +0000</pubDate>
      <slash:comments>142</slash:comments>
      <description>Both great cars but the MK7.5 interior is better</description>
    </item>
  </channel>
</rss>"""

_ATOM_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Hot Deals Forum</title>
  <link href="https://forums.redflagdeals.com/" rel="alternate"/>
  <subtitle>Latest deals and discussions</subtitle>
  <entry>
    <title>50% off Widget Pro at Store X</title>
    <link href="https://forums.redflagdeals.com/store-x-widget-pro-50-off-t1234567.html" rel="alternate"/>
    <author><name>deal_hunter</name></author>
    <updated>2026-02-10T15:30:00Z</updated>
    <category term="Hot Deals"/>
    <content type="html">&lt;p&gt;Found this deal at Store X...&lt;/p&gt;</content>
  </entry>
  <entry>
    <title>Best credit card for groceries?</title>
    <link href="https://forums.redflagdeals.com/best-cc-groceries-t2345678.html" rel="alternate"/>
    <author><name>frugal_shopper</name></author>
    <updated>2026-02-09T10:00:00+00:00</updated>
    <content type="html">Looking for recommendations on grocery cashback cards</content>
  </entry>
</feed>"""


class TestParseFeed:
    """Tests for parse_feed."""

    def test_rss2_basic(self):
        feed = parse_feed(_RSS2_SAMPLE)
        assert feed is not None
        assert feed.feed_type == "rss2"
        assert feed.title == "Golf VII R"
        assert len(feed.items) == 2

    def test_rss2_item_fields(self):
        feed = parse_feed(_RSS2_SAMPLE)
        item = feed.items[0]
        assert item.title == "What did you do to your Golf R today?"
        assert "vwvortex.com" in item.link
        assert item.author == "Alive By time"
        assert item.published == "2026-02-10 18:11"
        assert item.comment_count == "27945"
        assert "Golf R" in item.categories
        assert "intake recommendations" in item.description

    def test_rss2_html_stripped(self):
        feed = parse_feed(_RSS2_SAMPLE)
        item = feed.items[0]
        assert "<p>" not in item.description

    def test_atom_basic(self):
        feed = parse_feed(_ATOM_SAMPLE)
        assert feed is not None
        assert feed.feed_type == "atom"
        assert feed.title == "Hot Deals Forum"
        assert len(feed.items) == 2

    def test_atom_item_fields(self):
        feed = parse_feed(_ATOM_SAMPLE)
        item = feed.items[0]
        assert item.title == "50% off Widget Pro at Store X"
        assert "redflagdeals.com" in item.link
        assert item.author == "deal_hunter"
        assert item.published == "2026-02-10 15:30"
        assert "Hot Deals" in item.categories
        assert "Store X" in item.description

    def test_atom_iso_date_with_offset(self):
        feed = parse_feed(_ATOM_SAMPLE)
        item = feed.items[1]
        assert item.published == "2026-02-09 10:00"

    def test_invalid_xml_returns_none(self):
        assert parse_feed("not xml at all") is None

    def test_non_feed_xml_returns_none(self):
        assert parse_feed("<root><child>text</child></root>") is None

    def test_empty_channel(self):
        xml = """<?xml version="1.0"?>
        <rss version="2.0">
          <channel>
            <title>Empty Forum</title>
          </channel>
        </rss>"""
        feed = parse_feed(xml)
        assert feed is not None
        assert feed.title == "Empty Forum"
        assert len(feed.items) == 0


# ---------------------------------------------------------------------------
# Feed Formatting Tests
# ---------------------------------------------------------------------------


class TestFormatFeedAsMarkdown:
    """Tests for format_feed_as_markdown."""

    def test_numbered_items(self):
        feed = parse_feed(_RSS2_SAMPLE)
        md = format_feed_as_markdown(feed)
        assert "# Golf VII R" in md
        assert "1. What did you do to your Golf R today?" in md
        assert "Alive By time" in md
        assert "27945 comments" in md
        assert "2. MK7.5 R vs MK8 R" in md

    def test_empty_feed(self):
        xml = """<?xml version="1.0"?>
        <rss version="2.0">
          <channel><title>Empty</title></channel>
        </rss>"""
        feed = parse_feed(xml)
        md = format_feed_as_markdown(feed)
        assert "# Empty" in md
        assert "(No items)" in md

    def test_missing_optional_fields(self):
        xml = """<?xml version="1.0"?>
        <rss version="2.0">
          <channel>
            <item>
              <title>Thread title</title>
              <link>https://example.com/thread/1</link>
            </item>
          </channel>
        </rss>"""
        feed = parse_feed(xml)
        md = format_feed_as_markdown(feed)
        assert "1. Thread title" in md
        assert "example.com" in md
        # No author/date/comments metadata line
        assert "|" not in md


# ---------------------------------------------------------------------------
# Forum HTML Detection Tests
# ---------------------------------------------------------------------------


class TestIsForumHtml:
    """Tests for is_forum_html."""

    def test_xenforo_2x_detected(self):
        html = '<html id="XF"><head></head><body>content</body></html>'
        soup = BeautifulSoup(html, "lxml")
        assert is_forum_html(soup) is True

    def test_xenforo_1x_detected(self):
        """XenForo 1.x uses id='XenForo' instead of id='XF'."""
        html = '<html id="XenForo"><head></head><body>content</body></html>'
        soup = BeautifulSoup(html, "lxml")
        assert is_forum_html(soup) is True

    def test_vbulletin_detected(self):
        html = '<html><head><meta name="generator" content="vBulletin 3.8.11"></head><body></body></html>'
        soup = BeautifulSoup(html, "lxml")
        assert is_forum_html(soup) is True

    def test_discourse_not_in_is_forum_html(self):
        """Discourse is handled separately by is_discourse_html(), not is_forum_html()."""
        html = '<html><head><meta name="generator" content="Discourse 3.1.0"></head><body></body></html>'
        soup = BeautifulSoup(html, "lxml")
        assert is_forum_html(soup) is False

    def test_phpbb_detected(self):
        """phpBB detected via element with phpbb in id."""
        html = '<html><head></head><body><div id="phpbb_alert">alert</div><p>content</p></body></html>'
        soup = BeautifulSoup(html, "lxml")
        assert is_forum_html(soup) is True

    def test_phpbb_powered_by_detected(self):
        """phpBB detected via 'Powered by phpBB' footer text."""
        html = '<html><head></head><body><p>content</p><p>Powered by phpBB &copy; phpBB Group</p></body></html>'
        soup = BeautifulSoup(html, "lxml")
        assert is_forum_html(soup) is True

    def test_non_forum_not_detected(self):
        html = "<html><head><title>Blog</title></head><body><p>Hello</p></body></html>"
        soup = BeautifulSoup(html, "lxml")
        assert is_forum_html(soup) is False


# ---------------------------------------------------------------------------
# Autodiscovery Tests
# ---------------------------------------------------------------------------


class TestDiscoverFeedUrl:
    """Tests for discover_feed_url."""

    def test_rss_link_found(self):
        html = """<html><head>
            <link rel="alternate" type="application/rss+xml" href="/forums/my-forum.5/index.rss">
        </head><body></body></html>"""
        url = discover_feed_url(html, "https://example.com/forums/my-forum.5/")
        assert url == "https://example.com/forums/my-forum.5/index.rss"

    def test_atom_link_found(self):
        html = """<html><head>
            <link rel="alternate" type="application/atom+xml" href="/feed/forum/5">
        </head><body></body></html>"""
        url = discover_feed_url(html, "https://example.com/forum/5")
        assert url == "https://example.com/feed/forum/5"

    def test_prefers_rss_over_atom(self):
        html = """<html><head>
            <link rel="alternate" type="application/atom+xml" href="/atom.xml">
            <link rel="alternate" type="application/rss+xml" href="/rss.xml">
        </head><body></body></html>"""
        url = discover_feed_url(html, "https://example.com/")
        assert url == "https://example.com/rss.xml"

    def test_no_link_returns_none(self):
        html = "<html><head><title>No feeds</title></head><body></body></html>"
        assert discover_feed_url(html, "https://example.com/") is None

    def test_cross_domain_rejected(self):
        html = """<html><head>
            <link rel="alternate" type="application/rss+xml" href="https://evil.com/feed.rss">
        </head><body></body></html>"""
        assert discover_feed_url(html, "https://example.com/") is None

    def test_relative_url_resolved(self):
        html = """<html><head>
            <link rel="alternate" type="application/rss+xml" href="index.rss">
        </head><body></body></html>"""
        url = discover_feed_url(html, "https://example.com/forums/test.5/")
        assert url == "https://example.com/forums/test.5/index.rss"


# ---------------------------------------------------------------------------
# Discourse Detection Tests
# ---------------------------------------------------------------------------


class TestIsDiscourseHtml:
    """Tests for is_discourse_html."""

    def test_discourse_detected_separately(self):
        """is_discourse_html() returns True for Discourse meta generator."""
        html = '<html><head><meta name="generator" content="Discourse 3.1.0"></head><body></body></html>'
        soup = BeautifulSoup(html, "lxml")
        assert is_discourse_html(soup) is True

    def test_non_discourse_not_detected(self):
        html = '<html><head><meta name="generator" content="WordPress 6.0"></head><body></body></html>'
        soup = BeautifulSoup(html, "lxml")
        assert is_discourse_html(soup) is False

    def test_no_meta_generator(self):
        html = "<html><head><title>Blog</title></head><body><p>Hello</p></body></html>"
        soup = BeautifulSoup(html, "lxml")
        assert is_discourse_html(soup) is False
