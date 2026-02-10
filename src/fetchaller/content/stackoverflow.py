"""Stack Overflow / Stack Exchange HTML cleanup and post-processing.

Exports the standard site interface (SELECTORS_LIST, is_stackoverflow,
strip_stackoverflow_junk, postprocess_stackoverflow).

Covers stackoverflow.com and the broader Stack Exchange network
(*.stackexchange.com, superuser.com, serverfault.com, askubuntu.com,
mathoverflow.net) which share the same HTML structure.
"""

import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

_SE_DOMAINS = frozenset((
    "stackoverflow.com",
    "www.stackoverflow.com",
    "superuser.com",
    "www.superuser.com",
    "serverfault.com",
    "www.serverfault.com",
    "askubuntu.com",
    "www.askubuntu.com",
    "mathoverflow.net",
    "www.mathoverflow.net",
))


def is_stackoverflow(url: str) -> bool:
    """Check if URL is a Stack Overflow or Stack Exchange page."""
    hostname = (urlparse(url).hostname or "").lower()
    if hostname in _SE_DOMAINS:
        return True
    if hostname.endswith(".stackexchange.com"):
        return True
    return False


# ---------------------------------------------------------------------------
# CSS selectors for elements to remove before markdown conversion
# ---------------------------------------------------------------------------

SELECTORS_LIST = [
    # Left sidebar nav (Home/Questions/Tags/Users/Companies/Collectives)
    "#left-sidebar",
    # Right sidebar (Hot Network Questions, Related, Linked, ads)
    "#sidebar",
    # Post action menus (Share/Edit/Follow per answer)
    ".js-post-menu",
    # Vote buttons (up/down/save per answer)
    ".js-voting-container",
    # User signature blocks (avatar, name, badges per answer)
    ".post-signature",
    # Ad zones
    ".js-zone-container",
    ".everyonelovesstackoverflow",
    # Pagination (page 1/2/Next)
    ".s-pagination",
    # Comment action buttons (upvote/flag per comment)
    ".comment-actions",
    # "Add a comment" links
    "a.js-add-link",
    # Top header bar (logo, search, nav)
    ".s-topbar",
    # Sticky header
    "#sticky-header",
    # Filter/sort controls on question lists
    ".s-btn-group",
    "#uql-form",
    # Question stat summaries on list pages (vote/answer/view counts)
    ".s-post-summary--stats",
    # Comment user avatars (s-avatar class)
    ".s-avatar",
    # User cards in comments
    ".s-user-card--info",
]


# ---------------------------------------------------------------------------
# Soup-level cleanup (runs before markdownify)
# ---------------------------------------------------------------------------


def strip_stackoverflow_junk(soup: BeautifulSoup) -> None:
    """Remove SO-specific junk that CSS selectors can't easily catch."""
    # Remove all user avatar images (alt contains "user avatar" or empty alt on sstatic/gravatar)
    for img in list(soup.find_all("img")):
        alt = (img.get("alt") or "").lower()
        src = img.get("src") or ""
        is_avatar = "user avatar" in alt
        if not is_avatar and not alt:
            is_avatar = "sstatic.net" in src or "gravatar.com" in src
        if is_avatar:
            parent = img.parent
            if parent and parent.name == "a" and len(list(parent.children)) == 1:
                parent.decompose()
            else:
                img.decompose()

    # Remove "Want to improve this post?" banners
    for el in list(soup.find_all(["b", "strong"])):
        if "Want to improve this post?" in el.get_text():
            # Walk up to the containing div
            container = el.parent
            while container and container.name != "body":
                if container.name == "div":
                    container.decompose()
                    break
                container = container.parent
            else:
                # No div container — just remove the immediate parent if possible
                if el.parent and el.parent.name != "body":
                    el.parent.decompose()
                else:
                    el.decompose()

    # Remove Collectives promo block (h5 with "Collectives" text + container)
    for h in list(soup.find_all(["h5", "h4", "h3"])):
        text = h.get_text()
        if "Collectives" in text:
            # Remove the parent container if it looks like a promo block
            parent = h.parent
            if parent and parent.name == "div":
                parent.decompose()
            else:
                # Remove the heading and its sibling description + learn more link
                for sib in list(h.find_next_siblings()):
                    sib_text = sib.get_text(strip=True)
                    if sib_text.startswith("Find centralized"):
                        sib.decompose()
                    elif "Learn more about Collectives" in sib_text:
                        sib.decompose()
                        break  # Stop after the learn-more link
                    else:
                        break
                h.decompose()

    # Remove "Skip to main content" link
    for a in list(soup.select('a[href="#content"]')):
        if "skip" in a.get_text(strip=True).lower():
            a.decompose()

    # Remove "Know someone who can answer?" blocks
    for el in list(soup.find_all(string=re.compile(r"Know someone who can answer"))):
        parent = el.parent
        if parent and parent.name in ("p", "div", "span"):
            grandparent = parent.parent
            if grandparent and grandparent.name == "div":
                grandparent.decompose()
            else:
                parent.decompose()

    # Remove protected question notice
    for a in list(soup.select('a[href="/help/privileges/protect-questions"]')):
        container = a.parent
        if container:
            # Walk up to the notice container div
            if container.parent and container.parent.name == "div":
                container.parent.decompose()
            else:
                container.decompose()


