"""Forum feed detection, URL transforms, RSS/Atom parsing, and generic forum cleanup.

Supports XenForo, vBulletin, phpBB, and Discourse forums. Automatically
transforms forum listing/thread URLs to RSS/Atom feeds for structured output.

Exports the standard site interface (SELECTORS_LIST, is_forum_html,
strip_forum_junk, postprocess_forum) plus forum-specific helpers
(transform_forum_url, parse_feed, format_feed_as_markdown, discover_feed_url).
"""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from html import unescape as _html_unescape
from urllib.parse import parse_qs, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

_MAX_FEED_ITEMS = 250
_MAX_FEED_TITLE_CHARS = 512
_MAX_FEED_LINK_CHARS = 8_192
_MAX_FEED_AUTHOR_CHARS = 256
_MAX_FEED_CATEGORY_CHARS = 256
_MAX_FEED_CATEGORIES = 20
_MAX_FEED_DESCRIPTION_CHARS = 1_000

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ForumTransformResult:
    """Result of forum URL transformation."""

    url: str  # Transformed URL (feed or original)
    is_forum_feed: bool  # True if transformed to feed URL
    original_url: str  # Original URL for [Feed: ...] note
    forum_software: str | None  # "xenforo", "vbulletin", "phpbb", "discourse"
    is_thread: bool = False  # True if URL is a known thread (skip autodiscovery)


@dataclass
class FeedItem:
    """A single item from an RSS/Atom feed."""

    title: str
    link: str
    author: str | None = None
    published: str | None = None  # Human-readable date
    comment_count: str | None = None  # From slash:comments
    categories: list[str] = field(default_factory=list)
    description: str | None = None  # Plain text preview


@dataclass
class ParsedFeed:
    """Parsed RSS/Atom feed."""

    title: str | None
    link: str | None
    description: str | None
    items: list[FeedItem]
    feed_type: str  # "rss2" or "atom"


# ---------------------------------------------------------------------------
# Known forum domains → software mapping
# ---------------------------------------------------------------------------

# hostname → software type
_KNOWN_DOMAINS: dict[str, str] = {
    # BimmerPost (vBulletin) — multiple subdomains
    "u11.bimmerpost.com": "vbulletin",
    "f10.bimmerpost.com": "vbulletin",
    "f30.bimmerpost.com": "vbulletin",
    "f80.bimmerpost.com": "vbulletin",
    "f87.bimmerpost.com": "vbulletin",
    "g20.bimmerpost.com": "vbulletin",
    "g80.bimmerpost.com": "vbulletin",
    "g05.bimmerpost.com": "vbulletin",
    # XenForo sites
    "www.avsforum.com": "xenforo",
    "www.vwvortex.com": "xenforo",
    "www.golfmk7.com": "xenforo",
    "www.rdforum.org": "xenforo",
    # phpBB (RedFlagDeals)
    "forums.redflagdeals.com": "phpbb",
}

# Wildcard subdomains: suffix → software
_KNOWN_SUFFIXES: dict[str, str] = {
    ".bimmerpost.com": "vbulletin",
}


def _get_forum_software(hostname: str) -> str | None:
    """Look up forum software by hostname."""
    hostname = hostname.lower()
    sw = _KNOWN_DOMAINS.get(hostname)
    if sw:
        return sw
    for suffix, sw in _KNOWN_SUFFIXES.items():
        if hostname.endswith(suffix):
            return sw
    return None


# ---------------------------------------------------------------------------
# URL Transform — Tier 1 (known domain patterns)
# ---------------------------------------------------------------------------

# vBulletin listing: forumdisplay.php?f={id}
_VB_LISTING_RE = re.compile(r"/forumdisplay\.php\b")
# vBulletin thread URLs (skip — external.php doesn't support thread-specific feeds)
_VB_THREAD_RE = re.compile(r"/showthread\.php\b")

# XenForo listing (clean URL): /forums/{slug}.{id}/
# Slug can contain dots (e.g. "radar-detectors-tech.rates") — use greedy match
# up to the last dot before the numeric ID
_XF_LISTING_RE = re.compile(r"/forums/(.+)\.(\d+)/?$")
# XenForo listing (index.php): index.php?forums/{slug}.{id}/
_XF_LISTING_QS_RE = re.compile(r"forums/(.+)\.(\d+)/?$")
# XenForo thread URLs (skip — thread RSS is disabled on most XenForo installs)
_XF_THREAD_RE = re.compile(r"/threads/")

# phpBB (RFD) listing: /{slug}-f{id}/
_PHPBB_RFD_LISTING_RE = re.compile(r"/([a-z0-9-]+)-f(\d+)/?$")
# phpBB (RFD) thread: /{slug}-t{id}.html or /{slug}-{id}/ (slug ending in digits)
_PHPBB_RFD_THREAD_RE = re.compile(r"/[a-z0-9-]+-(?:t\d+\.html|\d+/?)$")

# Discourse listing: /c/{slug}/{id}
_DISCOURSE_LISTING_RE = re.compile(r"/c/([a-z0-9-]+)/(\d+)/?$")
# Discourse thread: /t/{slug}/{id}
_DISCOURSE_THREAD_RE = re.compile(r"/t/([a-z0-9-]+)/(\d+)/?(?:\d+)?$", re.IGNORECASE)

# Already a feed URL
_FEED_URL_RE = re.compile(
    r"(?:\.rss$|/index\.rss|/feed/|external\.php\?.*type=RSS|\.atom$)",
    re.IGNORECASE,
)


def is_thread_url(url: str) -> bool:
    """Check if a URL looks like a forum thread (any software).

    Used by Tier 2 autodiscovery on unknown domains to avoid hijacking
    thread pages with feed content.
    """
    try:
        path = urlparse(url).path
    except Exception:
        return False
    return bool(
        _XF_THREAD_RE.search(path)
        or _VB_THREAD_RE.search(path)
        or _DISCOURSE_THREAD_RE.search(path)
        or _PHPBB_RFD_THREAD_RE.search(path)
    )


def transform_forum_url(url: str) -> ForumTransformResult:
    """Transform known forum URLs to their RSS/Atom feed equivalents.

    Handles both listing pages (forum indexes) and thread pages.
    Unknown URLs pass through unchanged.
    """
    passthrough = ForumTransformResult(url=url, is_forum_feed=False, original_url=url, forum_software=None)

    try:
        parsed = urlparse(url)
    except Exception:
        return passthrough

    hostname = (parsed.hostname or "").lower()
    software = _get_forum_software(hostname)
    if not software:
        return passthrough

    path = parsed.path
    query = parsed.query

    # Already a feed URL — passthrough with metadata
    if _FEED_URL_RE.search(path + "?" + query):
        return ForumTransformResult(url=url, is_forum_feed=True, original_url=url, forum_software=software)

    # --- vBulletin ---
    if software == "vbulletin":
        # Thread URLs: skip (external.php doesn't support thread-specific feeds)
        if _VB_THREAD_RE.search(path):
            return ForumTransformResult(
                url=url,
                is_forum_feed=False,
                original_url=url,
                forum_software="vbulletin",
                is_thread=True,
            )

        # Listing: forumdisplay.php?f={id}
        if _VB_LISTING_RE.search(path):
            qs = parse_qs(query)
            forum_id = qs.get("f", [None])[0]
            if forum_id:
                feed_url = urlunparse(
                    (
                        parsed.scheme,
                        parsed.netloc,
                        "/forums/external.php",
                        "",
                        f"type=RSS2&forumids={forum_id}",
                        "",
                    )
                )
                return ForumTransformResult(
                    url=feed_url,
                    is_forum_feed=True,
                    original_url=url,
                    forum_software="vbulletin",
                )

    # --- XenForo ---
    if software == "xenforo":
        # Thread URLs: skip (thread RSS is disabled on most XenForo installs)
        if _XF_THREAD_RE.search(path):
            return ForumTransformResult(
                url=url,
                is_forum_feed=False,
                original_url=url,
                forum_software="xenforo",
                is_thread=True,
            )
        if "index.php" in path and query and "threads/" in query:
            return ForumTransformResult(
                url=url,
                is_forum_feed=False,
                original_url=url,
                forum_software="xenforo",
                is_thread=True,
            )

        # Listing (clean URL): /forums/{slug}.{id}/
        m = _XF_LISTING_RE.search(path)
        if m:
            slug, fid = m.group(1), m.group(2)
            feed_path = f"/forums/{slug}.{fid}/index.rss"
            feed_url = urlunparse((parsed.scheme, parsed.netloc, feed_path, "", "", ""))
            return ForumTransformResult(
                url=feed_url,
                is_forum_feed=True,
                original_url=url,
                forum_software="xenforo",
            )

        # Listing (index.php query): index.php?forums/{slug}.{id}/
        if "index.php" in path and query:
            m = _XF_LISTING_QS_RE.search(query)
            if m:
                slug, fid = m.group(1), m.group(2)
                new_query = f"forums/{slug}.{fid}/index.rss"
                feed_url = urlunparse((parsed.scheme, parsed.netloc, path, "", new_query, ""))
                return ForumTransformResult(
                    url=feed_url,
                    is_forum_feed=True,
                    original_url=url,
                    forum_software="xenforo",
                )

    # --- phpBB (RedFlagDeals) ---
    if software == "phpbb":
        # Listing: /{slug}-f{id}/
        m = _PHPBB_RFD_LISTING_RE.search(path)
        if m:
            forum_id = m.group(2)
            feed_url = urlunparse((parsed.scheme, parsed.netloc, f"/feed/forum/{forum_id}", "", "", ""))
            return ForumTransformResult(
                url=feed_url,
                is_forum_feed=True,
                original_url=url,
                forum_software="phpbb",
            )
        # Thread/topic pages: pass through (redflagdeals.py handles HTML cleanup)
        if _PHPBB_RFD_THREAD_RE.search(path):
            return ForumTransformResult(
                url=url,
                is_forum_feed=False,
                original_url=url,
                forum_software="phpbb",
                is_thread=True,
            )

    # --- Discourse ---
    if software == "discourse":
        # Thread: /t/{slug}/{id}
        m = _DISCOURSE_THREAD_RE.search(path)
        if m:
            slug, tid = m.group(1), m.group(2)
            feed_url = urlunparse((parsed.scheme, parsed.netloc, f"/t/{slug}/{tid}.rss", "", "", ""))
            return ForumTransformResult(
                url=feed_url,
                is_forum_feed=True,
                original_url=url,
                forum_software="discourse",
            )

        # Listing: /c/{slug}/{id}
        m = _DISCOURSE_LISTING_RE.search(path)
        if m:
            slug, cid = m.group(1), m.group(2)
            feed_url = urlunparse((parsed.scheme, parsed.netloc, f"/c/{slug}/{cid}.rss", "", "", ""))
            return ForumTransformResult(
                url=feed_url,
                is_forum_feed=True,
                original_url=url,
                forum_software="discourse",
            )

    # No transform matched — return with software info for Tier 2
    return ForumTransformResult(url=url, is_forum_feed=False, original_url=url, forum_software=software)


