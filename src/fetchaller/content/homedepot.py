"""homedepot.com pages that take the HTML path: articles, guides and landing pages.

Exports the standard site interface (SELECTORS_LIST, is_homedepot_com,
extract_landing_layout, strip_homedepot_junk, postprocess_homedepot).

Products, listings, reviews and stores never get here; they are read from the
site's own federation gateway (``fetchaller.homedepot``). What does get here is
wrapped in the site's chrome, which markdownify renders as page text: the
header app (a ~1,400-character category mega-menu, the store picker, the cart),
the footer app (brand logos, legal links, a 1.5 KB survey URL) and an "earned
media" block of SEO links. Inside an article every control renders too: the
table of contents prints each step number twice ("1. 1"), slideshows print
their unhydrated "0 / 0" counter, and "See More"/"Show More" become paragraphs.
Only labelled controls are removed: a blanket button rule would also take any
button whose label is content.

**Landing pages are read from their layout data, never their HTML.** A
``#root.landing-page`` (``/c/customer_service``, ``/c/diy_projects_and_ideas``)
is a Contentful ``UniversalLayout`` whose sections the browser draws from
``__APOLLO_STATE__``. The server's HTML draws some of them and not others: the
customer service page ships its breadcrumb and heading and none of its help
topics, cards or contact numbers, so the HTML path returned a banner under a
title -- a page that looks read and is empty. Every section is in the state, in
slot order, so the page is rendered from there.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from ..homedepot.urls import COM_HOSTS, COM_ORIGIN


def is_homedepot_com(url: str) -> bool:
    """Check if URL is a homedepot.com page."""
    return (urlparse(url).hostname or "").lower() in COM_HOSTS


SELECTORS_LIST = [
    # Header app (mega-menu, store picker, cart) and footer app (#footer-static).
    ".hfapp",
    # "Earned media" SEO link block under the article.
    '[data-component^="emt-links:"]',
]

# Button labels that are page controls, not content.
_CONTROL_LABELS = frozenset(
    {"see more", "see less", "show more", "show less", "view more", "view less", "load more"}
)


# ---------------------------------------------------------------------------
# Articles and every other HTML-path page
# ---------------------------------------------------------------------------


def _breadcrumbs(trail) -> list[tuple[str, str | None]]:
    """A breadcrumb trail element as (text, href) pairs.

    The site lists the current page twice -- as the last link and again as plain
    text -- so a trailing plain crumb that repeats the one before it is dropped.
    """
    if trail is None:
        return []
    crumbs: list[tuple[str, str | None]] = []
    for item in trail.select(".breadcrumb__item"):
        link = item.find("a", href=True)
        text = " ".join((link or item).get_text(" ", strip=True).split()).strip("/ ")
        if text:
            crumbs.append((text, link["href"] if link else None))
    if len(crumbs) > 1 and crumbs[-1][1] is None and crumbs[-1][0] == crumbs[-2][0]:
        crumbs.pop()
    return crumbs


def _compact_breadcrumbs(soup: BeautifulSoup) -> None:
    """One linked line instead of a crumb and a "/" per paragraph."""
    for trail in soup.select('[data-component^="breadcrumbs:"]'):
        crumbs = _breadcrumbs(trail)
        if not crumbs:
            trail.decompose()
            continue
        line = soup.new_tag("p")
        for index, (text, href) in enumerate(crumbs):
            if index:
                line.append(" / ")
            if href:
                anchor = soup.new_tag("a", href=href)
                anchor.string = text
                line.append(anchor)
            else:
                line.append(text)
        trail.replace_with(line)


def _compact_tags(soup: BeautifulSoup) -> None:
    """The article's topic chips, once each, on one line."""
    for chips in soup.select('[data-testid="tags"]'):
        tags: list[str] = []
        for chip in chips.find_all(recursive=False):
            text = " ".join(chip.get_text(" ", strip=True).split())
            if text and text not in tags:
                tags.append(text)
        if not tags:
            chips.decompose()
            continue
        line = soup.new_tag("p")
        line.string = "Tags: " + " · ".join(tags)
        chips.replace_with(line)


