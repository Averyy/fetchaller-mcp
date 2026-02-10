"""RedFlagDeals forum HTML cleanup and post-processing.

Exports the standard site interface (SELECTORS_LIST, is_redflagdeals,
strip_rfd_junk, postprocess_rfd).
"""

import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------


def is_redflagdeals(url: str) -> bool:
    """Check if URL is a RedFlagDeals forum page."""
    hostname = (urlparse(url).hostname or "").lower()
    return hostname == "forums.redflagdeals.com"


# ---------------------------------------------------------------------------
# CSS selectors for elements to remove before markdown conversion
# ---------------------------------------------------------------------------

SELECTORS_LIST = [
    # Site header (logo, nav, search, user menu, PTO banner)
    ".main_nav_bar",
    ".main_nav_bar_outer",
    "#main_nav_overlay",
    "#main_nav_scroller",
    "#site_header_pto_banner",
    ".header_logo",
    ".header_actions",
    "#forums_nav_search_box",

    # Ads
    ".ad_leaderboard_large",
    ".ad_leaderboard_medium_large",
    ".ad_leaderboard_small",
    ".ad_sidebar_bigbox",
    "#header_leaderboard",
    "#header_leaderboard_large",
    "#header_leaderboard_medium_large",
    "#header_leaderboard_small",
    "#header_billboard_bottom",
    "#header_billboard_bottom_large",
    "#desktop_ad_between_thread_1",
    "#desktop_ad_between_thread_2",
    "#mobile_ad_between_thread_1",
    "#mobile_ad_between_thread_2",
    "#mobile_ad_between_thread_3",
    "#inline_bigbox_first",
    ".forum_topic_inline_bigbox",
    ".pencil_ad",

    # Breaking news promo
    "#breaking_news",

    # Auth / popups
    "#user-auth-container",
    "#user-auth-popup",

    # Forum nav sidebar
    ".forums_nav_sidebar_column",
    ".forums_nav_primary_column",

    # Forum page header buttons (Post Deal, Create Alert)
    ".forums_page_header_buttons",
    ".forums_tabbar_buttons",

    # Filter bar (category, date range, store filters)
    ".filter_bar",
    ".filter_dropdown",
    ".category_filter",
    ".category_filter_dropdown",
    ".change_view_dropdown",
    ".filter_bg_overlay",

    # Thread listing metadata
    ".author_avatar",
    ".avatar_container",
    ".ellipses_menu",
    ".thread_outer_header",
    ".thread_outer_footer",
    ".thread_image",
    ".thread_list_small_menu",
    ".thread_meta_small_replies",
    ".thread_pagination_forums",

    # Thread page: action buttons
    ".thread_navigation",
    ".below_postlist",

    # Thread page: deal alert/expired prompts
    ".thead_expired_msg_mobile",
    ".thread_expired_alert_button_link",

    # Thread page: voting section (score, reasons, breakdown)
    ".thread_voting",
    ".threadvoting_breakdown_trigger",
    ".threadvoting_reasons_form_dropdown",
    ".threadvoting_breakdown_dropdown",
    ".thread_header_actions_container",

    # Thread page: header buttons (Get This Deal, Deal Alert, Share)
    ".thread_header_buttons",
    ".copy_share_link",
    ".thread_header_post_reply",

    # Thread page: topic info / who's online
    ".whosonline",

    # Thread page: display posts filter / sort
    ".topic_filter_daysprune",

    # Trending Hot Deals sidebar
    "#trending_hotdeals_threads",
    ".section_sidebar",

    # Thread display options / sort controls
    ".thread_display_options_container",
    ".sort-tools",

    # Forum jump dropdown
    ".forum_jump",
    ".thread_navigation_secondary",

    # Footer
    "#site_footer_menus",
    "#site_footer_boilerplate",

    # Misc
    "#partition_deals_widget",
]


# ---------------------------------------------------------------------------
# Soup-level cleanup (runs before markdownify)
# ---------------------------------------------------------------------------


def strip_rfd_junk(soup: BeautifulSoup) -> None:
    """Remove RFD-specific junk that CSS selectors can't easily catch."""
    # Remove PTO banner images
    for img in list(soup.find_all("img", src=True)):
        src = img["src"]
        if "pto_banners" in src or "rfdcontent.com/graphics" in src:
            parent = img.parent
            if parent and parent.name == "a" and len(list(parent.children)) == 1:
                parent.decompose()
            else:
                img.decompose()

    # Remove SVG icons (forum icons, pin icons, vote arrows)
    for svg in list(soup.find_all("svg")):
        svg.decompose()


# ---------------------------------------------------------------------------
# Markdown-level post-processing (runs after markdownify)
# ---------------------------------------------------------------------------

# Pre-compiled regex for whitespace cleanup
_EXCESSIVE_NEWLINES = re.compile(r"\n{3,}")