# ---------------------------------------------------------------------------
# Feed Parser (stdlib XML — no new dependencies)
# ---------------------------------------------------------------------------

# XML namespaces used by forum feeds
_NS = {
    "dc": "http://purl.org/dc/elements/1.1/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "slash": "http://purl.org/rss/1.0/modules/slash/",
    "atom": "http://www.w3.org/2005/Atom",
}

# Strip HTML tags for plain-text preview
_HTML_TAG_RE = re.compile(r"<[^>]+>")
# Collapse whitespace
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_html(text: str) -> str:
    """Strip HTML tags, decode HTML entities, and collapse whitespace."""
    text = _HTML_TAG_RE.sub(" ", text)
    text = _html_unescape(text)
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip()


def _parse_rfc2822_date(date_str: str) -> str | None:
    """Parse RFC 2822 date to YYYY-MM-DD HH:MM."""
    try:
        dt = parsedate_to_datetime(date_str)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return None


def _parse_iso_date(date_str: str) -> str | None:
    """Parse ISO 8601 date to YYYY-MM-DD HH:MM."""
    try:
        # Handle Z suffix and +00:00
        date_str = date_str.replace("Z", "+00:00")
        from datetime import datetime

        dt = datetime.fromisoformat(date_str)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return None


def _text(el: ET.Element | None) -> str | None:
    """Get text content of an XML element, or None."""
    if el is None:
        return None
    return (el.text or "").strip() or None


