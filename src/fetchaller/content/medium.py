"""Medium-specific HTML cleanup and post-processing.

Exports the standard site interface (SELECTORS_LIST, is_medium,
strip_medium_junk, postprocess_medium).
"""

import re
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

# Known Medium-hosted publication domains (custom domains on Medium platform)
_MEDIUM_PUBLICATION_DOMAINS = frozenset((
    # In Plain English network
    "python.plainenglish.io",
    "javascript.plainenglish.io",
    "aws.plainenglish.io",
    "ai.plainenglish.io",
    # Community / tech publications
    "blog.stackademic.com",
    "blog.bitsrc.io",
    "blog.devgenius.io",
    "blog.devops.dev",
    "levelup.gitconnected.com",
    "betterprogramming.pub",
    "betterhumans.pub",
    "uxdesign.cc",
    "ehandbook.com",
    "writingcooperative.com",
    "psiloveyou.xyz",
    "entrepreneurshandbook.co",
    "codeburst.io",
    "itnext.io",
    "pub.towardsai.net",
    "infosecwriteups.com",
    "systemweakness.com",
    "faun.pub",
    "blog.gopenai.com",
    "towardsdev.com",
    "generativeai.pub",
    "selectfrom.dev",
    "ai.gopubby.com",
    "medium.datadriveninvestor.com",
    "blog.prototypr.io",
    "blog.cubed.run",
    "blog.dataengineerthings.org",
    # Company engineering / official blogs
    "netflixtechblog.com",
    "proandroiddev.com",
    "blog.kotlin-academy.com",
    "blog.flutter.dev",
    "blog.angular.dev",
    "blog.angular.io",
    "engineering.razorpay.com",
    "engineering.depop.com",
    "tech.olx.com",
    "medium.engineering",
))


def is_medium(url: str) -> bool:
    """Check if URL is a Medium page (medium.com, *.medium.com, or known publications)."""
    hostname = (urlparse(url).hostname or "").lower()
    if hostname in ("medium.com", "www.medium.com"):
        return True
    if hostname.endswith(".medium.com"):
        return True
    return hostname in _MEDIUM_PUBLICATION_DOMAINS


def is_medium_html(soup: BeautifulSoup) -> bool:
    """Detect Medium pages by HTML markers (for unknown custom domains).

    Checks for Medium-specific data-testid attributes that only appear on
    Medium-hosted pages.
    """
    return bool(soup.select_one('[data-testid="headerSignUpButton"]'))


# ---------------------------------------------------------------------------
# CSS selectors for elements to remove before markdown conversion
# ---------------------------------------------------------------------------

SELECTORS_LIST = [
    # Header buttons (data-testid)
    '[data-testid="headerSignUpButton"]',
    '[data-testid="headerSignInButton"]',
    '[data-testid="headerSearchInput"]',
    '[data-testid="headerSearchButton"]',
    '[data-testid="headerWriteButton"]',
    '[data-testid="headerMediumLogo"]',
    '[data-testid="headerUserIcon"]',
    # Sitemap link
    'a[href="/sitemap/sitemap.xml"]',
    'a[href*="/sitemap/sitemap.xml"]',
    # Open in app (Google Play link)
    'a[href*="play.google.com/store/apps"]',
    # Member-only story buttons
    'button[aria-label="Member-only story"]',
    # Speechify / text-to-speech
    '.speechify-ignore',
    'a[href*="speechify.com/medium"]',
]


# ---------------------------------------------------------------------------
# Soup-level cleanup (runs before markdownify)
# ---------------------------------------------------------------------------


def strip_medium_junk(soup: BeautifulSoup) -> None:
    """Remove Medium-specific junk that CSS selectors can't easily catch."""
    # Strip ?source=... tracking params from all <a> hrefs
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "source=" not in href:
            continue
        try:
            parsed = urlparse(href)
            params = parse_qs(parsed.query, keep_blank_values=True)
            params.pop("source", None)
            new_query = urlencode(params, doseq=True)
            a["href"] = urlunparse(parsed._replace(query=new_query))
        except Exception:
            pass

    # Remove duplicate miro.medium.com images (multiple sizes of same image).
    # Medium renders the same image at fill:40:40, fill:64:64, fill:76:76,
    # fill:80:80, fill:96:96, fill:128:128 — keep only the first occurrence
    # of each base image hash.
    seen_hashes = set()
    for img in list(soup.find_all("img", src=True)):
        src = img["src"]
        if "miro.medium.com" not in src:
            continue
        # Extract the image hash (the part after the last /)
        # e.g., "1*VA3oGfprJgj5fRsTjXp6fA@2x.png" from the URL
        parts = src.split("/")
        img_hash = parts[-1] if parts else src
        # Normalize by removing @2x suffix
        img_hash = re.sub(r"@\dx", "", img_hash)
        if img_hash in seen_hashes:
            # Remove the parent <a> if the img is the only child
            parent = img.parent
            if parent and parent.name == "a" and len(list(parent.children)) == 1:
                parent.decompose()
            else:
                img.decompose()
        else:
            seen_hashes.add(img_hash)

    # Remove sign in/up links
    for a in list(soup.find_all("a", href=True)):
        href = a["href"]
        if "/m/signin" in href:
            a.decompose()