# "Back to Menu" lines
_BACK_TO_MENU_RE = re.compile(r"(?:^|\n)Back to Menu(?:\n|$)")

# Stray "Cancel" lines (from filter dropdowns)
_CANCEL_RE = re.compile(r"(?:^|\n)Cancel(?:\n|$)")

# Auth lines
_SIGN_UP_LOG_IN_RE = re.compile(r"(?:^|\n)Sign Up / Log In(?:\n|$)")
_SIGN_UP_RE = re.compile(r"(?:^|\n)Sign Up(?:\n|$)")

# Action buttons
_POST_DEAL_RE = re.compile(r"(?:^|\n)Post (?:a )?Deal(?:\n|$)")
_CREATE_ALERT_RE = re.compile(r"(?:^|\n)Create Alert(?:\n|$)")

# Forum rule links
_NEW_POSTS_RE = re.compile(r"(?:^|\n)\[?New Posts\]?(?:\([^\)]*\))?(?:\n|$)")
_RSS_FEED_RE = re.compile(r"(?:^|\n)\[?RSS Feed\]?(?:\([^\)]*\))?(?:\n|$)")
_FORUM_RULES_RE = re.compile(r"(?:^|\n)\[?Forum Rules\]?(?:\([^\)]*\))?(?:\n|$)")

# "Search this thread" lines (both plain and linked)
_SEARCH_THREAD_RE = re.compile(r"(?:^|\n)-?\s*\[?Search this thread\]?(?:\([^\)]*\))?(?:\n|$)")

# Voting summary lines (SCORE / votes / views patterns)
# May appear inside list items: "  - +50 SCORE" or "  - -4 SCORE"
_SCORE_LINE_RE = re.compile(r"(?:^|\n)\s*-?\s*[+-]?\d+[\s\xa0]*SCORE(?:\n|$)")
_VOTES_VIEWS_RE = re.compile(
    r"(?:^|\n)\d+[\s\xa0]*(?:votes?|views?)(?:[\s\xa0]+\d+[\s\xa0]*(?:votes?|views?))*(?:\n|$)"
)

# Thread listing: "X replies" standalone lines
_REPLIES_LINE_RE = re.compile(r"(?:^|\n)\d+[\s\xa0]+replies?(?:\n|$)")

# Deal score in thread listings: standalone "+N" or "-N"
_DEAL_SCORE_RE = re.compile(r"(?:^|\n)[+-]\d+(?:\n|$)")

# "Last post by <user>" lines (thread listing metadata)
_LAST_POST_BY_RE = re.compile(r"(?:^|\n)Last [Pp]ost by [^\n]+(?:\n|$)")

# "Go to first new post" links
_FIRST_NEW_POST_RE = re.compile(r"(?:^|\n)\[Go to first new post\]\([^\)]+\)(?:\n|$)")

# Forum breadcrumb: "Forums > Hot Deals" or "RedFlagDeals.com > Forums"
_BREADCRUMB_RE = re.compile(
    r"(?:^|\n)\[?(?:RedFlagDeals\.com|Forums|Hot Deals|Pair of Deals)"
    r"(?:\](?:\([^\)]*\))?)?"
    r"(?:[\s\xa0]*>[\s\xa0]*\[?(?:RedFlagDeals\.com|Forums|Hot Deals|Pair of Deals)"
    r"(?:\](?:\([^\)]*\))?)?)*(?:\n|$)"
)

# "Expired Deal" / "Hot" / "Popular" deal status badges
_DEAL_STATUS_RE = re.compile(r"(?:^|\n)(?:Expired Deal|Expired)(?:\n|$)")

# "Report" / "Share" standalone action lines
_REPORT_RE = re.compile(r"(?:^|\n)Report(?:\n|$)")
_SHARE_RE = re.compile(r"(?:^|\n)Share(?:\n|$)")

# Post number references: "#1", "#2" etc. on their own line
_POST_NUMBER_RE = re.compile(r"(?:^|\n)#\d+(?:\n|$)")

# "Reply" / "Quote" standalone action lines
_REPLY_QUOTE_RE = re.compile(r"(?:^|\n)(?:Reply|Quote)(?:\n|$)")

# "Jump to Forum" lines
_JUMP_TO_FORUM_RE = re.compile(r"(?:^|\n)Jump to Forum(?:\n|$)")

# "Reply to ThreadReply" / "Reply to Thread" button text
_REPLY_TO_THREAD_RE = re.compile(
    r"(?:^|\n)\[?Reply to Thread(?:Reply)?\]?(?:\([^\)]*\))?(?:\n|$)"
)

# "Post New Deal Post" button at bottom
_POST_NEW_DEAL_RE = re.compile(r"(?:^|\n)\[?Post New Deal[\s\n]*Post\]?(?:\([^\)]*\))?(?:\n|$)")

