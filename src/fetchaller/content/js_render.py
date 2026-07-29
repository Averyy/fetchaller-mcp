"""Detect pages whose content is assembled by JavaScript in the browser.

fetchaller does not execute JavaScript, so a client-rendered page comes back as
whatever the server actually sent — often a near-empty shell, sometimes a real
page with one region still showing a spinner. Both extract "successfully", which
is the problem: the caller cannot tell "this site has no jobs listed" from "the
jobs arrive after hydration" and records the wrong conclusion.

This module does not fix that; it names it, and salvages what the shell does
carry. A page whose body never rendered still ships its metadata — a Framer site
that extracts to nothing but its title still declares a description, a canonical
URL, and its company profiles. That is real signal for anyone cataloguing sites,
and discarding it turns a recoverable page into a blank.

The markers and metadata are collected from the DOM inside the disposable parser
process, and the verdict is reached by :func:`describe` once the extracted text
is in hand — how much survived extraction is the strongest signal available, and
it is not known until then.
"""

from __future__ import annotations

import json

from bs4 import BeautifulSoup, Tag

# Containers a framework mounts into. Empty in the served HTML, filled on
# hydration — the single clearest signal that the real page never shipped.
_MOUNT_POINT_IDS = frozenset({"root", "app", "__next", "__nuxt", "__svelte", "main-app"})
_MOUNT_POINT_TAGS = frozenset({"app-root", "ember-view"})

# Substrings that mark an element as a loading placeholder. Matched against
# class names only — never raw markup, where a site's stylesheet or a hidden
# template would match text that is not on the page.
_LOADING_CLASS_HINTS = ("spinner", "loader", "loading", "skeleton", "placeholder-shimmer")

# A real placeholder is an empty container waiting to be filled. Utility-class
# frameworks put these same words in the class list of ordinary links and
# buttons (Tailwind's `group/trigger ... spinner` variants, for one), so an
# element that already has its own text is a false match, and so is anything
# interactive or inline. Both conditions are required.
_LOADING_PLACEHOLDER_TAGS = frozenset(
    {"div", "section", "main", "aside", "article", "ul", "ol", "table", "tbody"}
)

# Frameworks whose presence hints at client rendering. Presence alone is NOT a
# verdict — plenty of Next.js pages are fully server-rendered — it only
# corroborates a page that also extracted to nothing.
_FRAMEWORK_MARKERS = (
    ("framer", ("framerusercontent.com", "data-framer-")),
    ("next.js", ("/_next/static/",)),
    ("nuxt", ("/_nuxt/",)),
    ("gatsby", ("/page-data/app-data.json",)),
)
_FRAMEWORK_NAMES = frozenset(name for name, _ in _FRAMEWORK_MARKERS)

# Markers that mean the served document was never meant to be read as-is: an
# empty mount point, or a publisher (Framer, Nuxt, Gatsby) whose output is
# client-rendered by default. Next.js is deliberately absent — it is the common
# case for server-rendered pages too, so on its own it proves nothing.
_STRONG_MARKERS = frozenset({"mount-point", "framer", "nuxt", "gatsby"})

# A shell is short. This is deliberately near "nothing but the title" rather
# than merely "a short page": a stub page with a paragraph of real prose is a
# correct extraction and must not be labelled a rendering failure.
SHELL_TEXT_CHARS = 200
# Below this, the document is too small for its emptiness to mean anything.
MIN_HTML_BYTES = 500

# The other shape a shell takes: a large document that is nearly all script and
# style. Framer ships 140KB of markup that renders to a few hundred characters —
# too much text for the absolute threshold above, and still not a real page.
# Requires a strong marker, so a genuinely script-heavy but readable page (a
# Next.js docs site sits around 5%) is never caught by the ratio alone.
SHELL_TEXT_RATIO = 0.01
RATIO_MIN_HTML_BYTES = 20_000

_MAX_ELEMENTS_SCANNED = 4000