def _bounded_feed_text(value: str | None, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    return value[:limit]


def parse_feed(xml_text: str) -> ParsedFeed | None:
    """Parse RSS 2.0 or Atom 1.0 feed XML.

    Returns ParsedFeed or None if the XML is not a valid feed.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag

    # RSS 2.0: <rss><channel><item>...
    if tag == "rss":
        return _parse_rss2(root)

    # Atom 1.0: <feed xmlns="http://www.w3.org/2005/Atom">
    if tag == "feed" or root.tag == f"{{{_NS['atom']}}}feed":
        return _parse_atom(root)

    # RDF/RSS 1.0: <rdf:RDF> containing <channel> and <item>
    if "RDF" in tag:
        return _parse_rss2(root, rdf=True)

    return None


def _parse_rss2(root: ET.Element, rdf: bool = False) -> ParsedFeed | None:
    """Parse RSS 2.0 (or RDF/RSS 1.0) feed."""
    channel = root.find("channel")
    if channel is None and not rdf:
        return None

    source = channel if channel is not None else root

    items = []
    # RSS 2.0: items inside <channel>; RDF: items at root level
    item_elements = source.findall("item")
    if not item_elements and rdf:
        item_elements = root.findall("item")

    for item_el in item_elements[:_MAX_FEED_ITEMS]:
        title = (
            _bounded_feed_text(
                _text(item_el.find("title")),
                _MAX_FEED_TITLE_CHARS,
            )
            or "(untitled)"
        )
        link = (
            _bounded_feed_text(
                _text(item_el.find("link")),
                _MAX_FEED_LINK_CHARS,
            )
            or ""
        )

        # Author: <dc:creator> or <author>
        author = _text(item_el.find(f"{{{_NS['dc']}}}creator"))
        if not author:
            author = _text(item_el.find("author"))
        author = _bounded_feed_text(author, _MAX_FEED_AUTHOR_CHARS)

        # Date: <pubDate>
        pub_date = _text(item_el.find("pubDate"))
        published = _parse_rfc2822_date(pub_date) if pub_date else None

        # Comment count: <slash:comments>
        comment_count = _text(item_el.find(f"{{{_NS['slash']}}}comments"))

        # Categories
        categories = [
            c.text.strip()[:_MAX_FEED_CATEGORY_CHARS]
            for c in item_el.findall("category")[:_MAX_FEED_CATEGORIES]
            if c.text and c.text.strip()
        ]

        # Description: prefer <content:encoded>, fall back to <description>
        desc_el = item_el.find(f"{{{_NS['content']}}}encoded")
        if desc_el is None or not desc_el.text:
            desc_el = item_el.find("description")
        description = None
        if desc_el is not None and desc_el.text:
            description = _strip_html(desc_el.text)[:_MAX_FEED_DESCRIPTION_CHARS]

        items.append(
            FeedItem(
                title=title,
                link=link,
                author=author,
                published=published,
                comment_count=comment_count,
                categories=categories,
                description=description,
            )
        )

    return ParsedFeed(
        title=_bounded_feed_text(
            _text(source.find("title")),
            _MAX_FEED_TITLE_CHARS,
        ),
        link=_bounded_feed_text(
            _text(source.find("link")),
            _MAX_FEED_LINK_CHARS,
        ),
        description=_bounded_feed_text(
            _text(source.find("description")),
            _MAX_FEED_DESCRIPTION_CHARS,
        ),
        items=items,
        feed_type="rss2",
    )


def _parse_atom(root: ET.Element) -> ParsedFeed:
    """Parse Atom 1.0 feed."""
    ns = _NS["atom"]

    def _find(el: ET.Element, tag: str) -> ET.Element | None:
        """Find child in both namespaced and bare forms."""
        result = el.find(f"{{{ns}}}{tag}")
        if result is None:
            result = el.find(tag)
        return result

    def _find_all(el: ET.Element, tag: str) -> list[ET.Element]:
        result = el.findall(f"{{{ns}}}{tag}")
        if not result:
            result = el.findall(tag)
        return result

    # Feed-level metadata
    title_el = _find(root, "title")
    feed_title = _text(title_el)

    link_el = _find(root, "link")
    feed_link = link_el.get("href") if link_el is not None else None

    subtitle_el = _find(root, "subtitle")
    feed_desc = _text(subtitle_el)

    items = []
    for entry in _find_all(root, "entry")[:_MAX_FEED_ITEMS]:
        title = (
            _bounded_feed_text(
                _text(_find(entry, "title")),
                _MAX_FEED_TITLE_CHARS,
            )
            or "(untitled)"
        )

        # Link: prefer rel="alternate", fall back to first <link>
        link = ""
        for link_el in _find_all(entry, "link"):
            href = link_el.get("href", "")
            rel = link_el.get("rel", "alternate")
            if rel == "alternate" and href:
                link = href
                break
            if href and not link:
                link = href
        link = link[:_MAX_FEED_LINK_CHARS]

        # Author
        author_el = _find(entry, "author")
        author = None
        if author_el is not None:
            name_el = _find(author_el, "name")
            author = _text(name_el)
        author = _bounded_feed_text(author, _MAX_FEED_AUTHOR_CHARS)

        # Date: <updated> or <published>
        updated = _text(_find(entry, "updated"))
        published_el = _text(_find(entry, "published"))
        date_str = updated or published_el
        published = _parse_iso_date(date_str) if date_str else None

        # Categories
        categories = []
        for cat in _find_all(entry, "category")[:_MAX_FEED_CATEGORIES]:
            term = cat.get("term", "").strip()
            if term:
                categories.append(term[:_MAX_FEED_CATEGORY_CHARS])

        # Content
        content_el = _find(entry, "content")
        if content_el is None:
            content_el = _find(entry, "summary")
        description = None
        if content_el is not None and content_el.text:
            description = _strip_html(content_el.text)[:_MAX_FEED_DESCRIPTION_CHARS]

        items.append(
            FeedItem(
                title=title,
                link=link,
                author=author,
                published=published,
                comment_count=None,  # Atom doesn't have slash:comments
                categories=categories,
                description=description,
            )
        )

    return ParsedFeed(
        title=_bounded_feed_text(feed_title, _MAX_FEED_TITLE_CHARS),
        link=_bounded_feed_text(feed_link, _MAX_FEED_LINK_CHARS),
        description=_bounded_feed_text(
            feed_desc,
            _MAX_FEED_DESCRIPTION_CHARS,
        ),
        items=items,
        feed_type="atom",
    )


# ---------------------------------------------------------------------------
# Feed Formatter
# ---------------------------------------------------------------------------


def format_feed_as_markdown(feed: ParsedFeed) -> str:
    """Format a parsed feed as a clean numbered markdown listing."""
    lines = []

    if feed.title:
        lines.append(f"# {feed.title}")
        lines.append("")

    if feed.description:
        lines.append(feed.description)
        lines.append("")

    if not feed.items:
        lines.append("(No items)")
        return "\n".join(lines)

    for i, item in enumerate(feed.items, 1):
        lines.append(f"{i}. {item.title}")

        # Metadata line: author | date | comments
        meta_parts = []
        if item.author:
            meta_parts.append(item.author)
        if item.published:
            meta_parts.append(item.published)
        if item.comment_count:
            meta_parts.append(f"{item.comment_count} comments")
        if meta_parts:
            lines.append(f"   {' | '.join(meta_parts)}")

        if item.link:
            lines.append(f"   {item.link}")

        if item.description:
            # Indent preview text as a blockquote
            lines.append(f"   > {item.description}")

        lines.append("")

    return "\n".join(lines).strip()


def parse_and_format_feed(xml_text: str) -> tuple[str, int] | None:
    """Parse and render a bounded feed inside one isolated operation."""

    feed = parse_feed(xml_text)
    if feed is None or not feed.items:
        return None
    return format_feed_as_markdown(feed), len(feed.items)


# ---------------------------------------------------------------------------
# Autodiscovery — Tier 2 (HTML <link rel="alternate">)
# ---------------------------------------------------------------------------

# Prefer RSS over Atom (more common for forums)
_FEED_TYPES = ("application/rss+xml", "application/atom+xml")


def discover_feed_url(html: str, page_url: str) -> str | None:
    """Discover RSS/Atom feed URL from HTML <link> tags.

    Returns absolute feed URL or None. Rejects cross-domain links (SSRF).
    """
    soup = BeautifulSoup(html, "lxml")
    page_host = urlparse(page_url).hostname or ""

    for feed_type in _FEED_TYPES:
        link = soup.find("link", attrs={"rel": "alternate", "type": feed_type})
        if link and link.get("href"):
            feed_url = urljoin(page_url, link["href"])
            # Same-domain check (SSRF protection)
            feed_host = urlparse(feed_url).hostname or ""
            if feed_host.lower() == page_host.lower():
                return feed_url

    return None


# ---------------------------------------------------------------------------
# Forum HTML Detection (for Tier 2 / generic cleanup fallback)
# ---------------------------------------------------------------------------


def is_discourse_html(soup: BeautifulSoup) -> bool:
    """Detect Discourse from HTML meta generator tag.

    Discourse pages get their own site key ('discourse') in html.py so they
    only receive generic junk removal, not XenForo/vBulletin-focused cleanup.
    """
    meta = soup.find("meta", attrs={"name": "generator"})
    if meta and "discourse" in (meta.get("content") or "").lower():
        return True
    return False


def is_forum_html(soup: BeautifulSoup) -> bool:
    """Detect forum software from HTML markers.

    Used as a fallback in html.py when no URL-based site match is found.
    Returns True if XenForo, vBulletin, or phpBB is detected.
    Discourse is handled separately by is_discourse_html().
    """
    # XenForo 2.x: <html id="XF">, XenForo 1.x: <html id="XenForo">
    html_tag = soup.find("html")
    if html_tag and html_tag.get("id") in ("XF", "XenForo"):
        return True

    # vBulletin: <meta name="generator" content="vBulletin ...">
    meta = soup.find("meta", attrs={"name": "generator"})
    if meta:
        content = (meta.get("content") or "").lower()
        if "vbulletin" in content:
            return True

    # phpBB: class or id containing "phpbb", or "Powered by phpBB" text
    body = soup.find("body")
    if body:
        body_id = (body.get("id") or "").lower()
        body_classes = " ".join(body.get("class") or []).lower()
        if "phpbb" in body_id or "phpbb" in body_classes:
            return True
    if soup.find(id=re.compile(r"phpbb", re.IGNORECASE)):
        return True
    powered = soup.find(string=re.compile(r"Powered by phpBB", re.IGNORECASE))
    if powered:
        return True

    return False


# ---------------------------------------------------------------------------
# Generic forum CSS selectors (conservative thread cleanup)
# ---------------------------------------------------------------------------

SELECTORS_LIST = [
    # ---- XenForo ----
    # Hidden SEO/social preview elements (California theme duplicate OP)
    ".california-thread-body-container > .hidden",
    # California theme: OP thread card header (category name, view/reply stats)
    # Only target the original post, not reply headers (which have useful author info)
    ".MessageCard__container__original-post .MessageCard__header",
    # California theme: collapse/expand buttons and collapsed-state preview image
    ".MessageCard__collapse-link",
    ".MessageCard__content-preview-image",
    # California theme: toggle-replies buttons ("Show more replies", "N Reply/Replies")
    ".toggle-replies-button",
    # Page structure
    ".p-nav",
    ".p-navSticky",
    ".p-header",
    ".p-footer",
    ".p-breadcrumbs",
    ".p-body-sidebar",
    ".p-body-sidebarCol",
    # User info column: handled in strip_forum_junk to preserve username link
    # (classic XenForo renders usernames only inside .message-cell--user on
    #  paginated pages; California theme uses MessageCard__user-info instead)
    # Signatures
    ".message-signature",
    # Action bars per post (reply, quote, like, react, bookmark)
    ".message-actionBar",
    ".actionBar",
    ".reactionsBar",
    ".reactionSummary",
    # Post metadata / decoration
    ".message-userArrow",
    ".message-attribution-opposite",
    ".message-footer",
    ".message-newIndicator",
    ".message-lastEdit",
    # Pagination
    ".pageNavWrapper",
    ".pageNav",
    ".pageNavSimple",
    # Blocks (similar threads, outer wrappers)
    ".block-outer--after",
    ".block--similarThreads",
    # Share buttons
    ".shareButtons",
    # Notices & browser warnings
    ".js-notices",
    ".notices",
    # App install prompts
    ".offCanvasMenu-installBanner",
    # Expand link for code/quote blocks
    ".bbCodeBlock-expandLink",
    # Ads — XenForo California theme (VWVortex)
    "[class*='california-ad']",
    "[class*='california-banner-ad']",
    "[class*='california-sidebar-ad']",
    "[class*='california-sticky-footer-ad']",
    "[class*='california-thread-ad']",
    ".california-recommended-reading-container",
    ".related-threads-ad-container",
    ".sticky-block",
    # ---- XenForo 1.x (TacomaWorld, older installs) ----
    # User info block (left side of each post — avatar, name, title, badges)
    ".messageUserBlock",
    ".messageUserInfo",
    # Action buttons per post (reply, quote, like)
    ".publicControls",
    ".privateControls",
    # Navigation & breadcrumbs
    "#navigation",
    ".breadBoxTop",
    ".breadBoxBottom",
    # Product recommendation ads (TacomaWorld: tires, parts, Amazon links)
    "[class*='relProd']",
    # Search bar / quick search
    "#searchBar",
    "#QuickSearch",
    # Login bar
    "#loginBar",
    # Login/signup forms
    ".xenForm#login",
    # Notice panels
    ".PanelScrollerOff#Notices",
    # Pagination (XF1)
    ".pageNavLinkGroup",
    ".PageNav",
    # Sidebar
    ".sidebar",
    # ---- vBulletin / InternetBrands ----
    # InternetBrands platform overlay (Rennlist, CorvetteForum, etc.)
    "header.has-nav",
    "#darkmode_option",
    ".toolbox",
    ".top-nav",
    # Navigation & login
    ".mainnavbar",
    ".bplogin",
    "#hamburger",
    "#hamitems",
    "#hamnav",
    # Navbar / menus
    "#navbar",
    ".vbmenu_popup",
    # User profile scores & badges in posts
    ".postBitScoreItem",
    ".postBitScoreGraphContainer",
    ".postBitScoreGraph",
    # Garage / car list in user profiles
    ".garagelist",
    # Signatures
    ".fixedsig",
    # Action buttons per post (Appreciate, Tweet, Quote)
    "#leftControls",
    ".postBotBarItem",
    ".postBotBarItemNoBorder",
    ".postBitActionFrame",
    ".postbitbuttons",
    # Post header rows (date, post number)
    ".thead",
    # Quick reply
    "#qr_open",
    # Pagination
    ".pagenav",
    # Breadcrumbs
    "#breadcrumb",
    # Forum jump selector
    ".forumjump",
    # Social widget containers
    "#fb-root",
    # Online/offline status
    ".isOffline",
    ".isOnline",
    # ---- Discourse ----
    ".topic-footer-buttons",
    ".signup-cta",
    "#topic-progress",
    ".topic-map",
    # ---- phpBB ----
    "#phpbb_alert",  # JS modal dialog
    "#phpbb_confirm",  # JS confirmation dialog
    "#darken",  # Modal backdrop
    "#darkenwrapper",  # Modal wrapper
    "dl.post_voting",  # Voting markup (definition list)
    ".post_profilearea",  # User profile in posts
    ".post_dateline",  # Post date/time
    ".actionbar",  # Post action buttons (reply, quote, etc.)
    ".pagination",  # Pagination controls
    "#page-footer",  # Page footer
    ".notice",  # System notices
]


# ---------------------------------------------------------------------------
# Soup-level cleanup
# ---------------------------------------------------------------------------


# YouTube/video embed URL patterns → canonical watch URL
def strip_forum_junk(soup: BeautifulSoup) -> None:
    """Remove forum-specific junk that CSS selectors can't easily catch."""
    # Quick-reply / reply forms
    for form in list(soup.find_all("form")):
        action = (form.get("action") or "").lower()
        if any(kw in action for kw in ("reply", "newreply", "post")):
            form.decompose()

    # Avatar images (XenForo + vBulletin)
    for img in list(soup.find_all("img")):
        src = img.get("src", "")
        classes = " ".join(img.get("class") or [])
        # XenForo: /data/avatars/ paths or avatar class
        if "/avatars/" in src or "avatar" in classes:
            img.decompose()
            continue
        # vBulletin: /customavatars/ paths
        if "/customavatars/" in src:
            img.decompose()
            continue
        # XenForo cross-login tracking images (GolfMK7 uses these)
        if "xcmss/cross-login" in src or "xcmss/sitemap" in src:
            img.decompose()
            continue

    # vBulletin: user profile sidebar TDs (width=175, class=alt2, first in row)
    for td in list(soup.find_all("td", class_="alt2")):
        width = td.get("width", "")
        if width and "175" in str(width):
            td.decompose()
            continue

    # XenForo: "Install the app" instruction blocks
    for div in list(soup.find_all(class_=lambda c: c and any("installPrompt" in cls for cls in c))):
        div.decompose()

    # XenForo: browser warning blocks
    for div in list(soup.find_all(class_=lambda c: c and any("browserWarning" in cls for cls in c))):
        div.decompose()

    # XenForo: "Similar threads" / related content blocks (specific class)
    for div in list(
        soup.find_all(class_=lambda c: c and any(kw in cls for cls in c for kw in ("similarThreads", "relatedThreads")))
    ):
        div.decompose()

    # XenForo: "Similar threads" blocks identified by heading text
    for heading in list(soup.find_all(["h2", "h3", "h4"])):
        text = heading.get_text(strip=True).lower()
        if text in ("similar threads", "related threads"):
            block = heading.find_parent(class_="block")
            if block:
                block.decompose()
            else:
                # Remove just the heading's parent container
                parent = heading.parent
                if parent and parent.name == "div":
                    parent.decompose()

    # XenForo: off-canvas menu elements (Mobile "Navigation", "More options")
    for div in list(soup.find_all(class_=lambda c: c and any("offCanvasMenu" in cls for cls in c))):
        div.decompose()

    # vBulletin: small navigation images (house icon, etc.)
    for img in list(soup.find_all("img")):
        src = img.get("src", "")
        if "house.png" in src or "icon_" in src:
            # If wrapped in a link, remove the link too
            parent = img.parent
            if parent and parent.name == "a":
                parent.decompose()
            else:
                img.decompose()

    # XenForo: banner/logo images (large site banners)
    for img in list(soup.find_all("img")):
        alt = (img.get("alt") or "").lower()
        if "banner" in alt or "logo" in alt:
            parent = img.parent
            if parent and parent.name == "a":
                parent.decompose()
            else:
                img.decompose()

    # XenForo classic: slim .message-cell--user to just the username link.
    # On paginated pages, this cell is the ONLY place the author name appears.
    # (California theme uses MessageCard__user-info instead, which is preserved.)
    for cell in list(soup.select(".message-cell--user")):
        name_link = cell.select_one(".message-name a.username, .message-name a")
        if name_link:
            name_link = name_link.extract()
            wrapper = soup.new_tag("div")
            wrapper.append(name_link)
            cell.replace_with(wrapper)
        else:
            cell.decompose()

    # XenForo/VerticalScope: unwrap PhraseLinker auto-affiliate links.
    # These are server-injected <a> tags wrapping common product terms
    # (e.g. "dolby atmos" → <a href="https://amazon.com/s?k=Dolby&tag=vs-phraselinker-20">dolby atmos</a>).
    # Replace with just the link text to avoid token-wasting affiliate noise.
    for a_tag in list(soup.find_all("a")):
        href = a_tag.get("href", "")
        if "tag=vs-phraselinker-20" in href:
            a_tag.replace_with(a_tag.get_text())

    # XenForo: strip smiley <img> tags (class="smilie" or data-shortname).
    # These render as `:ROFLMAO:`, `:cool:`, etc. in markdown alt text.
    # Remove them entirely — they're noise for LLM consumption.
    for img in list(soup.find_all("img", class_="smilie")):
        img.decompose()
    # Some XenForo installs use data-shortname instead of class="smilie"
    for img in list(soup.find_all("img", attrs={"data-shortname": True})):
        if "smilie" in (img.get("class") or []) or img.get("data-shortname"):
            img.decompose()
    # Old-style smiley images (pre-XenForo 2.x): no class/data-shortname,
    # only identifiable by src path containing /smilies/ (e.g.
    # files.avsforum.com/images/smilies/smile.gif, avs-vb/images/smilies/wink.gif).
    # Also strip gallery thumbnail duplicates (full-size images already present).
    # Check data-src too — lazy-loaded images won't have src yet.
    for img in list(soup.find_all("img")):
        src = img.get("src") or img.get("data-src") or ""
        if "/smilies/" in src:
            img.decompose()
            continue
        if "attachment_gallery_thumbnails" in src:
            img.decompose()

    # XenForo: strip [View attachment XXXXXX] links inside blockquotes.
    # When a post with images is quoted, images become useless
    # [View attachment 1234](url) references.
    for bq in soup.find_all("blockquote"):
        for a_tag in list(bq.find_all("a")):
            text = a_tag.get_text(strip=True)
            if re.match(r"View attachment \d+", text):
                a_tag.decompose()

    # XenForo: slim down link unfurl cards (bbCodeBlock--unfurl).
    # Keep title link + description, remove favicon image and bare domain name.
    for unfurl in list(soup.select(".bbCodeBlock--unfurl")):
        # Remove favicon row: <span class="contentRow-figure"> containing the favicon <img>
        for fig in list(unfurl.select(".contentRow-figure")):
            fig.decompose()
        # Remove the bare domain <span class="contentRow-minor">
        for minor in list(unfurl.select(".contentRow-minor")):
            minor.decompose()

    # XenForo: remove "Discussion starter" badge and its adjacent dot-separator
    # from .message-attribution-main (the post number/date area)
    for el in list(soup.select(".message-attribution-discussion-starter")):
        next_sib = el.find_next_sibling()
        if next_sib and "dot-separator" in (next_sib.get("class") or []):
            next_sib.decompose()
        el.decompose()

    # vBulletin: badge images, rank images, and status icons
    # (InternetBrands forums like CorvetteForum, Rennlist)
    # These are in generic <div> tags with no useful class, so CSS selectors can't target them
    for img in list(soup.find_all("img")):
        src = img.get("src", "")
        if any(p in src for p in ("images/badges", "images/ranks", "statusicon/", "icons/icon")):
            img.decompose()

    # vBulletin: remove social sharing bookmarks (Submit Thread to twitter/Facebook)
    meta = soup.find("meta", attrs={"name": "generator"})
    if meta and "vbulletin" in (meta.get("content") or "").lower():
        for img in list(soup.find_all("img")):
            src = img.get("src", "")
            if "bookmarksite" in src:
                # Find containing list or table
                container = img.find_parent("ul") or img.find_parent("table") or img.find_parent("div")
                if container:
                    container.decompose()
                    break  # All sharing links are in one container

        # Remove permissions block ("You may not post new threads")
        # Text is split across HTML tags (You <b>may not</b> post), so use get_text()
        for td in list(soup.find_all("td")):
            text = td.get_text(separator=" ", strip=True)
            if "You may not post new threads" in text:
                container = td.find_parent("table") or td
                container.decompose()
                break

        # Remove "Contact Us" footer table
        for a in list(soup.find_all("a", string=re.compile(r"Contact Us"))):
            container = a.find_parent("table") or a.find_parent("div")
            if container:
                container.decompose()
                break

        # Remove "Post Reply" / "New Reply" links (top and bottom of thread)
        for a in list(soup.find_all("a", href=lambda h: h and "newreply" in h.lower())):
            container = a.find_parent("td") or a.find_parent("div")
            if container:
                container.decompose()

        # Remove "Thread Tools" dropdown wrapper
        for a in list(soup.find_all("a")):
            text = a.get_text(strip=True)
            if text == "Thread Tools":
                container = a.find_parent("td") or a.find_parent("div")
                if container:
                    container.decompose()

        # Remove top nav bar table (Garage, Meets, Register, Today's Posts, Search)
        # Target: tables containing register.php + search.php links together
        for table in list(soup.find_all("table")):
            links = [(a.get("href") or "") for a in table.find_all("a", href=True)]
            link_str = " ".join(links)
            if "register.php" in link_str and "search.php" in link_str:
                table.decompose()
                break

        # Unwrap post body table layout AFTER removing junk tables above.
        # vBulletin wraps each post in <table><tr><td class="alt1/alt2">.
        # Convert remaining post-content TDs to divs, then unwrap tables.
        for td in list(soup.find_all("td", class_=re.compile(r"^alt[12]$"))):
            td.name = "div"
            if "class" in td.attrs:
                del td["class"]
        for tag_name in ["table", "tbody", "thead", "tfoot", "tr", "th"]:
            for el in list(soup.find_all(tag_name)):
                el.unwrap()


# ---------------------------------------------------------------------------
# Markdown post-processing (minimal — CSS does most of the work)
# ---------------------------------------------------------------------------

# Pre-compiled regex
_EXCESSIVE_NEWLINES = re.compile(r"\n{3,}")

# ---- XenForo patterns ----

# "Menu\n\n[Log in](/login/)\n\n---\n\n[Sign up](/register/)" or "Register"
_XF_MENU_LOGIN_RE = re.compile(
    r"(?:^|\n)Menu\n+\[Log in\]\([^\)]+\)\n+---\n+\[(?:Sign up|Register)\]\([^\)]+\)\n*---\n?"
)

# "[Jump to Latest](/threads/.../latest)" link
_XF_JUMP_LATEST_RE = re.compile(r"(?:^|\n)\[Jump to Latest\]\([^\)]+\)(?:\n|$)")

# Share buttons: "Share:\n\nFacebook\nTwitter\nReddit..."
_XF_SHARE_RE = re.compile(
    r"(?:^|\n)Share:\n+(?:(?:Facebook|Twitter|Reddit|Pinterest|Tumblr|WhatsApp|Email|Share|Link)\n?)+"
)

# "Sort by\n\nOldest first\n\n[Oldest first](...)\n[Newest first](...)\n..."
_XF_SORT_BY_RE = re.compile(
    r"(?:^|\n)Sort by\n+(?:Oldest first|Newest first|Most reactions)"
    r"(?:\n+\[(?:Oldest first|Newest first|Most reactions)\]\([^\)]+\))*\n?"
)

# Related Threads block: "Related Threads\n\n1. ?\n2. ?\n..."
_XF_RELATED_THREADS_RE = re.compile(r"(?:^|\n)Related Threads\n+(?:\d+\.\s*\?\n?)+")

# Site stats: "posts\n:\s+\d+[KMB]?\n\nmembers\n:\s+..."
_XF_SITE_STATS_RE = re.compile(
    r"(?:^|\n)(?:[A-Za-z ]+\n:\s+[\d.,]+[KMB]?\n+){2,}"
    r"(?:Since\n:\s+\d+\n?)?"
)

# "[Full Forum Listing](/forums/)" or "Show Less" / "See less" / "See more"
_XF_FULL_LISTING_RE = re.compile(r"(?:^|\n)\[Full Forum Listing\]\([^\)]+\)(?:\n|$)")
_XF_SHOW_LESS_RE = re.compile(r"(?:^|\n)Show Less(?:\n|$)")
_XF_SEE_LESS_MORE_RE = re.compile(r"(?:^|\n)See (?:less|more)(?:\n|$)")

# "Add to quote" link: "- [Add to quote](/threads/.../reply?quote=...)"
_XF_ADD_QUOTE_RE = re.compile(r"(?:^|\n)-?\s*\[Add to quote\]\([^\)]+\)(?:\n|$)")

# Thread metadata: "- Thread starter\n  [user](/members/...)\n- Start date\n  ..."
_XF_THREAD_META_RE = re.compile(
    r"(?:^|\n)(?:-\s*)?Thread starter\n\s*\[.*?\]\([^\)]+\)\n"
    r"(?:-\s*)?Start date\n\s*\[.*?\]\([^\)]+\)"
    r"(?:\n(?:-\s*)?Tagged users\n\s*None)?\n?"
)

# Post attribution: "Discussion starter\n\nN posts\n·\n\nJoined YYYY"
_XF_POST_ATTRIB_RE = re.compile(
    r"(?:^|\n)(?:Discussion starter[\s\n]+)?[\d,]+[\s,]*posts?[\s\n]+·[\s\n]+Joined \d+(?:\n|$)"
)

# "You are using an out of date browser..." warning
_XF_BROWSER_WARNING_RE = re.compile(
    r"(?:^|\n)You are using an out of date browser[^\n]*\n"
    r"You should upgrade or use an \[alternative browser\]\([^\)]+\)\.?\n?"
)

# Views/replies/participants stats: "63\nviews\n\n3\nreplies\n\n3\nparticipants"
_XF_VIEWS_REPLIES_RE = re.compile(r"(?:^|\n)\d+\nviews\n+\d+\nrepl(?:y|ies)\n+\d+\nparticipants(?:\n|$)")

# "last post by\n[user](/members/...)\n \n1h ago" (may have whitespace-only lines)
_XF_LAST_POST_BY_RE = re.compile(r"(?:^|\n)last post by[\s\n]+\[.*?\]\([^\)]+\)[\s\n]+\d+\w?\s+ago(?:\n|$)")

# "Install the app\n\nInstall\n\nHow to install..."
_XF_INSTALL_APP_RE = re.compile(r"(?:^|\n)Install the app\n+Install(?:\n[\s\S]*?More options)?(?:\n|$)")

# "[Contact us](/misc/contact)\n\nClose Menu"
_XF_CONTACT_CLOSE_RE = re.compile(r"(?:^|\n)\[Contact us\]\([^\)]+\)\n+Close Menu(?:\n|$)")

# "- [New posts](/whats-new/posts/)\n- [Search forums](/search/..."
_XF_NAV_LINKS_RE = re.compile(r"(?:^|\n)(?:-\s*\[(?:New posts|Search forums|Media|Search media)\]\([^\)]+\)\n?)+")

# "How to install the app on iOS..." instruction block
_XF_INSTALL_HOWTO_RE = re.compile(
    r"(?:^|\n)How to install the app on iOS\n[\s\S]*?"
    r"(?:some browsers|More options)\.\n?"
)

# Cross-site nav links: "[GOLFMK8](//www.golfmk8.com/forums/) [GOLFMK7]..."
_XF_CROSSSITE_NAV_RE = re.compile(r"(?:^|\n)(?:\[GOLF(?:MK\d+|MKV)\]\(//[^\)]+\)\n?)+")

# "[7](/forums/index.php)" — bare numeral nav link
_XF_NUMERAL_NAV_RE = re.compile(r"(?:^|\n)\[\d+\]\(/forums/[^\)]*\)(?:\n|$)")

# "Install\n\nInstall" duplicate (from mobile overlay)
_XF_INSTALL_DUPE_RE = re.compile(r"(?:^|\n)Install(?:\n+Install)+(?:\n|$)")

# XenForo "#post-NNNN" or "#reply" anchors — transform to readable separators
_XF_POST_ANCHOR_RE = re.compile(r"(?:^|\n)\[#([\d,]+)\]\(#[^\)]*\)\n·\n\[([^\]]*)\]\(#[^\)]*\)(?:\n|$)")

# XenForo 1.x: "### [Log in or Sign up](login/)" header
_XF1_LOGIN_RE = re.compile(r"(?:^|\n)#{1,4}\s*\[Log in or Sign up\]\([^\)]+\)(?:\n|$)")

# XenForo 1.x: Search form remnants
_XF1_SEARCH_FORM_RE = re.compile(
    r"(?:^|\n)Search\n+:\s+-\s+Search titles only\n+"
    r"Posted by Member:\n+:\s+Separate names with a comma\.\n+"
    r"Newer Than:\n+[\s\S]*?\[More\.\.\.\]\([^\)]+\)(?:\n|$)"
)

# XenForo 1.x: "Post New Thread" link
_XF1_POST_NEW_RE = re.compile(r"(?:^|\n)\[Post New Thread\]\([^\)]+\)(?:\n|$)")

# XenForo 1.x: "Page N of N" pagination
_XF1_PAGE_OF_RE = re.compile(r"(?:^|\n)Page \d+ of \d+(?:\n|$)")

# XenForo 1.x: Sort by controls
_XF1_SORT_BY_RE = re.compile(
    r"(?:^|\n)Sort by:\n+:\s+\[[^\]]+\]\([^\)]+\)\n+"
    r"(?:\s+\[[^\]]+\]\([^\)]+\)\n+)*"
    r"(?::\s+\[[^\]]+\]\([^\)]+\)\n*)*"
)

# XenForo 1.x: "![To Top](...)" image
_XF1_TO_TOP_RE = re.compile(r"(?:^|\n)!\[To Top\]\([^\)]+\)(?:\n|$)")

# ---- vBulletin patterns ----

# Login block: "Login\n\nRemember Me?\n\n[Register](...)"
_VB_LOGIN_RE = re.compile(r"(?:^|\n)Login\n+Remember Me\?\n+\[Register\]\([^\)]+\)(?:\n|$)")

# Nav table: "| [BMW Garage](...) | [BMW Meets](...) | ... |"
_VB_NAV_TABLE_RE = re.compile(
    r"(?:^|\n)\|[^\n]*(?:Garage|Meets|Register|Today['\u2019]s Posts|Search)[^\n]*\|(?:\n|$)"
    r"(?:\| ---[^\n]*\|(?:\n|$))?"
)

# "| [Post Reply](...) |" and "| [Thread Tools](...) |"
_VB_POST_REPLY_RE = re.compile(r"(?:^|\n)\|[\s\xa0]*\[Post Reply\]\([^\)]+\)[\s\xa0]*\|(?:\n|$)")
_VB_THREAD_TOOLS_RE = re.compile(
    r"(?:^|\n)\|[\s\xa0]*\[?Thread Tools\]?(?:\([^\)]*\))?[\s\xa0]*\|(?:\n|$)"
    r"(?:\| ---[^\n]*\|(?:\n|$))?"
)

# Breadcrumb: "[![](...)  Bimmerpost](//www.bimmerpost.com/)"
_VB_LOGO_RE = re.compile(r"(?:^|\n)\[!\[\]\([^\)]+\)\n*Bimmerpost\]\([^\)]+\)(?:\n|$)")

# Social icons: "[![Facebook](...)](...) [![Twitter](...)](...)"
_VB_SOCIAL_RE = re.compile(r"(?:^|\n)(?:\[!\[(?:Facebook|Twitter|Instagram|YouTube)\]\([^\)]+\)\]\([^\)]+\)\n?)+")

# "❯  Thread Title" breadcrumb
_VB_BREADCRUMB_RE = re.compile(r"(?:^|\n)❯\s+[^\n]+(?:\n|$)")

# User profile in posts: "Registered\n\n0 Rep\n\n1 Posts" etc.
_VB_PROFILE_RE = re.compile(
    r"(?:^|\n)(?:Registered|Member|Enlisted Member|Senior Member|"
    r"Lieutenant General|Brigadier General|Major General|First Lieutenant|"
    r"Captain|Colonel|General|Private|Corporal|Sergeant|Staff Sergeant|"
    r"Master Sergeant|Warrant Officer|Second Lieutenant|"
    r"New Member|Active Member|Well-Known Member|Banned)\n"
)

# "N Rep\n\nN Posts" badge lines
_VB_REP_POSTS_RE = re.compile(r"(?:^|\n)\d+[\s\xa0]+Rep\n+\d+[\s\xa0]+Posts(?:\n|$)")

# "Drives: BMW X1 2025\n\nJoin Date: Feb 2026\n\nLocation: UAE"
_VB_DRIVES_RE = re.compile(r"(?:^|\n)Drives:[^\n]+(?:\n|$)")
_VB_JOIN_DATE_RE = re.compile(r"(?:^|\n)Join Date:[^\n]+(?:\n|$)")
_VB_LOCATION_RE = re.compile(r"(?:^|\n)Location:[^\n]+(?:\n|$)")

# "iTrader: (0)" or "iTrader: ([0](/itrader.php...))"
_VB_ITRADER_RE = re.compile(r"(?:^|\n)iTrader:[\s\xa0]*\(?(?:\[?\d+\]?(?:\([^\)]*\))?)\)?(?:\n|$)")

# Garage list: car model + score entries
_VB_GARAGE_RE = re.compile(r"(?:^|\n)\d{4}\s+\w+[^\n]*\[[\d.]+\](?:\n|$)")

# "Appreciate N" / "Tweet" / "Quote" action buttons
_VB_APPRECIATE_RE = re.compile(r"(?:^|\n)Appreciate[\s\xa0]+\d+(?:\n|$)")
_VB_TWEET_RE = re.compile(r"(?:^|\n)Tweet(?:\n|$)")
_VB_QUOTE_RE = re.compile(r"(?:^|\n)\[?Quote\]?(?:\([^\)]*\))?(?:\n|$)")

# Post date/number header: "| 02-08-2026, 10:36 AM | #[**1**](...) |"
_VB_POST_HEADER_RE = re.compile(
    r"(?:^|\n)\|[\s\xa0]*\d{2}-\d{2}-\d{4}[^\|]+\|[^\|]*#[^\|]+\|(?:\n|$)"
    r"(?:\| ---[^\n]*\|(?:\n|$))?"
)

# Separator tables: "| --- | --- |" alone
_VB_TABLE_SEP_RE = re.compile(r"(?:^|\n)\|[\s-]+\|[\s-]*\|?(?:\n|$)")

# "[BMW\n\nX1 / X2\n\nforum](/forums)" navbar link
_VB_FORUM_NAV_RE = re.compile(r"(?:^|\n)\[BMW\n+[^\]]+\n+forum\]\([^\)]+\)(?:\n|$)")

# Post number: "#[**1**](showpost.php...)" or "#**1**"
_VB_POST_NUM_RE = re.compile(r"(?:^|\n)#\[\*\*\d+\*\*\]\([^\)]+\)(?:\n|$)")

# ---- XenForo extra patterns ----

# Standalone "Navigation", "More options", "Back" lines (off-canvas menu remnants)
_XF_NAV_REMNANT_RE = re.compile(r"(?:^|\n)(?:Navigation|More options|Back)(?:\n|$)")

# "[Search media](/search/...)" link (may be indented as sub-item of [Media])
_XF_SEARCH_MEDIA_RE = re.compile(r"(?:^|\n)\s*\[Search media\]\([^\)]+\)(?:\n|$)")

# "[Top](#top)" link
_XF_TOP_LINK_RE = re.compile(r"(?:^|\n)\[Top\]\(#top\)(?:\n|$)")

# Standalone single-character avatar placeholders "[M](/members/...)" or "[1](/members/...)"
_XF_AVATAR_PLACEHOLDER_RE = re.compile(r"(?:^|\n)\[[A-Z0-9]\]\(/members/[^\)]+\)(?:\n|$)")

# "0\n \nReply" remnants from reaction/reply bar (may have whitespace-only lines)
_XF_REPLY_REMNANT_RE = re.compile(r"(?:^|\n)(?:\d+|Show more)[\s\n]+Repl(?:y|ies)(?:\n|$)", re.IGNORECASE)

# XenForo user info block: "[username](/members/...) + (Discussion starter)? + NNN posts · Joined YYYY"
_XF_USER_INFO_BLOCK_RE = re.compile(
    r"(?:^|\n)\[[^\]]+\]\(/members/[^\)]+\)[\s\n]+"
    r"(?:Discussion starter[\s\n]+)?"
    r"[\d,]+[\s,]*posts?[\s\n]+·[\s\n]+Joined \d+(?:\n|$)"
)

# Standalone username (no link) at very start of page (XenForo thread preview)
_XF_STANDALONE_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_ -]{1,30}\n\n")

