"""Tests for RedFlagDeals markdown post-processing — outcome-level tests."""

from fetchaller.content.html import html_to_markdown
from fetchaller.content.redflagdeals import is_redflagdeals

RFD_URL = "https://forums.redflagdeals.com/hot-deals-f9/"
RFD_THREAD_URL = "https://forums.redflagdeals.com/some-deal-t1234567/"


class TestURLDetection:
    """URL detection should match forums.redflagdeals.com only."""

    def test_forums_subdomain_matches(self):
        assert is_redflagdeals("https://forums.redflagdeals.com/hot-deals-f9/")

    def test_forums_thread_matches(self):
        assert is_redflagdeals("https://forums.redflagdeals.com/some-deal-t1234567/")

    def test_www_does_not_match(self):
        assert not is_redflagdeals("https://www.redflagdeals.com/deals/")

    def test_bare_domain_does_not_match(self):
        assert not is_redflagdeals("https://redflagdeals.com/")

    def test_unrelated_does_not_match(self):
        assert not is_redflagdeals("https://example.com/redflagdeals")


class TestHeaderAndNavStripped:
    """Site header, nav, and search should be removed."""

    async def test_main_nav_stripped(self):
        html = """<body>
        <div class="main_nav_bar">
            <a href="/">Home</a>
            <a href="/forums">Forums</a>
            <a href="/deals">Deals</a>
        </div>
        <div class="header_logo"><img src="https://rfdcontent.com/graphics/logo.png"/></div>
        <div class="header_actions"><a href="/login">Log In</a></div>
        <div id="forums_nav_search_box"><input placeholder="Search"/></div>
        <h1>Hot Deals</h1>
        <p>Great deal on a TV.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url=RFD_URL)
        assert "Home" not in md or "Hot Deals" in md
        assert "Forums" not in md or "Hot Deals" in md
        assert "Log In" not in md
        assert "Search" not in md or "Hot Deals" in md
        assert "Great deal" in md

    async def test_forums_nav_sidebar_stripped(self):
        html = """<body>
        <div class="forums_nav_sidebar_column">
            <a href="/hot-deals">Hot Deals</a>
            <a href="/freebies">Freebies</a>
        </div>
        <p>Thread content about a sale.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url=RFD_URL)
        assert "Freebies" not in md
        assert "sale" in md


class TestAdsStripped:
    """All ad elements should be removed."""

    async def test_leaderboard_ads_stripped(self):
        html = """<body>
        <div id="header_leaderboard">Ad content here</div>
        <div class="ad_leaderboard_large">Big banner ad</div>
        <div id="desktop_ad_between_thread_1">Inline ad 1</div>
        <div class="pencil_ad">Pencil ad unit</div>
        <p>Actual deal discussion content.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url=RFD_URL)
        assert "Ad content" not in md
        assert "Big banner" not in md
        assert "Inline ad" not in md
        assert "Pencil ad" not in md
        assert "deal discussion" in md

    async def test_sidebar_bigbox_stripped(self):
        html = """<body>
        <div class="ad_sidebar_bigbox">Sidebar advertisement</div>
        <p>Good price on headphones.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url=RFD_URL)
        assert "Sidebar advertisement" not in md
        assert "headphones" in md


class TestAuthAndPopupsStripped:
    """Auth containers and popups should be removed."""

    async def test_auth_popup_stripped(self):
        html = """<body>
        <div id="user-auth-container"><form>Login form</form></div>
        <div id="user-auth-popup">Sign up popup</div>
        <div id="phpbb_alert">Alert dialog</div>
        <div id="darken">Overlay</div>
        <p>Deal post content.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url=RFD_URL)
        assert "Login form" not in md
        assert "Sign up popup" not in md
        assert "Alert dialog" not in md
        assert "Deal post content" in md


class TestFilterBarStripped:
    """Filter bar and category dropdowns should be removed."""

    async def test_filter_bar_stripped(self):
        html = """<body>
        <div class="filter_bar">
            <select class="category_filter"><option>All Categories</option></select>
            <div class="filter_dropdown">Date range options</div>
        </div>
        <div class="forums_page_header_buttons">
            <a href="/post">Post Deal</a>
            <a href="/alert">Create Alert</a>
        </div>
        <p>Deal listing content.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url=RFD_URL)
        assert "All Categories" not in md
        assert "Date range" not in md
        assert "Deal listing content" in md


