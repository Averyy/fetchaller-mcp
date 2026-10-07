"""homedepot.com pages on the HTML path: article cleanup and landing pages.

An article rendered with the site's chrome around it -- a 1,400-character
category menu at the top, the footer's legal links and a survey URL at the
bottom -- and with every control as text: "1. 1" in the table of contents,
"0 / 0" under each slideshow, "See More" as a paragraph. A landing page
(customer service, the DIY hub) draws its sections from __APOLLO_STATE__, and
the server's HTML holds only some of them: the customer service page came back
as a banner under its title. These tests pin both.
"""

import json

from fetchaller.content.homedepot import _rich_text, is_homedepot_com
from fetchaller.content.html import _detect_site, _html_to_markdown_sync

ARTICLE_URL = "https://www.homedepot.com/c/ab/drill-bit-buying-guide/9ba683603be9fa5395fab9026af9044"
LANDING_URL = "https://www.homedepot.com/c/customer_service"

HEADER = (
    '<div class="hfapp"><div id="header-earned-media-links">'
    '<a href="/b/Appliances/N-5yc1vZbv1w">Appliances</a><a href="/b/Bath/N-5yc1vZbzb3">Bath</a></div>'
    '<div id="header-static">Select store <a href="/cart">Cart</a> Log In</div></div>'
)
FOOTER = (
    '<div id="footer-static" class="hfapp"><a href="/c/Terms_of_Use">Terms of Use</a>'
    '<a href="https://homedepot.az1.qualtrics.com/jfe/form/SV_x?long=survey">Provide Feedback</a></div>'
)
BREADCRUMBS = (
    '<div data-component="breadcrumbs:Breadcrumbs:v11.10.3">'
    '<div class="breadcrumb__item"><a href="/">Home</a></div>'
    '<div class="breadcrumb__item"><div aria-hidden="true">/</div>'
    '<a href="/c/diy_projects_and_ideas">DIY Projects &amp; Ideas</a></div>'
    '<div class="breadcrumb__item"><div aria-hidden="true">/</div><a href="/c/alp/diy-workshops/sdki">DIY Workshops</a></div>'
    '<div class="breadcrumb__item"><div aria-hidden="true">/</div><span>DIY Workshops</span></div></div>'
)


def _article() -> str:
    return f"""<html><head><title>Choose the Best Drill Bits - The Home Depot</title></head><body>
    {HEADER}
    <div id="root" class="article experience">
      {BREADCRUMBS}
      <h1>Choose the Best Drill Bits</h1>
      <div data-testid="tags">
        <div><span>Buying Guide</span></div><div><span>DIY Workshops</span></div>
        <div><span>DIY Workshops</span></div><div><span>Drills</span></div>
      </div>
      <h2>Table of Contents</h2>
      <ol>
        <li aria-label="Step 1 out of 2."><button type="button"><span>1</span></button><span><p>What Are Drill Bits Used For?</p></span></li>
        <li aria-label="Step 2 out of 2."><button type="button"><span>2</span></button><span><p>Twist Drill Bit</p></span></li>
      </ol>
      <div><button type="button">See <!-- -->More</button></div>
      <h2>What Are Drill Bits Used For?</h2>
      <div><div class="swiper"><div class="swiper-wrapper"><div class="swiper-slide">
        <img alt="A drill bit in metal." src="https://dam.thdstatic.com/a.jpg"></div></div>
        <div class="swiper-scrollbar"></div><div><div>0 / 0</div></div></div></div>
      <p>Drill bits come in different styles.</p>
      <button type="button">What size bit do I need for a 1/4-inch anchor?</button>
      <div><button type="button">Show <!-- -->More</button></div>
      <div data-component="emt-links:EmtLinks:v9.3.2"><a href="/b/x">Cordless Drills</a><a href="/b/y">Drill Sets</a></div>
    </div>
    {FOOTER}
    </body></html>"""


def test_homedepot_com_is_its_own_site_and_ca_is_not():
    assert is_homedepot_com(ARTICLE_URL)
    assert not is_homedepot_com("https://www.homedepot.ca/en/home/ideas-how-to/tools/types-of-drill-bits.html")
    assert _detect_site(ARTICLE_URL, False, None) == "homedepot"