# "You have insufficient privileges to reply here." (classified/locked threads)
_XF_INSUFFICIENT_PRIV_RE = re.compile(r"(?:^|\n)You have insufficient privileges to reply here\.(?:\n|$)")

# Unresolved emoji placeholders: [emoji846], [emoji1234] etc.
_XF_EMOJI_PLACEHOLDER_RE = re.compile(r"\[emoji\d+\]")

# U+FFFC Object Replacement Character — placeholder for unresolved inline objects
_OBJECT_REPLACEMENT_RE = re.compile(r"\ufffc")

# XenForo smiley alt text remnants: :ROFLMAO:, :cool:, :LOL:, :oops: etc.
# Only match XenForo-style codes (2+ alpha chars between colons, all-alpha),
# NOT common text emoticons like :) :( :D :P which people type naturally.
_XF_SMILEY_TEXT_RE = re.compile(r":(?:[A-Za-z]{2,}):")

# Old smiley images surviving as markdown: ![Image](url containing /smilies/)
_XF_SMILEY_IMG_RE = re.compile(r"!\[[^\]]*\]\([^\)]*\/smilies\/[^\)]+\)\s*")

# XenForo gallery alt text noise: "Preview image for gallery." → just the image
_XF_GALLERY_ALT_RE = re.compile(r"!\[Preview image for gallery\.\]")