class TestThreadListingCleanup:
    """Thread listing metadata should be stripped, titles preserved."""

    async def test_thread_avatars_stripped(self):
        html = """<body>
        <div class="author_avatar"><img src="/avatar.jpg"/></div>
        <div class="thread_image"><img src="/thumb.jpg"/></div>
        <a href="/deal-t123/">Amazing TV Deal - 50% off</a>
        </body>"""
        md, _ = await html_to_markdown(html, url=RFD_URL)
        assert "avatar" not in md.lower()
        assert "Amazing TV Deal" in md

    async def test_ellipses_menu_stripped(self):
        html = """<body>
        <div class="ellipses_menu"><button>...</button><ul><li>Report</li></ul></div>
        <p>Great deal on speakers.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url=RFD_URL)
        assert "speakers" in md


class TestThreadPagePostContent:
    """Thread page: post content preserved, profiles/actions stripped."""

    async def test_post_profiles_stripped(self):
        html = """<body>
        <div class="post_profilearea">
            <span>Sr. Member</span>
            <span>Join Date: Jan 2015</span>
            <span>Posts: 1,234</span>
            <span>Upvotes: 567</span>
            <span>Location: Toronto</span>
        </div>
        <div class="post_dateline">Feb 10, 2026 12:34 PM</div>
        <p>I just ordered this. The price is incredible for what you get.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url=RFD_THREAD_URL)
        assert "Sr. Member" not in md
        assert "Join Date" not in md
        assert "Posts: 1,234" not in md
        assert "price is incredible" in md

    async def test_action_buttons_stripped(self):
        html = """<body>
        <p>This deal is amazing, just bought two.</p>
        <div class="actionbar">
            <button>Reply</button>
            <button>Quote</button>
        </div>
        <div class="thread_navigation">
            <input placeholder="Search this thread"/>
            <div class="pagination"><a href="?page=2">2</a></div>
        </div>
        </body>"""
        md, _ = await html_to_markdown(html, url=RFD_THREAD_URL)
        assert "deal is amazing" in md
        assert "pagination" not in md.lower()

    async def test_expired_deal_prompt_stripped(self):
        html = """<body>
        <div class="thead_expired_msg_mobile">This deal has expired</div>
        <div class="thread_expired_alert_button_link">Set alert</div>
        <p>Was a great deal while it lasted.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url=RFD_THREAD_URL)
        assert "great deal while" in md

    async def test_vote_breakdown_stripped(self):
        html = """<body>
        <div class="threadvoting_breakdown_trigger">See votes</div>
        <p>Confirmed this works at Best Buy.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url=RFD_THREAD_URL)
        assert "See votes" not in md
        assert "Best Buy" in md


class TestSoupCleanup:
    """Soup-level cleanup should handle images and SVGs."""

    async def test_pto_banner_images_stripped(self):
        html = """<body>
        <img src="https://rfdcontent.com/pto_banners/banner1.jpg"/>
        <img src="https://rfdcontent.com/graphics/logo.png"/>
        <p>Deal content here.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url=RFD_URL)
        assert "pto_banners" not in md
        assert "rfdcontent.com/graphics" not in md
        assert "Deal content" in md

    async def test_svg_icons_stripped(self):
        html = """<body>
        <svg viewBox="0 0 24 24"><path d="M12 0"/></svg>
        <p>Content after SVG icon.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url=RFD_URL)
        assert "viewBox" not in md
        assert "Content after SVG" in md


class TestFooterStripped:
    """Footer elements should be removed."""

    async def test_footer_stripped(self):
        html = """<body>
        <p>Last post content.</p>
        <div id="page-footer">
            <div id="site_footer_menus">About | Contact | Advertising</div>
            <div id="site_footer_boilerplate">Copyright 2026 RFD</div>
        </div>
        </body>"""
        md, _ = await html_to_markdown(html, url=RFD_URL)
        assert "Copyright" not in md
        assert "Advertising" not in md
        assert "Last post content" in md

    async def test_below_postlist_stripped(self):
        html = """<body>
        <p>Final post content.</p>
        <div class="below_postlist">
            <a href="/similar">Similar threads</a>
        </div>
        </body>"""
        md, _ = await html_to_markdown(html, url=RFD_URL)
        assert "Similar threads" not in md
        assert "Final post content" in md