# ---------------------------------------------------------------------------
# Markdown-level post-processing (runs after markdownify)
# ---------------------------------------------------------------------------

# Pre-compiled regex for whitespace cleanup
_EXCESSIVE_NEWLINES = re.compile(r"\n{3,}")

# Header chrome: [Open in app](...), [Sitemap](...), [Write](...), [Search](...)
_OPEN_IN_APP_RE = re.compile(r"\n?\[Open in app\]\([^\)]+\)\n?")
_WRITE_LINK_RE = re.compile(r"\n?\[Write\]\([^\)]+\)\n?")
_SEARCH_LINK_RE = re.compile(r"\n?\[Search\]\([^\)]+\)\n?")
_SITEMAP_LINK_RE = re.compile(r"\n?\[Sitemap\]\([^\)]+\)\n?")
# Standalone "Sign up" / "Sign in" lines
_SIGN_UP_RE = re.compile(r"\nSign up\n")
_SIGN_IN_RE = re.compile(r"\n\[Sign in\]\([^\)]+\)\n")

# Member-only label
_MEMBER_ONLY_RE = re.compile(r"(?:^|\n)Member-only story\n|(?:^|\n)Member-only\n")

# Follow publication / Follow (standalone)
_FOLLOW_PUB_RE = re.compile(r"(?:^|\n)Follow publication(?:\n|$)")
_FOLLOW_RE = re.compile(r"(?:^|\n)Follow(?:\n|$)")

# Article action buttons: "--" (clap count separator), "Listen", "Share"
# The "--" appears as a separator before the response/clap count
_CLAP_SEPARATOR_RE = re.compile(r"\n--\n")
_LISTEN_RE = re.compile(r"\nListen\n")
_SHARE_RE = re.compile(r"\nShare\n")

# "Press enter or click to view image in full size"
_IMAGE_FULLSIZE_RE = re.compile(r"(?:^|\n)Press enter or click to view image in full size\n")

# Post-article blocks — markdownify may produce any of these formats:
#   "## Published in ..."      (plain heading)
#   "[## Published in ...](u)" (link wrapping heading)
#   "## [Published in ...](u)" (heading containing link)
_PUBLISHED_IN_RE = re.compile(
    r"\n\[?##\]? ?\[?Published in .+?"
    r"(?=\n\[?##\]? ?\[?Written by |\n## Responses |\n\[Help\]|\Z)",
    re.DOTALL,
)
_WRITTEN_BY_RE = re.compile(
    r"\n\[?##\]? ?\[?Written by .+?"
    r"(?=\n## Responses |\n\[Help\]|\Z)",
    re.DOTALL,
)
# "## Responses (N)" and "See all responses"
_RESPONSES_RE = re.compile(
    r"\n## Responses \(\d+\)\n+(?:See all responses\n?)?",
)
_SEE_ALL_RESPONSES_RE = re.compile(r"\nSee all responses\n")

# Footer: combinations of Help, Status, About, Careers, Press, Blog, Privacy, Rules, Terms, Text to speech
_FOOTER_RE = re.compile(
    r"\n\[(?:Help|About)\]\([^\)]+\)"
    r".*?"
    r"\[(?:Text to speech|Terms|Privacy)\]\([^\)]+\)\s*$",
    re.DOTALL,
)

# Medium small logo image (64x64 Medium "M" logo)
_MEDIUM_LOGO_IMG_RE = re.compile(
    r"\n?!\[\]\(https://miro\.medium\.com/v2/resize:fill:64:64/1\*dmbNkD5D[^\)]*\)\n?"
)

# Small miro.medium.com avatar/logo images (resize:fill:NN:NN) — linked or standalone
_MIRO_AVATAR_RE = re.compile(
    r"\n?\[?!\[[^\]]*\]\(https://miro\.medium\.com/v2/resize:fill:[^\)]+\)\]?"
    r"(?:\([^\)]+\))?\n?"
)

# Source tracking params left in markdown URLs: ?source=...---------) or &source=...)
_SOURCE_PARAM_RE = re.compile(r"[?&]source=[a-z0-9_-]+(?:---[a-z0-9_-]*)*-*")

# Explore topics link
_EXPLORE_TOPICS_RE = re.compile(r"\n?\[Explore topics\]\([^\)]+\)\n?")

# "See more recommended stories" link at bottom of tag pages
_SEE_MORE_RE = re.compile(r"\n\[See more recommended stories\]\([^\)]+\)\n?")