# XenForo NSFW/spoiler toggle label (content follows, just strip the label/button)
_XF_SENSITIVE_CONTENT_RE = re.compile(
    r"(?:^|\n)Sensitive content, not recommended for those under 18\n+Show Content(?:\n|$)"
)

# XenForo attachment metadata: file size + view count under each attachment image
# e.g. "  126.4 KB\n  Views: 1,266"
_XF_ATTACHMENT_META_RE = re.compile(r"\n[ \t]+[\d.,]+\s+[KMG]?B\n[ \t]+Views:[ \t]+[\d.,]+")

# Old quote attribution: strip dead path after "Originally Posted by **username**"
# Matches /forum/post/NNNNN (vBulletin) and /t/NNNNN/slug#post_NNNNN (XenForo 1.x)
_OLD_QUOTE_ATTRIB_RE = re.compile(r"(Originally Posted by \*\*[^\*]+\*\*)\s+/(?:forum/post/\d+|t/\d+/[^\s]+)")

# Old quote label: "> Quote:" line at start of blockquote — always redundant.
# Covers "Quote:" before "Originally posted/Posted by" AND standalone "Quote:" labels.
_BLOCKQUOTE_QUOTE_LABEL_RE = re.compile(
    r"^>[ \t]*Quote:[ \t]*\n",
    re.MULTILINE,
)