class TestArticle:
    def _render(self) -> str:
        markdown, _title = _html_to_markdown_sync(_article(), url=ARTICLE_URL)
        return markdown

    def test_header_footer_and_seo_links_are_gone(self):
        out = self._render()
        for chrome in ("Appliances", "Select store", "Cart", "Terms of Use", "qualtrics", "Cordless Drills"):
            assert chrome not in out, chrome

    def test_breadcrumb_is_one_line_without_the_repeated_current_page(self):
        out = self._render()
        assert "[Home](/) / [DIY Projects & Ideas](/c/diy_projects_and_ideas) / [DIY Workshops](/c/alp/diy-workshops/sdki)\n" in out
        assert out.count("DIY Workshops") == 2  # the crumb and the tag, never a third

    def test_tags_are_one_deduplicated_line(self):
        assert "Tags: Buying Guide · DIY Workshops · Drills" in self._render()

    def test_controls_are_not_page_text(self):
        out = self._render()
        assert "1. What Are Drill Bits Used For?\n2. Twist Drill Bit" in out
        assert "1. 1" not in out
        assert "0 / 0" not in out
        assert "See More" not in out and "Show More" not in out

    def test_slides_survive_and_a_button_with_real_words_is_kept(self):
        out = self._render()
        assert "![A drill bit in metal.](https://dam.thdstatic.com/a.jpg)" in out
        assert "What size bit do I need for a 1/4-inch anchor?" in out


# ---------------------------------------------------------------------------
# Landing pages
# ---------------------------------------------------------------------------


def _doc(*blocks):
    return json.dumps({"nodeType": "document", "data": {}, "content": list(blocks)})


def _p(*inlines):
    return {"nodeType": "paragraph", "data": {}, "content": list(inlines)}


def _t(value, *marks):
    return {"nodeType": "text", "value": value, "marks": [{"type": m} for m in marks], "data": {}}


def _a(uri, label):
    return {"nodeType": "hyperlink", "data": {"uri": uri}, "content": [_t(label)]}


STATE = {
    "ROOT_QUERY": {"layouts({\"slug\":\"customer-service\"})": {"__ref": "UniversalLayout:1"}},
    "UniversalLayout:1": {
        "__typename": "UniversalLayout",
        "title": "Customer Service Center",
        "sponsoredTopBanner": {"__ref": "SponsoredTopBanner:1"},
        "flexibleCluster2": [{"__ref": "Section:help"}],
        "flexibleCluster1": [{"__ref": "Section:do"}],
    },
    "SponsoredTopBanner:1": {"__typename": "SponsoredTopBanner", "clickthruUrl": "https://www.homedepot.com/c/delivery"},
    "Section:do": {"__typename": "Section", "title": "WHAT WOULD YOU LIKE TO DO?", "components": [{"__ref": "VisualNavigation:1"}]},
    "VisualNavigation:1": {
        "__typename": "VisualNavigation",
        "title": None,
        "visualNavigationList": [{"__ref": "VisualNavigationItem:1"}, {"__ref": "VisualNavigationItem:2"}],
    },
    "VisualNavigationItem:1": {"__typename": "VisualNavigationItem", "title": None, "altText": "Track An Order", "link": "/order/view/tracking"},
    "VisualNavigationItem:2": {"__typename": "VisualNavigationItem", "altText": "Odd Link", "link": "javascript:alert(1)"},
    "Section:help": {
        "__typename": "Section",
        "title": "Explore Help Topics",
        "components": [{"__ref": "ContentAccordion:1"}, {"__ref": "CapabilityCard:1"}, {"__ref": "Spotlight:1"}, {"__ref": "MedioInline:1"}],
    },
    "ContentAccordion:1": {
        "__typename": "ContentAccordion",
        "title": "Orders & Purchases",
        "subtitle": "Track an Order, Return Policies",
        "description": _doc(_p(_t("Fix Your Issue Now", "bold")), _p(_a("https://www.homedepot.com/c/Return_Policy", "Return Policy"))),
    },
    "CapabilityCard:1": {
        "__typename": "CapabilityCard",
        "headline": "Text Us",
        "richTextContent": _doc(_p(_t("Text SUPPORT to "), _a("sms:38698", "38698"), _t(" any time."))),
    },
    "Spotlight:1": {"__typename": "Spotlight", "title": "CONSUMER CREDIT CARD", "description": "1-800-677-0232 or TTY:711", "richTextContent": ""},
    "MedioInline:1": {"__typename": "MedioInline", "altText": "Download on the App Store", "link": "https://thd.onelink.me/x"},
}


def _landing(state: dict | None) -> str:
    # Script contents are raw text on the real page; never HTML-escaped.
    script = f"<script>window.__APOLLO_STATE__={json.dumps(state)};</script>" if state else ""
    return f"""<html><head><title>Customer Service Center - The Home Depot</title></head><body>
    {HEADER}
    <div id="root" class="landing-page experience">
      <div data-component="breadcrumbs:Breadcrumbs:v11.10.3">
        <div class="breadcrumb__item"><a href="/">Home</a></div>
        <div class="breadcrumb__item"><div aria-hidden="true">/</div><span>Customer Service Center</span></div>
      </div>
      <a href="https://www.homedepot.com/c/delivery"><img alt="sponsored banner" src="https://dam.thdstatic.com/b.png"></a>
      <h1>Customer Service Center</h1>
    </div>
    {FOOTER}{script}
    </body></html>"""