def strip_homedepot_junk(soup: BeautifulSoup) -> None:
    """Remove page controls and compact navigation inside a homedepot.com page."""
    for button in soup.find_all("button"):
        label = " ".join(button.get_text(" ", strip=True).split()).casefold()
        if label.isdigit() or label in _CONTROL_LABELS:
            button.decompose()
    # A slideshow keeps its slides; its scrollbar and "0 / 0" counter go.
    for slideshow in soup.select("div.swiper"):
        for child in slideshow.find_all(recursive=False):
            if "swiper-wrapper" not in (child.get("class") or []):
                child.decompose()
    _compact_breadcrumbs(soup)
    _compact_tags(soup)


# ---------------------------------------------------------------------------
# Landing pages (Contentful UniversalLayout in __APOLLO_STATE__)
# ---------------------------------------------------------------------------

_APOLLO_ASSIGNMENT = "__APOLLO_STATE__="
_MAX_APOLLO_CHARS = 8 * 1024 * 1024
_MARKER = "__HD_LANDING_MARKER__"
_NL_TOKEN = "__HD_LANDING_NL__"
_INDENT_TOKEN = "__HD_LANDING_SP__"
_MARKER_RE = re.compile(re.escape(_MARKER) + r"([\s\S]*?)" + re.escape(_MARKER))
_SAFE_LINK = re.compile(r"(?:https?://|tel:|sms:|mailto:)\S+\Z", re.IGNORECASE)
# Paid placements and SEO furniture, not page content.
_SKIPPED_TYPES = frozenset(
    {
        "SponsoredTopBanner",
        "SponsoredBottomCarousel",
        "SponsoredMiddleBanner",
        "SponsoredBanner",
        "SeoLinks",
        "RelatedSearchesAndProducts",
        "MedioInline",
    }
)


def _apollo_state(soup: BeautifulSoup) -> dict | None:
    for script in soup.find_all("script"):
        text = script.string or ""
        start = text.find(_APOLLO_ASSIGNMENT)
        if start < 0 or len(text) > _MAX_APOLLO_CHARS:
            continue
        try:
            state, _ = json.JSONDecoder().raw_decode(text[start + len(_APOLLO_ASSIGNMENT) :])
        except ValueError:
            return None
        return state if isinstance(state, dict) else None
    return None


def _link(uri: object) -> str | None:
    if not isinstance(uri, str):
        return None
    uri = uri.strip()
    if uri.startswith("/") and not uri.startswith("//"):
        uri = COM_ORIGIN + uri
    return uri if _SAFE_LINK.match(uri) else None


def _clean(text: object) -> str:
    return " ".join(str(text).split()) if isinstance(text, str) else ""


def _rich_text(raw: object) -> str:
    """Contentful rich text (a JSON document string) as markdown."""
    if not isinstance(raw, str) or not raw.strip():
        return ""
    try:
        document = json.loads(raw)
    except ValueError:
        return _clean(raw)

    def inline(node: dict) -> str:
        kind = node.get("nodeType")
        if kind == "text":
            value = str(node.get("value") or "")
            marks = {m.get("type") for m in node.get("marks") or [] if isinstance(m, dict)}
            stripped = value.strip()
            if stripped and "bold" in marks:
                value = value.replace(stripped, f"**{stripped}**", 1)
            elif stripped and "italic" in marks:
                value = value.replace(stripped, f"*{stripped}*", 1)
            return value
        children = "".join(inline(c) for c in node.get("content") or [] if isinstance(c, dict))
        if kind == "hyperlink":
            href = _link((node.get("data") or {}).get("uri"))
            return _linked(children.strip(), href) if href and children.strip() else children
        return children

    def block(node: dict, depth: int = 0) -> list[str]:
        kind = str(node.get("nodeType") or "")
        children = [c for c in node.get("content") or [] if isinstance(c, dict)]
        if kind in ("document", "list-item", "blockquote"):
            out: list[str] = []
            for child in children:
                out.extend(block(child, depth))
            return out
        if kind.startswith("heading-"):
            text = _clean("".join(inline(c) for c in children))
            return [f"#### {text}"] if text else []
        if kind in ("unordered-list", "ordered-list"):
            lines = []
            for number, item in enumerate(children, 1):
                # The item's paragraph, then any nested list on its own lines.
                text = "\n".join(block(item, depth + 1)).strip()
                if text:
                    bullet = f"{number}." if kind == "ordered-list" else "-"
                    lines.append(f"{'  ' * depth}{bullet} {text}")
            return ["\n".join(lines)] if lines else []
        if kind == "paragraph":
            text = _clean("".join(inline(c) for c in children))
            return [text] if text else []
        return []

    return "\n\n".join(block(document)) if isinstance(document, dict) else ""