# [View attachment XXXXXX] links that survived as markdown text
_XF_VIEW_ATTACHMENT_RE = re.compile(r"\[View attachment \d+\]\(https?://[^\)]+\)\s*")

# BBCode remnants from vBulletin→XenForo migration: [URL], [/URL], [IMG], [/IMG], etc.
_BBCODE_TAG_RE = re.compile(
    r"\[/?(?:URL|IMG|B|I|U|SIZE|COLOR|FONT|INDENT|LEFT|RIGHT|CENTER|MEDIA)(?:=[^\]]*)?\]",
    re.IGNORECASE,
)

# XenForo secondary heading that's the forum category name (before "last post by")
_XF_CATEGORY_LAST_POST_RE = re.compile(
    r"(?:^|\n)# [^\n]+\n+last post by[\s\n]+",
)

# ---- vBulletin extra patterns ----

# Social sharing table: "| - [Submit Thread to twitter](...) ... |"
_VB_SOCIAL_TABLE_RE = re.compile(r"(?:^|\n)\|[^\n]*Submit Thread to[^\n]*\|(?:\n|$)")

# Previous/Next thread: "**«** [Previous Thread](...) | [Next Thread](...) **»**"
_VB_PREV_NEXT_RE = re.compile(r"(?:^|\n)\*\*«\*\*[\s\S]*?\*\*»\*\*(?:\n|$)")