# ---------------------------------------------------------------------------
# Markdown-level post-processing (runs after markdownify)
# ---------------------------------------------------------------------------

# Pre-compiled regex for whitespace cleanup
_EXCESSIVE_NEWLINES = re.compile(r"\n{3,}")

# Header chrome
_SKIP_TO_CONTENT_RE = re.compile(r"(?:^|\n)\[Skip to main content\]\(#content\)\n?")
_SO_LOGO_RE = re.compile(r"(?:^|\n)\[Stack Overflow\]\(https://stackoverflow\.com\)\n?")
_SE_LOGO_RE = re.compile(r"(?:^|\n)\[.+?\]\(https://[a-z]+\.(?:stackexchange\.com|superuser\.com|serverfault\.com|askubuntu\.com|mathoverflow\.net)/?\)\n?")
_ABOUT_NAV_RE = re.compile(
    r"(?:^|\n)1\.\s*\[About\]\([^\)]+\)\n"
    r"2\.\s*Products\n"
    r"3\.\s*\[For Teams\]\([^\)]+\)\n?"
)
_COLLECTIVES_BLOCK_RE = re.compile(
    r"(?:^|\n)#{1,5}\s*Collectives™ on Stack Overflow\n+"
    r"(?:Find centralized, trusted content.*?\n+)?"
    r"(?:\[Learn more about Collectives\]\([^\)]+\)\n?)?"
)
# Partial leftovers if soup stripped the heading but not the text
_COLLECTIVES_LEFTOVER_RE = re.compile(
    r"(?:^|\n)Find centralized, trusted content[^\n]*\n+"
    r"(?:\[Learn more about Collectives\]\([^\)]+\)\n?)?"
)
_LEARN_MORE_COLLECTIVES_RE = re.compile(
    r"(?:^|\n)\[Learn more about Collectives\]\([^\)]+\)(?:\n|$)"
)
_ASK_QUESTION_RE = re.compile(r"(?:^|\n)\[Ask [Qq]uestion\]\(/questions/ask\)(?:\n|$)")

# Question metadata line: "Asked N years ago / Modified / Viewed"
_QUESTION_META_RE = re.compile(
    r"(?:^|\n)Asked\n[\s\S]*?Viewed\n[\d,.]+[kKmM]?\s*times\n?"
)

# Per-answer repeated junk
# Badge lines: "28k22 gold badges122 silver badges156 bronze badges"
_BADGE_LINE_RE = re.compile(
    r"(?:^|\n)[\d,.]+[kKmM]?\d*\s*(?:gold badges?|silver badges?|bronze badges?)"
    r"[\d\s,]*(?:gold badges?|silver badges?|bronze badges?)*\n?"
)
# "edited/asked/answered [date] at [time]" attribution lines
_EDIT_DATE_RE = re.compile(
    r"\n\[?edited\s+[A-Z][a-z]+\s+\d{1,2},?\s+\d{4}\s+at\s+\d{1,2}:\d{2}\]?"
    r"(?:\([^\)]+\))?\n?"
)
_ASKED_DATE_RE = re.compile(
    r"\nasked\s+[A-Z][a-z]+\s+\d{1,2},?\s+\d{4}\s+at\s+\d{1,2}:\d{2}\n?"
)
_ANSWERED_DATE_RE = re.compile(
    r"\nanswered\s+[A-Z][a-z]+\s+\d{1,2},?\s+\d{4}\s+at\s+\d{1,2}:\d{2}\n?"
)
# "## Comments" and "## N Comments" section headers
_COMMENTS_HEADER_RE = re.compile(r"\n## \d*\s*Comments?\n")
# "Add a comment" lines (standalone or with "| Show N more comments")
_ADD_COMMENT_RE = re.compile(r"(?:^|\n)Add a comment(?:\s*\|[^\n]*)?(?:\n|$)")
# "Commented [date]" lines in comment metadata
_COMMENTED_DATE_RE = re.compile(
    r"\n\s*Commented\n\s*[A-Z][a-z]+\s+\d{1,2},?\s+\d{4}\s+at\s+\d{1,2}:\d{2}\n?"
)
# Standalone user rep in comments: "– [username](/users/...) "123 reputation""
_COMMENT_USER_REP_RE = re.compile(
    r"\n\s*–[\s\xa0]*\n\s*\[[^\]]+\]\(/users/[^\)]+\)\n+"
    r'(?:"[\d,.]+ reputation"\n+)?'
)