def needs_dom_scan(html: str, extracted_text: str) -> bool:
    """Whether :func:`collect_markers` could possibly change the outcome.

    The DOM scan costs a second full parse, so it is not run on every page. Only
    two shapes can produce a verdict, and both are cheap to rule out first: a
    document that extracted to almost nothing, or one whose markup even mentions
    a loading placeholder. A page with plenty of text and no such word — most
    pages — can be skipped without looking at its DOM.
    """
    html_bytes = len(html)
    if html_bytes < MIN_HTML_BYTES:
        return False
    text_length = len(extracted_text.strip())
    if text_length < max(SHELL_TEXT_CHARS, html_bytes * SHELL_TEXT_RATIO):
        return True
    lowered = html.lower()
    return any(hint in lowered for hint in _LOADING_CLASS_HINTS)


def _is_hidden(element: Tag) -> bool:
    """Cheap visibility check for the two ways a placeholder is usually parked."""
    if element.get("hidden") is not None or element.get("aria-hidden") == "true":
        return True
    style = str(element.get("style") or "").replace(" ", "").lower()
    return "display:none" in style or "visibility:hidden" in style


def _classes(element: Tag) -> str:
    value = element.get("class")
    if isinstance(value, list):
        return " ".join(str(item) for item in value).lower()
    return str(value or "").lower()


# Where a shell's description lives, best source first. `og:description` is
# written for humans reading a link preview; `description` is often the same
# text or a longer variant. Either beats returning only the page title.
_DESCRIPTION_META = (
    ("property", "og:description"),
    ("name", "description"),
    ("name", "twitter:description"),
)
_MAX_METADATA_CHARS = 500
_MAX_PROFILE_LINKS = 6
# JSON-LD types that describe the site's owner rather than a product or article.
_ORGANIZATION_TYPES = frozenset({"organization", "corporation", "localbusiness", "website"})
_MAX_JSONLD_BYTES = 100_000


def _clean_metadata_text(value: str | None) -> str | None:
    """Collapse whitespace and bound length; drop control characters."""
    if not value:
        return None
    text = " ".join(value.split())
    text = "".join(character for character in text if character >= " " and character != "\x7f")
    if not text:
        return None
    if len(text) > _MAX_METADATA_CHARS:
        text = text[: _MAX_METADATA_CHARS - 1].rstrip() + "…"
    return text


def _organization_profiles(soup: BeautifulSoup) -> list[str]:
    """`sameAs` URLs from an Organization JSON-LD block.

    These are the company's own LinkedIn/X/GitHub profiles — on a page that
    rendered to nothing, they are often the only route to the real entity.
    """
    found: list[str] = []
    for tag in soup.find_all("script", type=lambda value: value and "ld+json" in value.lower()):
        body = tag.get_text()
        if not body or len(body) > _MAX_JSONLD_BYTES:
            continue
        try:
            payload = json.loads(body)
        except (ValueError, TypeError):
            continue
        for node in payload if isinstance(payload, list) else [payload]:
            if not isinstance(node, dict):
                continue
            node_type = node.get("@type")
            types = node_type if isinstance(node_type, list) else [node_type]
            if not any(isinstance(t, str) and t.lower() in _ORGANIZATION_TYPES for t in types):
                continue
            same_as = node.get("sameAs")
            for url in same_as if isinstance(same_as, list) else [same_as]:
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    cleaned = _clean_metadata_text(url)
                    if cleaned and cleaned not in found:
                        found.append(cleaned)
                        if len(found) >= _MAX_PROFILE_LINKS:
                            return found
    return found


def extract_shell_metadata(soup: BeautifulSoup) -> dict[str, object]:
    """Salvage the descriptive metadata a non-rendering page still ships."""
    metadata: dict[str, object] = {}

    for attribute, key in _DESCRIPTION_META:
        tag = soup.find("meta", attrs={attribute: key})
        description = _clean_metadata_text(tag.get("content") if tag else None)
        if description:
            metadata["description"] = description
            break

    canonical = soup.find("link", rel=lambda value: value and "canonical" in str(value).lower())
    href = _clean_metadata_text(canonical.get("href") if canonical else None)
    if href:
        metadata["canonical"] = href

    profiles = _organization_profiles(soup)
    if profiles:
        metadata["profiles"] = tuple(profiles)

    return metadata