class TestLanding:
    def _render(self, state=STATE) -> str:
        markdown, _title = _html_to_markdown_sync(_landing(state), url=LANDING_URL)
        return markdown

    def test_sections_render_from_the_layout_in_slot_order(self):
        out = self._render()
        assert out.startswith("# Customer Service Center\n\n[Home](https://www.homedepot.com/) / Customer Service Center")
        assert out.index("## WHAT WOULD YOU LIKE TO DO?") < out.index("## Explore Help Topics")
        assert "- [Track An Order](https://www.homedepot.com/order/view/tracking)" in out

    def test_rich_text_cards_and_spotlights_keep_their_words_and_links(self):
        out = self._render()
        assert "### Orders & Purchases\n\nTrack an Order, Return Policies\n\n**Fix Your Issue Now**" in out
        assert "[Return Policy](https://www.homedepot.com/c/Return_Policy)" in out
        assert "### Text Us\n\nText SUPPORT to [38698](sms:38698) any time." in out
        assert "### CONSUMER CREDIT CARD\n\n1-800-677-0232 or TTY:711" in out

    def test_paid_placements_chrome_and_unsafe_links_stay_out(self):
        out = self._render()
        assert "sponsored banner" not in out
        assert "App Store" not in out
        assert "Appliances" not in out and "Terms of Use" not in out
        assert "- Odd Link\n" in out or out.rstrip().endswith("- Odd Link")
        assert "javascript:" not in out

    def test_without_layout_data_the_html_path_still_runs(self):
        out = self._render(state=None)
        assert "# Customer Service Center" in out
        assert "[Home](/) / Customer Service Center" in out
        assert "Appliances" not in out


def test_a_section_does_not_repeat_its_only_components_title():
    state = {
        "UniversalLayout:1": {"__typename": "UniversalLayout", "title": "DIY", "flexibleCluster1": [{"__ref": "Section:1"}]},
        "Section:1": {"__typename": "Section", "title": "Free Workshops", "components": [{"__ref": "HeroFlattenImage:1"}]},
        "HeroFlattenImage:1": {
            "__typename": "HeroFlattenImage",
            "title": "Free Workshops",
            "previewImage": {"imageHotspot": {"__ref": "ImageHotspot:1"}},
        },
        "ImageHotspot:1": {"__typename": "ImageHotspot", "hotspotActions": [{"__ref": "HotspotAction:1"}]},
        "HotspotAction:1": {"__typename": "HotspotAction", "title": "Kids Workshops", "url": "https://www.homedepot.com/c/kids-workshop"},
    }
    markdown, _ = _html_to_markdown_sync(_landing(state), url=LANDING_URL)
    assert "## Free Workshops\n\n- [Kids Workshops](https://www.homedepot.com/c/kids-workshop)" in markdown
    assert "### Free Workshops" not in markdown


def test_rich_text_lists_headings_and_plain_strings():
    doc = json.dumps({
        "nodeType": "document",
        "content": [
            {"nodeType": "heading-3", "content": [_t("Call Us")]},
            {"nodeType": "ordered-list", "content": [
                {"nodeType": "list-item", "content": [_p(_t("First"))]},
                {"nodeType": "list-item", "content": [_p(_t("Second", "italic"))]},
            ]},
        ],
    })
    assert _rich_text(doc) == "#### Call Us\n\n1. First\n2. *Second*"
    assert _rich_text("not json at all") == "not json at all"
    assert _rich_text("") == ""


def test_nested_rich_text_lists_and_odd_links_survive_the_page_round_trip():
    nested = json.dumps({"nodeType": "document", "content": [{"nodeType": "unordered-list", "content": [
        {"nodeType": "list-item", "content": [
            _p(_t("parent")),
            {"nodeType": "unordered-list", "content": [{"nodeType": "list-item", "content": [_p(_t("nested"))]}]},
        ]},
    ]}]})
    state = {
        "UniversalLayout:1": {"__typename": "UniversalLayout", "title": "Help", "flexibleCluster1": [{"__ref": "Section:1"}]},
        "Section:1": {"__typename": "Section", "title": "Topics", "components": [{"__ref": "CapabilityCard:1"}, {"__ref": "VisualNavigation:1"}]},
        "CapabilityCard:1": {"__typename": "CapabilityCard", "headline": "Lists", "richTextContent": nested},
        "VisualNavigation:1": {"__typename": "VisualNavigation", "visualNavigationList": [{"__ref": "VisualNavigationItem:1"}]},
        "VisualNavigationItem:1": {"__typename": "VisualNavigationItem", "altText": "Odd [label]", "link": "https://www.homedepot.com/c/a(b)"},
    }
    markdown, _ = _html_to_markdown_sync(_landing(state), url=LANDING_URL)
    assert "- parent\n  - nested" in markdown
    assert "- [Odd \\[label\\]](https://www.homedepot.com/c/a%28b%29)" in markdown