# Permissions block (single long line): "| ... You **may not** post ... Forum Rules ... |"
_VB_PERMISSIONS_RE = re.compile(r"(?:^|\n)[^\n]*You \*\*may not\*\*[^\n]*")

# "All times are GMT..." line
_VB_ALL_TIMES_RE = re.compile(r"(?:^|\n)All times are GMT[^\n]*(?:\n|$)")

# "Powered by vBulletin® Version ..."
_VB_POWERED_BY_RE = re.compile(r"(?:^|\n)Powered by vBulletin[^\n]*(?:\n(?:Copyright|©)[^\n]*)*(?:\n|$)")

# Trademark/copyright text: "1Addicts.com, BIMMERPOST.com, ..."
_VB_TRADEMARK_RE = re.compile(
    r"(?:^|\n)[A-Z0-9][A-Za-z0-9]+\.com(?:,\s*[A-Z0-9][A-Za-z0-9]+\.com)+"
    r"[^\n]*(?:trademark|properties|BIMMERPOST)[^\n]*(?:\n|$)"
)

# Privacy/Terms links: "[Privacy Policy](...) - [Terms of Service](...) ..."
_VB_PRIVACY_RE = re.compile(r"(?:^|\n)\[Privacy Policy\]\([^\)]+\)[^\n]*(?:\n|$)")

# Contact Us / Top table: "| **[Contact Us](...) - ... - [Top](#top)** |"
_VB_CONTACT_TOP_RE = re.compile(r"(?:^|\n)\|[^\n]*Contact Us[^\n]*Top[^\n]*\|(?:\n|$)")

# vBulletin logo at bottom: "[![u11](...)](http://...)"
_VB_BOTTOM_LOGO_RE = re.compile(r"(?:^|\n)\[!\[[^\]]*\]\([^\)]+\)\]\([^\)]+\)\s*(?:\n|$)")

# Signature separator: "__________________"
_VB_SIG_SEPARATOR_RE = re.compile(r"(?:^|\n)_{5,}(?:\n|$)")

# Empty table rows: "| |" or "|  |" or standalone "|"
_VB_EMPTY_TABLE_RE = re.compile(r"(?:^|\n)\|[\s|]*\|?(?:\n|$)")

# ---- InternetBrands vBulletin patterns (Rennlist, CorvetteForum, etc.) ----

# "Sponsored by:" label (sometimes with content after)
_IB_SPONSORED_RE = re.compile(r"(?:^|\n)Sponsored by:(?:\n|$)")

# "[Search this Thread](...)" link
_IB_SEARCH_THREAD_RE = re.compile(r"(?:^|\n)\[Search this Thread\]\([^\)]+\)(?:\n|$)")

# "Thread Starter" label after username
_IB_THREAD_STARTER_RE = re.compile(r"(?:^|\n)Thread Starter(?:\n|$)")

# User profile lines: "Joined: **Jun 2022**" / "Posts: **37**" / "Likes: **18**" / "From: **NC**"
_IB_USER_JOINED_RE = re.compile(r"(?:^|\n)Joined: \*\*[^\*]+\*\*(?:\n|$)")
_IB_USER_POSTS_RE = re.compile(r"(?:^|\n)Posts: \*\*[\d,]+\*\*(?:\n|$)")
_IB_USER_LIKES_RE = re.compile(r"(?:^|\n)Likes: \*\*[\d,]+\*\*(?:\n|$)")
_IB_USER_FROM_RE = re.compile(r"(?:^|\n)From: \*\*[^\*]+\*\*(?:\n|$)")

# User rank: "****Rennlist Member****" or "**Advanced**" or "**Racer**" etc.
# Bold text alone on a line that's a rank (with optional rank image)
_IB_USER_RANK_RE = re.compile(r"(?:^|\n)\*+[A-Za-z ]+\*+(?:\n|$)")

# Rank images: "![](https://.../images/ranks/...)"
_IB_RANK_IMAGE_RE = re.compile(r"(?:^|\n)!\[\]\([^\)]*images/ranks/[^\)]+\)(?:\n|$)")

# "N \n[Jump to Original Reply](...)" (like count + jump link)
_IB_JUMP_ORIGINAL_RE = re.compile(r"(?:^|\n)\d+\s*\n\[Jump to Original Reply\]\([^\)]+\)(?:\n|$)")

# "## Popular Reply" section header
_IB_POPULAR_REPLY_RE = re.compile(r"(?:^|\n)## Popular Reply(?:\n|$)")

# Date header: "Feb 8, 2026, 08:09 PM"
_IB_DATE_HEADER_RE = re.compile(
    r"(?:^|\n)(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{1,2}, \d{4}, \d{1,2}:\d{2} [AP]M(?:\n|$)"
)

# "[Back to Subforum\n\nForumName](...)" link
_IB_BACK_SUBFORUM_RE = re.compile(r"(?:^|\n)\[Back to Subforum\n+[^\]]+\]\([^\)]+\)(?:\n|$)")

# "[View Next Unread\n\nThread Title](...)" link
_IB_NEXT_UNREAD_RE = re.compile(r"(?:^|\n)\[View Next Unread\n+[^\]]+\]\([^\)]+\)(?:\n|$)")

# Thread Tools section: "Thread Tools\n\n[Show Printable Version]...\n[Email this Page]...\n[Advanced Search]..."
_IB_THREAD_TOOLS_RE = re.compile(
    r"(?:^|\n)Thread Tools\n+\[Show Printable Version\]\([^\)]+\)"
    r"(?:\n+\[Email this Page\]\([^\)]+\))?"
    r"(?:\n+\[Advanced Search\]\([^\)]+\))?(?:\n|$)"
)

# Forum Jump navigation (entire subforum listing)
_IB_FORUM_JUMP_RE = re.compile(r"(?:^|\n)User Control Panel\nPrivate Messages\n[\s\S]*?\[Forum Jump\]\([^\)]+\)")

# Pagination: "- First\n- Prev\n- N / N\n- Next\n- Last"
_IB_PAGINATION_RE = re.compile(
    r"(?:^|\n)(?:-\s*)?First\n(?:-\s*)?Prev\n(?:-\s*)?\d+ / \d+\n(?:-\s*)?Next\n(?:-\s*)?Last(?:\n|$)"
)

# Standalone bold page number: "**1**"
_IB_PAGE_NUM_RE = re.compile(r"(?:^|\n)\*\*\d+\*\*(?:\n|$)")

# "Trending Topics" section: heading + list of thread links with view/reply counts
_IB_TRENDING_RE = re.compile(
    r"(?:^|\n)Trending Topics\n+(?:[-*]\s+\[.*?\]\([^\)]+\)\n+\s*[\d.]+[kKMB]?\n+\s*[\d.,]+[kKMB]?\n+)+"
)

# "[Vendor Directory](...)" link
_IB_VENDOR_DIR_RE = re.compile(r"(?:^|\n)\[Vendor Directory\]\([^\)]+\)(?:\n|$)")

# Forum category description: "**C8 General Discussion** description text"
_IB_FORUM_CAT_RE = re.compile(r"(?:^|\n)\*\*[A-Z][A-Za-z0-9 ]+\*\* [A-Z][^\n]{10,}(?:\n|$)")

# "Last edited by..." in vBulletin (italic) — keep but it's fine
# Table separator lines (standalone)
_VB_TABLE_SEP_STANDALONE_RE = re.compile(r"(?:^|\n)\|(?:\s*---\s*\|)+(?:\n|$)")

# Table row with just Thread Tools link: "|  | [Thread Tools](...) |"
_VB_THREAD_TOOLS_TABLE_RE = re.compile(
    r"(?:^|\n)\|[^\|]*\|\s*\[Thread Tools\]\([^\)]+\)\s*\|(?:\n|$)"
    r"(?:\|[\s-]+\|[\s-]+\|(?:\n|$))?"
)

# "United_States" country label in user profiles
_VB_COUNTRY_RE = re.compile(r"(?:^|\n)[A-Z][a-z]+_[A-Z][a-z]+(?:\n|$)")