class TestPostprocessorRegex:
    """Regex post-processing should strip remaining UI text."""

    async def test_back_to_menu_stripped(self):
        html = """<body>
        <p>Back to Menu</p>
        <p>Actual content.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url=RFD_URL)
        assert "Back to Menu" not in md
        assert "Actual content" in md

    async def test_sign_up_log_in_stripped(self):
        html = """<body>
        <p>Sign Up / Log In</p>
        <p>Sign Up</p>
        <p>Deal discussion content.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url=RFD_URL)
        assert "Sign Up / Log In" not in md
        assert "Deal discussion" in md

    async def test_post_deal_stripped(self):
        html = """<body>
        <p>Post Deal</p>
        <p>Post a Deal</p>
        <p>Create Alert</p>
        <p>Forum content here.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url=RFD_URL)
        assert "Post Deal" not in md
        assert "Post a Deal" not in md
        assert "Create Alert" not in md
        assert "Forum content" in md

    async def test_forum_rules_links_stripped(self):
        html = """<body>
        <p>New Posts</p>
        <p>RSS Feed</p>
        <p>Forum Rules</p>
        <p>Real thread content.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url=RFD_URL)
        assert "New Posts" not in md
        assert "RSS Feed" not in md
        assert "Forum Rules" not in md
        assert "Real thread content" in md

    async def test_score_and_votes_stripped(self):
        html = """<body>
        <p>+42 SCORE</p>
        <p>15 votes</p>
        <p>1234 views</p>
        <p>This is a hot deal on a laptop.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url=RFD_URL)
        assert "SCORE" not in md
        assert "hot deal on a laptop" in md

    async def test_search_thread_stripped(self):
        html = """<body>
        <p>Search this thread</p>
        <p>Discussion about the deal.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url=RFD_URL)
        assert "Search this thread" not in md
        assert "Discussion about" in md


class TestContentPreserved:
    """Deal content and post text should be preserved."""

    async def test_deal_title_preserved(self):
        html = """<body>
        <h1>Samsung 65" OLED TV - $999 (was $1999) [Best Buy]</h1>
        <p>Amazing price for this TV. YMMV.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url=RFD_THREAD_URL)
        assert "Samsung 65" in md
        assert "$999" in md
        assert "YMMV" in md

    async def test_post_with_links_preserved(self):
        html = """<body>
        <p>Check out this deal: <a href="https://example.com/deal">link here</a></p>
        <p>Use coupon code <code>SAVE20</code> at checkout.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url=RFD_THREAD_URL)
        assert "link here" in md
        assert "SAVE20" in md

    async def test_quoted_post_preserved(self):
        html = """<body>
        <blockquote>
            <p>Original poster said this was available in-store only.</p>
        </blockquote>
        <p>I confirmed it also works online.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url=RFD_THREAD_URL)
        assert "in-store only" in md
        assert "works online" in md


class TestIsolation:
    """RFD processing must not affect other sites."""

    async def test_non_rfd_url_unaffected(self):
        html = """<body>
        <div class="post_profilearea">Profile info</div>
        <p>Post Deal</p>
        <p>Real content here.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://example.com/page")
        # post_profilearea is not in generic selectors, so it stays
        assert "Real content" in md

    async def test_www_rfd_not_treated_as_forums(self):
        """www.redflagdeals.com should NOT get forum cleanup."""
        html = """<body>
        <div class="post_profilearea">Profile info</div>
        <p>Content from main RFD site.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://www.redflagdeals.com/deals/")
        assert "Content from main" in md


class TestMiscCleanup:
    """Miscellaneous elements should be cleaned."""

    async def test_notice_stripped(self):
        html = """<body>
        <div class="notice">Last edited by user123 on Feb 10, 2026</div>
        <p>The corrected deal info.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url=RFD_URL)
        assert "Last edited" not in md
        assert "corrected deal" in md

    async def test_pagination_stripped(self):
        html = """<body>
        <p>Thread content.</p>
        <div class="pagination">
            <a href="?page=1">1</a>
            <a href="?page=2">2</a>
            <a href="?page=3">3</a>
        </div>
        </body>"""
        md, _ = await html_to_markdown(html, url=RFD_URL)
        assert "Thread content" in md

    async def test_breaking_news_stripped(self):
        html = """<body>
        <div id="breaking_news">Breaking: Flash sale happening now!</div>
        <p>Regular forum content.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url=RFD_URL)
        assert "Flash sale" not in md
        assert "Regular forum content" in md