# "N followers" / "N following" links (in post-article author blocks)
_FOLLOWERS_LINK_RE = re.compile(r"\n\[\d+[\d,.kKmM]* followers?\]\([^\)]+\)\n")
_FOLLOWING_LINK_RE = re.compile(r"\n\[\d+[\d,.kKmM]* following\]\([^\)]+\)\n")
_LAST_PUBLISHED_RE = re.compile(r"\n\[Last published [^\]]+\]\([^\)]+\)\n")

# "A response icon N" links (response count on tag/list pages)
_RESPONSE_ICON_RE = re.compile(r"\n?\[A response icon\d*\]\([^\)]+\)\n?")

# Bookmark signin links (empty text links to /m/signin with bookmark action)
_BOOKMARK_SIGNIN_RE = re.compile(r"\n?\[]\(/m/signin\?actionUrl=[^\)]*bookmark[^\)]*\)\n?")

# "[Home](?source=...)" link on 404 pages
_HOME_LINK_RE = re.compile(r"\n\[Home\]\([^\)]+\)\n")

# Pre-article publication header: "[## Publication Name](url)" followed by
# optional image and tagline. Matches from the linked heading through the
# tagline text before the actual article heading starts.
# Pre-article publication header block:
# "[## Publication Name](https://pub.io/)" or "[## Pub Name](https://pub.io/?source=...)"
# Only match links to a bare domain root (no path after /), not article links.
_PUB_HEADER_LINK_RE = re.compile(
    r"(?:^|\n)\[## [^\]]+\]\(https?://[a-zA-Z0-9._-]+/(?:\?[^\)]*)?\)\n"
)
_PUB_TAGLINE_RE = re.compile(
    r"\n.+Follow to join our [\d.]+[MKmk]\+ monthly readers\.\n"
)


def postprocess_medium(markdown: str) -> str:
    """Strip remaining Medium UI text from markdown."""
    # Header chrome
    markdown = _OPEN_IN_APP_RE.sub("\n", markdown)
    markdown = _WRITE_LINK_RE.sub("\n", markdown)
    markdown = _SEARCH_LINK_RE.sub("\n", markdown)
    markdown = _SITEMAP_LINK_RE.sub("\n", markdown)
    markdown = _SIGN_UP_RE.sub("\n", markdown)
    markdown = _SIGN_IN_RE.sub("\n", markdown)
    markdown = _MEDIUM_LOGO_IMG_RE.sub("\n", markdown)
    markdown = _EXPLORE_TOPICS_RE.sub("\n", markdown)

    # Member-only / follow
    markdown = _MEMBER_ONLY_RE.sub("\n", markdown)
    markdown = _FOLLOW_PUB_RE.sub("\n", markdown)

    # Article action buttons
    markdown = _CLAP_SEPARATOR_RE.sub("\n", markdown)
    markdown = _LISTEN_RE.sub("\n", markdown)
    markdown = _SHARE_RE.sub("\n", markdown)
    markdown = _IMAGE_FULLSIZE_RE.sub("\n", markdown)

    # Post-article blocks (order matters: Published -> Written -> Responses)
    markdown = _PUBLISHED_IN_RE.sub("\n", markdown)
    markdown = _WRITTEN_BY_RE.sub("\n", markdown)
    markdown = _RESPONSES_RE.sub("\n", markdown)
    markdown = _SEE_ALL_RESPONSES_RE.sub("\n", markdown)

    # Footer
    markdown = _FOOTER_RE.sub("\n", markdown)

    # Standalone Follow after other cleanup
    markdown = _FOLLOW_RE.sub("\n", markdown)

    # Post-article metadata links
    markdown = _FOLLOWERS_LINK_RE.sub("\n", markdown)
    markdown = _FOLLOWING_LINK_RE.sub("\n", markdown)
    markdown = _LAST_PUBLISHED_RE.sub("\n", markdown)

    # Tag/list page junk
    markdown = _RESPONSE_ICON_RE.sub("", markdown)
    markdown = _BOOKMARK_SIGNIN_RE.sub("", markdown)
    markdown = _SEE_MORE_RE.sub("\n", markdown)
    markdown = _HOME_LINK_RE.sub("\n", markdown)

    # Pre-article publication header links and avatar/logo images
    markdown = _PUB_HEADER_LINK_RE.sub("\n", markdown)
    markdown = _PUB_TAGLINE_RE.sub("\n", markdown)
    markdown = _MIRO_AVATAR_RE.sub("\n", markdown)

    # Clean source= tracking params from remaining markdown URLs
    markdown = _SOURCE_PARAM_RE.sub("", markdown)

    # Collapse excessive newlines left behind
    markdown = _EXCESSIVE_NEWLINES.sub("\n\n", markdown).strip()
    return markdown