# User profile links left after avatar/signature removal (standalone username lines)
# These appear as "[Username](/users/NNN/slug)" on their own line
_STANDALONE_USER_LINK_RE = re.compile(r"\n\[[^\]]+\]\(/users/\d+/[^\)]+\)\n")

# Footer/bottom junk
_START_ASKING_RE = re.compile(
    r"(?:^|\n)Start asking to get answers\n+"
    r"(?:Find the answer to your question by asking\.\n+)?"
)
_EXPLORE_RELATED_RE = re.compile(r"(?:^|\n)Explore related questions\n?")
_SEE_SIMILAR_RE = re.compile(r"(?:^|\n)See similar questions with these tags\.?\n?")
# Protected question notice (if soup didn't catch it)
_PROTECTED_RE = re.compile(
    r"(?:^|\n)\*?\*?\[?Protected question\]?"
    r"(?:\([^\)]+\))?\*?\*?\.?\s*"
    r"(?:To answer this question.*?(?:association bonus\)\)\.)?)?(?:\s*The reputation requirement.*?\.)?\n?"
)
# Feed link at the end
_FEED_LINK_RE = re.compile(r"\n?\[Newest [^\]]+feed\]\(/feeds/[^\)]+\)\n?")

# Question list pages
# N questions count
_N_QUESTIONS_RE = re.compile(r"(?:^|\n)[\d,.]+ questions\n?")
# Sort tabs: "Newest Active Bountied N Unanswered"
_SORT_TABS_RE = re.compile(
    r"(?:^|\n)\[Newest\]\([^\)]+\)\n"
    r"\[Active\]\([^\)]+\)\n"
    r"[\s\S]*?"
    r"(?:Unanswered(?: \(my tags\))?\n+)"
)
# Filter form text
_FILTER_FORM_RE = re.compile(
    r"(?:^|\n)Filter\n+Filter by\n+"
    r"[\s\S]*?"
    r"(?:Cancel\n?)"
)
# Vote/answer/view count blocks on list pages: "0\nvotes\n\n0\nanswers\n\n5\nviews"
_VOTE_ANSWER_VIEW_RE = re.compile(
    r"(?:^|\n)\d+\nvotes\n+\d+\nanswers?\n+\d+\nviews\n?"
)

# "asked/answered/modified N mins/hours/days ago" on list pages
_RELATIVE_TIME_RE = re.compile(
    r"\n(?:asked|answered|modified) (?:\d+ (?:min|hour|day|week|month|year)s? ago|today|yesterday)\n?"
)

# Standalone "Filter" line on list pages
_FILTER_LINE_RE = re.compile(r"(?:^|\n)Filter(?:\n|$)")

# "More" dropdown sort tabs (Bountied N, Frequent, Score, Trending, Week, Month, etc.)
_MORE_TABS_RE = re.compile(
    r"(?:^|\n)-\s*\[(?:Bountied|Unanswered|Frequent|Score|Trending|Week|Month)\b[^\n]*\n"
    r"(?:-\s*\[(?:Bountied|Unanswered|Frequent|Score|Trending|Week|Month)\b[^\n]*\n)*"
    r"(?:-\s*Unanswered \(my tags\)\n?)?"
)

# ISO timestamps from comments: "2017-05-23T21:41:53.703Z+00:00" or "2024-04-22 10:49:31 +00:00"
_ISO_TIMESTAMP_RE = re.compile(r"\n\s*\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[^\n]*\n")

# Comment score numbers: standalone small number followed by blank/whitespace-only lines
_COMMENT_SCORE_RE = re.compile(r"\n\n\d{1,4}\n\n(?: \n\n)+")

# Comment action text: "Reply", "Copy link" standalone
_REPLY_RE = re.compile(r"\nReply\n")
_COPY_LINK_RE = re.compile(r"\n-?\s*Copy link\n")

# "Show N more comments" link
_SHOW_MORE_COMMENTS_RE = re.compile(r"(?:^|\n)Show \d+ more comments?\n?")