def _resolve(state: dict, ref: object) -> dict | None:
    if isinstance(ref, dict) and "__ref" in ref:
        record = state.get(ref["__ref"])
        return record if isinstance(record, dict) else None
    return ref if isinstance(ref, dict) else None


def _linked(label: str, href: str | None) -> str:
    """``[label](href)``, escaped so neither can break out of the link."""
    if not href:
        return label
    label = label.replace("[", "\\[").replace("]", "\\]")
    href = href.replace("(", "%28").replace(")", "%29").replace(" ", "%20")
    return f"[{label}]({href})"


def _component(state: dict, record: dict) -> list[str]:
    """One layout component as markdown blocks."""
    kind = record.get("__typename")
    if kind in _SKIPPED_TYPES:
        return []
    if kind == "Section":
        blocks = []
        title = _clean(record.get("title"))
        if title:
            blocks.append(f"## {title}")
        for child in record.get("components") or []:
            resolved = _resolve(state, child)
            if not resolved:
                continue
            rendered = _component(state, resolved)
            # A component that repeats its section's title would print it twice.
            if rendered and title and rendered[0].casefold() == f"### {title}".casefold():
                rendered = rendered[1:]
            blocks.extend(rendered)
        return blocks if len(blocks) > (1 if title else 0) else []
    if kind in ("VisualNavigation", "PromoVisualNavigation"):
        items = record.get("visualNavigationList") or record.get("promoVisualNavigationList") or []
        lines = []
        for ref in items:
            item = _resolve(state, ref) or {}
            offer = item.get("promotionalOffer") if isinstance(item.get("promotionalOffer"), dict) else {}
            label = (
                _clean(item.get("title"))
                or _clean(offer.get("simpleOfferHeadline"))
                or _clean(item.get("altText"))
            )
            if not label:
                continue
            detail = _clean(item.get("description")) or _clean(offer.get("simpleOfferSubhead"))
            lines.append(f"- {_linked(label, _link(item.get('link')))}" + (f" — {detail}" if detail else ""))
        title = _clean(record.get("title"))
        return ([f"### {title}"] if title and lines else []) + (["\n".join(lines)] if lines else [])
    if kind in ("SideNavigation",):
        lines = []
        for ref in record.get("navigationList") or []:
            item = _resolve(state, ref) or {}
            label = _clean(item.get("title"))
            if label:
                lines.append(f"- {_linked(label, _link(item.get('link')))}")
        return ["\n".join(lines)] if lines else []
    if kind in ("ImageHotspot", "HeroFlattenImage"):
        hotspot = record if kind == "ImageHotspot" else _resolve(state, (record.get("previewImage") or {}).get("imageHotspot"))
        lines = []
        for ref in (hotspot or {}).get("hotspotActions") or []:
            action = _resolve(state, ref) or {}
            label = _clean(action.get("title"))
            if label:
                lines.append(f"- {_linked(label, _link(action.get('url')))}")
        title = _clean(record.get("title"))
        return ([f"### {title}"] if title and lines else []) + (["\n".join(lines)] if lines else [])
    # Card-like components: ContentAccordion, CapabilityCard, Spotlight, Hero and
    # anything else that carries a heading and text.
    heading = _clean(record.get("title")) or _clean(record.get("headline"))
    blocks = [f"### {heading}"] if heading else []
    for field in ("subtitle", "eyebrow", "description"):
        value = record.get(field)
        text = _rich_text(value) if isinstance(value, str) and value.lstrip().startswith("{") else _clean(value)
        if text:
            blocks.append(text)
    body = _rich_text(record.get("richTextContent"))
    if body:
        blocks.append(body)
    cta = _clean(record.get("cta"))
    href = _link(record.get("link"))
    if href:
        blocks.append(_linked(cta or heading or href, href))
    links = []
    for ref in record.get("linkList") or []:
        item = _resolve(state, ref) or {}
        label = _clean(item.get("title")) or _clean(item.get("text"))
        target = _link(item.get("link") or item.get("url"))
        if label:
            links.append(f"- {_linked(label, target)}")
    if links:
        blocks.append("\n".join(links))
    return blocks if len(blocks) > (1 if heading else 0) else []