def collect_shell_evidence(html: str) -> tuple[tuple[str, ...], dict[str, object]]:
    """Markers plus salvaged metadata, from a single parse.

    Runs inside the isolated parser process (see ``html_preflight``); the result
    must stay small and picklable.
    """
    markers: list[str] = []
    lowered = html.lower()
    for name, needles in _FRAMEWORK_MARKERS:
        if any(needle in lowered for needle in needles):
            markers.append(name)

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        return tuple(markers), {}

    # A truncated scan is safe: the markers found are still valid, and the
    # absence of one is not evidence — describe() only ever acts on markers that
    # ARE present, never on their absence.
    body = soup.body or soup
    for element in body.find_all(True, limit=_MAX_ELEMENTS_SCANNED):
        if "mount-point" not in markers:
            element_id = str(element.get("id") or "").lower()
            is_mount = element_id in _MOUNT_POINT_IDS or element.name in _MOUNT_POINT_TAGS
            # find(True) stops at the first child; get_text walks the whole
            # subtree. Cheapest discriminator first, so a document full of
            # non-empty mount-point lookalikes cannot make this quadratic.
            if is_mount and not element.find(True) and not element.get_text(strip=True):
                markers.append("mount-point")
        if "loading-placeholder" not in markers and element.name in _LOADING_PLACEHOLDER_TAGS:
            classes = _classes(element)
            if (
                any(hint in classes for hint in _LOADING_CLASS_HINTS)
                and not _is_hidden(element)
                and not element.get_text(strip=True)
            ):
                markers.append("loading-placeholder")
        if "mount-point" in markers and "loading-placeholder" in markers:
            break

    return tuple(markers), extract_shell_metadata(soup)


def collect_markers(html: str) -> tuple[str, ...]:
    """Markers only. Kept for callers and tests that do not need the metadata."""
    return collect_shell_evidence(html)[0]


def _salvaged_section(metadata: dict[str, object] | None) -> str:
    """Render the metadata a shell shipped, as content rather than a footnote.

    On a page that rendered to nothing this is the entire useful answer: what
    the site says it does, where its canonical home is, and how to reach the
    entity behind it.
    """
    if not metadata:
        return ""
    lines: list[str] = []
    description = metadata.get("description")
    if isinstance(description, str):
        lines.append(f"\n\n{description}")
    canonical = metadata.get("canonical")
    if isinstance(canonical, str):
        lines.append(f"\n\nCanonical URL: {canonical}")
    profiles = metadata.get("profiles")
    if isinstance(profiles, (list, tuple)) and profiles:
        lines.append("\n\nProfiles: " + " · ".join(str(link) for link in profiles))
    if not lines:
        return ""
    return "\n\n**Recovered from page metadata** (the shell still declares these):" + "".join(lines)


def describe(
    markers: tuple[str, ...],
    html_bytes: int,
    extracted_text: str,
    metadata: dict[str, object] | None = None,
) -> str | None:
    """Return a caller-facing warning, or ``None`` if the page looks complete.

    ``markers`` and ``metadata`` come from :func:`collect_shell_evidence`, and
    ``extracted_text`` is the markdown the page rendered down to.
    """
    if not markers:
        return None

    text_length = len(extracted_text.strip())
    framework = next((marker for marker in markers if marker in _FRAMEWORK_NAMES), None)
    has_strong_marker = any(marker in _STRONG_MARKERS for marker in markers)

    barely_any_text = html_bytes >= MIN_HTML_BYTES and text_length < SHELL_TEXT_CHARS
    nearly_all_scripting = (
        has_strong_marker
        and html_bytes >= RATIO_MIN_HTML_BYTES
        and text_length < html_bytes * SHELL_TEXT_RATIO
    )

    if barely_any_text or nearly_all_scripting:
        detail = f" ({framework})" if framework else ""
        return (
            f"[possibly_js_rendered: this page returned {html_bytes} bytes of HTML but only "
            f"{text_length} characters of readable text{detail}. Its content is assembled by "
            f"JavaScript in the browser, which fetchaller does not execute, so what follows is "
            f"the un-hydrated shell rather than the page a visitor sees. Treat 'no content' as "
            f"unknown, not as absent — look for the underlying API or feed the page calls.]"
        ) + _salvaged_section(metadata)

    if "loading-placeholder" in markers and text_length >= SHELL_TEXT_CHARS:
        return (
            "[possibly_js_rendered: part of this page was still a loading placeholder when the "
            "server sent it. The text below is real, but at least one section is filled in by "
            "JavaScript after load and is missing here — a job list, results grid, or similar. "
            "If an expected section looks empty, it is client-rendered, not absent.]"
        )

    return None