# "Sign up or log in ... clarification" notice
_SIGN_UP_CLARIFICATION_RE = re.compile(
    r"(?:^|\n)Sign up[^\n]*(?:clarification|additional context)[^\n]*\n?"
)

# Comment time links: "[Over a year ago](#comment...)" or "[3 months ago](#comment...)"
_COMMENT_TIME_LINK_RE = re.compile(r"\[(?:Over )?[\w\d]+ [\w]+ ago\]\(#comment\d+\)")

# Leftover "lang-py" / "lang-js" etc. at very end
_LANG_TAG_RE = re.compile(r"\nlang-\w+\s*$")

# Hot Network Questions
_HOT_NETWORK_RE = re.compile(
    r"(?:^|\n)#{1,4}\s*Hot Network Questions\n[\s\S]*$"
)


def postprocess_stackoverflow(markdown: str) -> str:
    """Strip remaining Stack Overflow UI text from markdown."""
    # Header chrome
    markdown = _SKIP_TO_CONTENT_RE.sub("\n", markdown)
    markdown = _SO_LOGO_RE.sub("\n", markdown)
    markdown = _SE_LOGO_RE.sub("\n", markdown)
    markdown = _ABOUT_NAV_RE.sub("\n", markdown)
    markdown = _COLLECTIVES_BLOCK_RE.sub("\n", markdown)
    markdown = _COLLECTIVES_LEFTOVER_RE.sub("\n", markdown)
    markdown = _LEARN_MORE_COLLECTIVES_RE.sub("\n", markdown)
    markdown = _ASK_QUESTION_RE.sub("\n", markdown)

    # Question metadata
    markdown = _QUESTION_META_RE.sub("\n", markdown)

    # Per-answer junk
    markdown = _BADGE_LINE_RE.sub("\n", markdown)
    markdown = _EDIT_DATE_RE.sub("\n", markdown)
    markdown = _ASKED_DATE_RE.sub("\n", markdown)
    markdown = _ANSWERED_DATE_RE.sub("\n", markdown)
    markdown = _COMMENTS_HEADER_RE.sub("\n", markdown)
    markdown = _ADD_COMMENT_RE.sub("\n", markdown)
    markdown = _COMMENTED_DATE_RE.sub("\n", markdown)
    markdown = _COMMENT_USER_REP_RE.sub("\n", markdown)
    markdown = _STANDALONE_USER_LINK_RE.sub("\n", markdown)

    # Footer/bottom
    markdown = _PROTECTED_RE.sub("\n", markdown)
    markdown = _START_ASKING_RE.sub("\n", markdown)
    markdown = _EXPLORE_RELATED_RE.sub("\n", markdown)
    markdown = _SEE_SIMILAR_RE.sub("\n", markdown)
    markdown = _FEED_LINK_RE.sub("\n", markdown)

    # Question list pages
    markdown = _N_QUESTIONS_RE.sub("\n", markdown)
    markdown = _SORT_TABS_RE.sub("\n", markdown)
    markdown = _FILTER_FORM_RE.sub("\n", markdown)
    markdown = _VOTE_ANSWER_VIEW_RE.sub("\n", markdown)

    # Question list page junk
    markdown = _RELATIVE_TIME_RE.sub("\n", markdown)
    markdown = _FILTER_LINE_RE.sub("\n", markdown)
    markdown = _MORE_TABS_RE.sub("\n", markdown)

    # Hot Network Questions (must come before standalone number cleanup)
    markdown = _HOT_NETWORK_RE.sub("\n", markdown)

    # ISO timestamps from expanded comments
    markdown = _ISO_TIMESTAMP_RE.sub("\n", markdown)

    # Comment score numbers (followed by whitespace-only lines)
    markdown = _COMMENT_SCORE_RE.sub("\n\n", markdown)

    # Comment action text
    markdown = _REPLY_RE.sub("\n", markdown)
    markdown = _COPY_LINK_RE.sub("\n", markdown)

    # Comment time links
    markdown = _COMMENT_TIME_LINK_RE.sub("", markdown)

    # Show more comments link
    markdown = _SHOW_MORE_COMMENTS_RE.sub("\n", markdown)

    # Sign up notice
    markdown = _SIGN_UP_CLARIFICATION_RE.sub("\n", markdown)

    # Leftover lang tag
    markdown = _LANG_TAG_RE.sub("", markdown)

    # Collapse excessive newlines
    markdown = _EXCESSIVE_NEWLINES.sub("\n\n", markdown).strip()
    return markdown