def _landing_markdown(soup: BeautifulSoup) -> str | None:
    root = soup.find(id="root")
    if root is None or "landing-page" not in (root.get("class") or []):
        return None
    state = _apollo_state(soup)
    if not state:
        return None
    layout = next(
        (v for v in state.values() if isinstance(v, dict) and v.get("__typename") == "UniversalLayout"),
        None,
    )
    if layout is None:
        return None
    sections: list[str] = []
    side = _resolve(state, layout.get("sideNavigation"))
    if side:
        sections.extend(_component(state, side))
    hero = _resolve(state, layout.get("heroCarousel"))
    if hero:
        sections.extend(_component(state, hero))
    clusters = sorted(
        (key for key in layout if re.fullmatch(r"flexibleCluster\d+", key)),
        key=lambda key: int(key.removeprefix("flexibleCluster")),
    )
    for key in clusters:
        for ref in layout.get(key) or []:
            record = _resolve(state, ref)
            if record:
                sections.extend(_component(state, record))
    if not sections:
        return None
    title = _clean(layout.get("title")) or (soup.title.get_text(strip=True) if soup.title else "")
    head = [f"# {title}"] if title else []
    crumbs = _breadcrumbs(soup.select_one('[data-component^="breadcrumbs:"]'))
    if crumbs:
        head.append(" / ".join(_linked(text, _link(href) if href else None) for text, href in crumbs))
    return "\n\n".join(head + sections)


def extract_landing_layout(soup: BeautifulSoup) -> None:
    """Replace a landing page's half-drawn HTML with its full layout data.

    Leaves every other page alone, and a landing page too when its state cannot
    be read, so the HTML path still runs.
    """
    rendered = _landing_markdown(soup)
    if not rendered:
        return
    body = soup.find("body")
    if body is None:
        return
    body.clear()
    marker = soup.new_tag("div", id="hd-landing-marker")
    # markdownify collapses whitespace in a text node, so newlines and the
    # indentation of nested list items travel as tokens.
    encoded = _NL_TOKEN.join(
        _INDENT_TOKEN * (len(line) - len(line.lstrip(" "))) + line.lstrip(" ")
        for line in rendered.split("\n")
    )
    marker.string = _MARKER + encoded + _MARKER
    body.append(marker)


def postprocess_homedepot(markdown: str) -> str:
    """Lift a rendered landing page back out of its marker, untouched by markdownify."""
    match = _MARKER_RE.search(markdown)
    if not match:
        return markdown
    return match.group(1).replace(_NL_TOKEN, "\n").replace(_INDENT_TOKEN, " ").strip() + "\n"