# "Share deal" / "Copied" lines
_SHARE_DEAL_RE = re.compile(r"(?:^|\n)Share deal(?:\n|$)")
_COPIED_RE = re.compile(r"(?:^|\n)Copied(?:\n|$)")

# "Verify pricing" disclaimer
_VERIFY_PRICING_RE = re.compile(
    r"(?:^|\n)Verify pricing and deal details[^\n]*(?:\n|$)"
)

# Vote reason labels
_VOTE_REASON_RE = re.compile(
    r"(?:^|\n):?\s*(?:Not a good price|Bad product/service|"
    r"Poor merchant reputation|Unable to get the deal|Other \(downvote\)|Upvote|Reason)(?:\n|$)"
)

# "Score breakdown" header
_SCORE_BREAKDOWN_RE = re.compile(r"(?:^|\n)Score breakdown[^\n]*(?:\n|$)")

# Percentage lines from score breakdown: "0%"
_PERCENTAGE_LINE_RE = re.compile(r"(?:^|\n):?\s*\d+%(?:\n|$)")

# "Display posts from previous" sort controls
_DISPLAY_POSTS_RE = re.compile(
    r"(?:^|\n)Display posts from previous:[^\n]*(?:\n|$)"
)

# "Sort by AuthorPost time AscendingDescending" lines
_SORT_BY_RE = re.compile(r"(?:^|\n)Sort by [^\n]*(?:\n|$)")

# Standalone "Go" button text
_GO_RE = re.compile(r"(?:^|\n)Go(?:\n|$)")

# "AscendingDescending" sort remnant
_ASC_DESC_RE = re.compile(r"(?:^|\n)AscendingDescending(?:\n|$)")

# "All postsLast dayLast 7 days..." filter option remnants
_ALL_POSTS_FILTER_RE = re.compile(
    r"(?:^|\n)All postsLast day[^\n]*(?:\n|$)"
)

# Topic Information header + viewer count
_TOPIC_INFO_RE = re.compile(
    r"(?:^|\n)#{1,4}\s*Topic Information\n[\s\S]*?(?=\n## |\Z)"
)

# "Add Deal Alert" links
_ADD_DEAL_ALERT_RE = re.compile(
    r"(?:^|\n)\[Add Deal Alert[^\]]*\]\([^\)]+\)(?:\n|$)"
)

# "Get This Deal" links
_GET_THIS_DEAL_RE = re.compile(
    r"(?:^|\n)\[Get This Deal\]\([^\)]+\)(?:\n|$)"
)

# User profile links: [username](/memberlist.php?...)
_MEMBERLIST_LINK_RE = re.compile(
    r"\[([^\]]+)\]\(/memberlist\.php\?[^\)]+\)"
)

# "replied Ns/m/h/d ago" standalone lines
_REPLIED_AGO_RE = re.compile(r"(?:^|\n)replied\s*\n?\s*\d+\w?\s+ago(?:\n|$)")

# Column header row: "Title / Thread start date / Score Replies Views Last post"
_COLUMN_HEADERS_RE = re.compile(
    r"(?:^|\n)-?\s*Title\s*\n\s*/\s*\n\s*Thread start date[^\n]*(?:\n|$)"
    r"(?:\s*(?:/\s*\n\s*)?(?:Score|Replies|Views|Last post|Sub-Forum|Topics|Posts)\s*\n)*"
)

# "Last Page" links in thread listings (may be nested: "- - [Last Page]")
_LAST_PAGE_RE = re.compile(r"(?:^|\n)(?:\s*-\s*)*\[Last Page\]\([^\)]+\)(?:\n|$)")

# Definition-list style score: "  :   +72" or ":   -5" (may have leading whitespace)
_DL_SCORE_RE = re.compile(r"(?:^|\n)\s*:\s+[+-]\d+(?:\n|$)")

# "Thread Display Options" header
_THREAD_DISPLAY_OPTIONS_RE = re.compile(r"(?:^|\n)Thread Display Options(?:\n|$)")

# "Show threads from the" + filter options
_SHOW_THREADS_RE = re.compile(
    r"(?:^|\n)Show threads from the\s*\n"
    r"(?:BeginningLast day[^\n]*\n)?"
)

# "Compare Credit Cards" / "Learn More" promo blocks (milesopedia)
_COMPARE_CARDS_RE = re.compile(
    r"(?:^|\n)Maximize your credit card rewards![^\n]*\n"
    r"Compare Credit Cards\n+"
    r"\[Learn More\]\([^\)]+\)\n?"
)

# "Nearby ... locations:" empty section
_NEARBY_LOCATIONS_RE = re.compile(r"(?:^|\n)Nearby [^\n]+ locations:(?:\n|$)")