def postprocess_forum(markdown: str) -> str:
    """Post-processing for generic forum pages (XenForo + vBulletin)."""
    # --- XenForo ---
    markdown = _XF_MENU_LOGIN_RE.sub("\n", markdown)
    markdown = _XF_JUMP_LATEST_RE.sub("\n", markdown)
    markdown = _XF_SHARE_RE.sub("\n", markdown)
    markdown = _XF_SORT_BY_RE.sub("\n", markdown)
    markdown = _XF_RELATED_THREADS_RE.sub("\n", markdown)
    markdown = _XF_SITE_STATS_RE.sub("\n", markdown)
    markdown = _XF_FULL_LISTING_RE.sub("\n", markdown)
    markdown = _XF_SHOW_LESS_RE.sub("\n", markdown)
    markdown = _XF_SEE_LESS_MORE_RE.sub("\n", markdown)
    markdown = _XF_ADD_QUOTE_RE.sub("\n", markdown)
    markdown = _XF_THREAD_META_RE.sub("\n", markdown)
    markdown = _XF_POST_ATTRIB_RE.sub("\n", markdown)
    markdown = _XF_BROWSER_WARNING_RE.sub("\n", markdown)
    markdown = _XF_VIEWS_REPLIES_RE.sub("\n", markdown)
    markdown = _XF_LAST_POST_BY_RE.sub("\n", markdown)
    markdown = _XF_INSTALL_APP_RE.sub("\n", markdown)
    markdown = _XF_CONTACT_CLOSE_RE.sub("\n", markdown)
    markdown = _XF_NAV_LINKS_RE.sub("\n", markdown)
    markdown = _XF_INSTALL_HOWTO_RE.sub("\n", markdown)
    markdown = _XF_CROSSSITE_NAV_RE.sub("\n", markdown)
    markdown = _XF_NUMERAL_NAV_RE.sub("\n", markdown)
    markdown = _XF_INSTALL_DUPE_RE.sub("\n", markdown)

    def _format_post_anchor(m: re.Match) -> str:
        num = m.group(1)
        date = m.group(2).replace("\n", " ").strip()
        return f"\n\n---\n**#{num}** · {date}\n"

    markdown = _XF_POST_ANCHOR_RE.sub(_format_post_anchor, markdown)

    # --- XenForo 1.x ---
    markdown = _XF1_LOGIN_RE.sub("\n", markdown)
    markdown = _XF1_SEARCH_FORM_RE.sub("\n", markdown)
    markdown = _XF1_POST_NEW_RE.sub("\n", markdown)
    markdown = _XF1_PAGE_OF_RE.sub("\n", markdown)
    markdown = _XF1_SORT_BY_RE.sub("\n", markdown)
    markdown = _XF1_TO_TOP_RE.sub("\n", markdown)

    # --- vBulletin ---
    markdown = _VB_LOGIN_RE.sub("\n", markdown)
    markdown = _VB_NAV_TABLE_RE.sub("\n", markdown)
    markdown = _VB_POST_REPLY_RE.sub("\n", markdown)
    markdown = _VB_THREAD_TOOLS_RE.sub("\n", markdown)
    markdown = _VB_LOGO_RE.sub("\n", markdown)
    markdown = _VB_SOCIAL_RE.sub("\n", markdown)
    markdown = _VB_BREADCRUMB_RE.sub("\n", markdown)
    markdown = _VB_PROFILE_RE.sub("\n", markdown)
    markdown = _VB_REP_POSTS_RE.sub("\n", markdown)
    markdown = _VB_DRIVES_RE.sub("\n", markdown)
    markdown = _VB_JOIN_DATE_RE.sub("\n", markdown)
    markdown = _VB_LOCATION_RE.sub("\n", markdown)
    markdown = _VB_ITRADER_RE.sub("\n", markdown)
    markdown = _VB_GARAGE_RE.sub("\n", markdown)
    markdown = _VB_APPRECIATE_RE.sub("\n", markdown)
    markdown = _VB_TWEET_RE.sub("\n", markdown)
    markdown = _VB_QUOTE_RE.sub("\n", markdown)
    markdown = _VB_POST_HEADER_RE.sub("\n", markdown)
    markdown = _VB_TABLE_SEP_RE.sub("\n", markdown)
    markdown = _VB_FORUM_NAV_RE.sub("\n", markdown)
    markdown = _VB_POST_NUM_RE.sub("\n", markdown)
    markdown = _VB_THREAD_TOOLS_TABLE_RE.sub("\n", markdown)
    markdown = _VB_COUNTRY_RE.sub("\n", markdown)

    # --- Extra cleanup ---
    markdown = _XF_NAV_REMNANT_RE.sub("\n", markdown)
    markdown = _XF_SEARCH_MEDIA_RE.sub("\n", markdown)
    markdown = _XF_TOP_LINK_RE.sub("\n", markdown)
    markdown = _XF_AVATAR_PLACEHOLDER_RE.sub("\n", markdown)
    markdown = _XF_REPLY_REMNANT_RE.sub("\n", markdown)
    markdown = _XF_USER_INFO_BLOCK_RE.sub("\n", markdown)
    markdown = _XF_CATEGORY_LAST_POST_RE.sub("\n", markdown)

    # --- vBulletin footer/social ---
    markdown = _VB_SOCIAL_TABLE_RE.sub("\n", markdown)
    markdown = _VB_PREV_NEXT_RE.sub("\n", markdown)
    markdown = _VB_PERMISSIONS_RE.sub("\n", markdown)
    markdown = _VB_ALL_TIMES_RE.sub("\n", markdown)
    markdown = _VB_POWERED_BY_RE.sub("\n", markdown)
    markdown = _VB_TRADEMARK_RE.sub("\n", markdown)
    markdown = _VB_PRIVACY_RE.sub("\n", markdown)
    markdown = _VB_CONTACT_TOP_RE.sub("\n", markdown)
    markdown = _VB_BOTTOM_LOGO_RE.sub("\n", markdown)
    markdown = _VB_SIG_SEPARATOR_RE.sub("\n", markdown)
    markdown = _VB_EMPTY_TABLE_RE.sub("\n", markdown)
    markdown = _VB_TABLE_SEP_STANDALONE_RE.sub("\n", markdown)

    # --- InternetBrands vBulletin (Rennlist, CorvetteForum) ---
    markdown = _IB_SPONSORED_RE.sub("\n", markdown)
    markdown = _IB_SEARCH_THREAD_RE.sub("\n", markdown)
    markdown = _IB_THREAD_STARTER_RE.sub("\n", markdown)
    markdown = _IB_USER_JOINED_RE.sub("\n", markdown)
    markdown = _IB_USER_POSTS_RE.sub("\n", markdown)
    markdown = _IB_USER_LIKES_RE.sub("\n", markdown)
    markdown = _IB_USER_FROM_RE.sub("\n", markdown)
    markdown = _IB_USER_RANK_RE.sub("\n", markdown)
    markdown = _IB_RANK_IMAGE_RE.sub("\n", markdown)
    markdown = _IB_JUMP_ORIGINAL_RE.sub("\n", markdown)
    markdown = _IB_POPULAR_REPLY_RE.sub("\n", markdown)
    markdown = _IB_DATE_HEADER_RE.sub("\n", markdown)
    markdown = _IB_BACK_SUBFORUM_RE.sub("\n", markdown)
    markdown = _IB_NEXT_UNREAD_RE.sub("\n", markdown)
    markdown = _IB_THREAD_TOOLS_RE.sub("\n", markdown)
    markdown = _IB_FORUM_JUMP_RE.sub("\n", markdown)
    markdown = _IB_PAGINATION_RE.sub("\n", markdown)
    markdown = _IB_PAGE_NUM_RE.sub("\n", markdown)
    markdown = _IB_TRENDING_RE.sub("\n", markdown)
    markdown = _IB_VENDOR_DIR_RE.sub("\n", markdown)
    markdown = _IB_FORUM_CAT_RE.sub("\n", markdown)

    # XenForo: remove standalone username at very start (thread preview duplicate)
    markdown = _XF_STANDALONE_USERNAME_RE.sub("", markdown)

    # XenForo: "You have insufficient privileges to reply here." (and variants)
    markdown = _XF_INSUFFICIENT_PRIV_RE.sub("\n", markdown)

    # XenForo: unresolved emoji placeholders like [emoji846]
    markdown = _XF_EMOJI_PLACEHOLDER_RE.sub("", markdown)

    # U+FFFC object replacement characters (unresolved inline images/emoji)
    markdown = _OBJECT_REPLACEMENT_RE.sub("", markdown)

    # XenForo: smiley alt text like :ROFLMAO:, :cool:, :LOL:
    markdown = _XF_SMILEY_TEXT_RE.sub("", markdown)

    # XenForo: [View attachment XXXXXX](url) remnants (especially in quotes)
    markdown = _XF_VIEW_ATTACHMENT_RE.sub("", markdown)

    # Old smiley images that survived HTML cleanup (fallback)
    markdown = _XF_SMILEY_IMG_RE.sub("", markdown)

    # XenForo gallery alt text: "Preview image for gallery." → clean image
    markdown = _XF_GALLERY_ALT_RE.sub("![]", markdown)

    # Old quote attribution: strip dead path fragments
    markdown = _OLD_QUOTE_ATTRIB_RE.sub(r"\1", markdown)

    # Old quote label: remove redundant "Quote:" line from blockquotes
    markdown = _BLOCKQUOTE_QUOTE_LABEL_RE.sub("", markdown)

    # XenForo NSFW/spoiler toggle label
    markdown = _XF_SENSITIVE_CONTENT_RE.sub("\n", markdown)

    # XenForo attachment metadata (file size + views)
    markdown = _XF_ATTACHMENT_META_RE.sub("", markdown)

    # BBCode remnants from vBulletin→XenForo migration
    markdown = _BBCODE_TAG_RE.sub("", markdown)

    # Collapse excessive newlines
    markdown = _EXCESSIVE_NEWLINES.sub("\n\n", markdown).strip()
    return markdown