# "My Profile" and profile menu items
_MY_PROFILE_RE = re.compile(r"(?:^|\n)My Profile(?:\n|$)")
_PROFILE_MENU_RE = re.compile(
    r"(?:^|\n)\[(?:Inbox|Notifications|Subscriptions|Thread History|Settings|"
    r"Deal Alerts|Log Out)\]\([^\)]+\)(?:\n|$)"
)


def postprocess_rfd(markdown: str) -> str:
    """Strip remaining RedFlagDeals UI text from markdown."""
    # Navigation / auth
    markdown = _BACK_TO_MENU_RE.sub("\n", markdown)
    markdown = _CANCEL_RE.sub("\n", markdown)
    markdown = _SIGN_UP_LOG_IN_RE.sub("\n", markdown)
    markdown = _SIGN_UP_RE.sub("\n", markdown)

    # Action buttons
    markdown = _POST_DEAL_RE.sub("\n", markdown)
    markdown = _CREATE_ALERT_RE.sub("\n", markdown)

    # Forum rule links
    markdown = _NEW_POSTS_RE.sub("\n", markdown)
    markdown = _RSS_FEED_RE.sub("\n", markdown)
    markdown = _FORUM_RULES_RE.sub("\n", markdown)

    # Profile menu
    markdown = _MY_PROFILE_RE.sub("\n", markdown)
    markdown = _PROFILE_MENU_RE.sub("\n", markdown)

    # Thread navigation
    markdown = _SEARCH_THREAD_RE.sub("\n", markdown)
    markdown = _FIRST_NEW_POST_RE.sub("\n", markdown)
    markdown = _REPLY_TO_THREAD_RE.sub("\n", markdown)

    # Voting/stats
    markdown = _SCORE_LINE_RE.sub("\n", markdown)
    markdown = _SCORE_BREAKDOWN_RE.sub("\n", markdown)
    markdown = _VOTE_REASON_RE.sub("\n", markdown)
    markdown = _PERCENTAGE_LINE_RE.sub("\n", markdown)
    markdown = _VOTES_VIEWS_RE.sub("\n", markdown)
    markdown = _REPLIES_LINE_RE.sub("\n", markdown)
    markdown = _DEAL_SCORE_RE.sub("\n", markdown)

    # Thread listing metadata
    markdown = _LAST_POST_BY_RE.sub("\n", markdown)
    markdown = _BREADCRUMB_RE.sub("\n", markdown)
    markdown = _DEAL_STATUS_RE.sub("\n", markdown)
    markdown = _REPLIED_AGO_RE.sub("\n", markdown)
    markdown = _COLUMN_HEADERS_RE.sub("\n", markdown)
    markdown = _LAST_PAGE_RE.sub("\n", markdown)
    markdown = _DL_SCORE_RE.sub("\n", markdown)

    # Thread page: deal actions
    markdown = _GET_THIS_DEAL_RE.sub("\n", markdown)
    markdown = _ADD_DEAL_ALERT_RE.sub("\n", markdown)
    markdown = _SHARE_DEAL_RE.sub("\n", markdown)
    markdown = _COPIED_RE.sub("\n", markdown)
    markdown = _VERIFY_PRICING_RE.sub("\n", markdown)

    # Post-level actions
    markdown = _REPORT_RE.sub("\n", markdown)
    markdown = _SHARE_RE.sub("\n", markdown)
    markdown = _POST_NUMBER_RE.sub("\n", markdown)
    markdown = _REPLY_QUOTE_RE.sub("\n", markdown)

    # Topic information
    markdown = _TOPIC_INFO_RE.sub("\n", markdown)

    # Sort/filter controls
    markdown = _DISPLAY_POSTS_RE.sub("\n", markdown)
    markdown = _ALL_POSTS_FILTER_RE.sub("\n", markdown)
    markdown = _SORT_BY_RE.sub("\n", markdown)
    markdown = _ASC_DESC_RE.sub("\n", markdown)
    markdown = _GO_RE.sub("\n", markdown)

    # Thread display options / sort footer
    markdown = _THREAD_DISPLAY_OPTIONS_RE.sub("\n", markdown)
    markdown = _SHOW_THREADS_RE.sub("\n", markdown)

    # Promo blocks
    markdown = _COMPARE_CARDS_RE.sub("\n", markdown)
    markdown = _NEARBY_LOCATIONS_RE.sub("\n", markdown)

    # Footer
    markdown = _JUMP_TO_FORUM_RE.sub("\n", markdown)
    markdown = _POST_NEW_DEAL_RE.sub("\n", markdown)

    # Convert memberlist links to plain usernames
    markdown = _MEMBERLIST_LINK_RE.sub(r"\1", markdown)

    # Collapse excessive newlines
    markdown = _EXCESSIVE_NEWLINES.sub("\n\n", markdown).strip()
    return markdown
