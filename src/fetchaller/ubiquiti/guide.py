"""Ubiquiti installation guides (``ui.com/qig/<slug>``).

A guide is a single-page app whose HTML is a 7KB shell: a logo, a title, a
copyright notice, and a bootstrap script. The pages themselves are separate
JavaScript assets, so reading the URL returns a document whose entire visible
content is the copyright line — a blank page that does not announce itself as
one. That is the failure this module exists to fix.

Each page asset registers one serialized DOM tree in a hyperscript-ish literal::

    !function(t,s,c){...}({STORE_NAME:"QIG_ASSETS"},(function(t){return t}),
      ["A",["div",{"data-page":"A"},[["svg",{version:"1.1",viewBox:"0 0 320 4684"},
        [["style",{type:"text/css"},[[0,".st0{fill:none;...}"]]], ...]]]]])

An element is ``[tag, attributes, children]`` and a text node is ``[0, text]``.
It is *not* JSON — attribute keys are bare identifiers wherever they are valid
ones (``viewBox:`` but ``"xmlns:xlink":``) — so it needs a real parse, which
``_parse_literal`` below provides.

**The guides carry no readable text.** Every page sampled draws its wording as
outlined vector paths: across the guides checked there is not one ``<text>`` or
``<tspan>`` element, and the only text nodes in the whole document are
stylesheets. So there is nothing to extract by trying harder, and a renderer
that quietly returned the page furniture would be claiming to have read a guide
it did not read. What the assets do carry, and what is worth returning, is the
page inventory and the real outbound links — support, help-centre, and app-store
URLs that lead to the same instructions in readable form.

The parse still runs whenever a page declares a text element, so a guide that
ships a text layer is rendered rather than dismissed on the strength of today's
sample. Guides that do not are handled by a cheap link scan instead of parsing
half a megabyte of path data per page for text known not to be there.

Two runtime generations exist. The current one declares its pages outright::

    window.QIG_CONTEXT={...,PAGE_TUPLES:[["index","index-e9pqUsSS.js"],["A","A-e9pqUsSS.js"]],...}

Older guides (``u6-pro``) predate that global, and their page list has to be
recovered from the stylesheet rules that show and hide each page
(``[data-current-page=A] [data-page=A]``) combined with the build hash on the
runtime script (``runtime-33MUgxJ6.js``), since assets are named
``<page>-<hash>.js``.

``dl.ui.com`` answers **200 with a 440KB app shell** for a slug that has no
guide, so a status code proves nothing here; a guide is recognized by its
bootstrap markers, and their absence is reported as "no guide" rather than as an
empty one.
"""

from __future__ import annotations

import html
import re
from urllib.parse import urljoin, urlparse

# Page assets are ~0.5MB each. A guide runs to a handful of pages; this only
# bounds a pathological one.
MAX_PAGES = 16

_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.DOTALL | re.IGNORECASE)
_DESCRIPTION_RE = re.compile(
    r'<meta\s+name=["\']description["\']\s+content=["\'](.*?)["\']', re.DOTALL | re.IGNORECASE
)
_HEADING_RE = re.compile(r'<h1[^>]*class=["\']qig-title["\'][^>]*>(.*?)</h1>', re.DOTALL | re.IGNORECASE)
_PAGE_TUPLES_RE = re.compile(r"PAGE_TUPLES:\s*(\[.*?\])\s*,\s*INDEX_PAGE", re.DOTALL)
_TUPLE_RE = re.compile(r'\[\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\]')
_RUNTIME_RE = re.compile(r'src=["\']runtime-([A-Za-z0-9_-]+)\.js["\']')
_CSS_PAGE_RE = re.compile(r"\[data-current-page=([A-Za-z0-9_-]+)\]")
# The payload is the third argument of the asset bundle's IIFE; `}),` closes the
# identity function just before it.
_PAYLOAD_RE = re.compile(r"\}\),\s*\[")
# `xlink:href` is not a valid bare identifier, so the bundler always quotes it —
# which puts a quote between `href` and the colon. Matching only the bare form
# would make this branch dead on exactly the attribute it was written for.
_HTTP_HREF_RE = re.compile(r'"?(?:xlink:)?href"?\s*:\s*"(https?://[^"]+)"')
# Illustrator stamps a unique id of the form `_00000<digits>_` onto objects when
# it exports, and in some guides it lands on the end of a link's href — the
# u6-pro guide ships `...watch?v=Oja-PE0fFew_00000111178...1150_`, with a
# different suffix on each page for the same video. The suffix is unmistakable
# (it is the same marker as every `SVGID_00000..._` in these files) and the URL
# does not resolve with it attached, so it is removed rather than passed on.
_ILLUSTRATOR_ID_SUFFIX_RE = re.compile(r"_0{4,}\d+_$")


class GuideNotFoundError(LookupError):
    """The slug has no installation guide (dl.ui.com serves its app shell)."""


def _unescape(text: str) -> str:
    """Full HTML entity decoding — guide titles carry numeric entities too."""
    return html.unescape(text).strip()


def discover(html: str, url: str) -> dict:
    """Read a guide's shell into ``{title, description, pages, base}``.

    Raises ``GuideNotFoundError`` when the shell is really dl.ui.com's catch-all app,
    which it serves with HTTP 200 for any slug that does not exist.
    """
    title_match = _HEADING_RE.search(html) or _TITLE_RE.search(html)
    title = _unescape(title_match.group(1)) if title_match else ""
    description_match = _DESCRIPTION_RE.search(html)
    description = _unescape(description_match.group(1)) if description_match else ""

    # Guides live under a directory; a URL without the trailing slash would
    # otherwise resolve assets against the parent.
    base = url if url.endswith("/") else url + "/"

    pages: list[tuple[str, str]] = []
    tuples_match = _PAGE_TUPLES_RE.search(html)
    if tuples_match:
        pages = _TUPLE_RE.findall(tuples_match.group(1))
    else:
        # Older runtime: recover the page list from the show/hide CSS and pair
        # it with the build hash the runtime script is named after.
        runtime = _RUNTIME_RE.search(html)
        if runtime:
            build = runtime.group(1)
            # Keep the stylesheet's own order rather than sorting: alphabetical
            # happens to be right for single letters and wrong the moment a
            # guide uses `AA` or numbers, which would bind the PDF out of order.
            ordered = list(dict.fromkeys(_CSS_PAGE_RE.findall(html)))
            pages = [("index", f"index-{build}.js")] + [(p, f"{p}-{build}.js") for p in ordered]

    if not pages:
        raise GuideNotFoundError(f"no installation guide published at {url}")

    return {
        "title": title,
        "description": description,
        "pages": pages[:MAX_PAGES],
        "dropped_pages": max(0, len(pages) - MAX_PAGES),
        "base": base,
    }


# --------------------------------------------------------------------------
# Asset literal parsing
# --------------------------------------------------------------------------


class _LiteralParser:
    """Recursive-descent reader for the JS object/array literal in an asset.

    Deliberately narrow: it accepts the value grammar the bundler emits —
    strings, numbers, ``true``/``false``/``null``, arrays, and objects whose
    keys may be bare identifiers — and nothing else. It never evaluates.
    """

    def __init__(self, src: str, pos: int = 0) -> None:
        self.src = src
        self.pos = pos

    def _skip(self) -> None:
        while self.pos < len(self.src) and self.src[self.pos] in " \t\r\n":
            self.pos += 1

    def value(self) -> object:
        self._skip()
        if self.pos >= len(self.src):
            raise ValueError("unexpected end of literal")
        char = self.src[self.pos]
        if char == "[":
            return self._array()
        if char == "{":
            return self._object()
        if char in "\"'":
            return self._string()
        return self._word()

    def _array(self) -> list:
        self.pos += 1  # [
        items: list[object] = []
        while True:
            self._skip()
            if self.pos >= len(self.src):
                raise ValueError("unterminated array")
            if self.src[self.pos] == "]":
                self.pos += 1
                return items
            items.append(self.value())
            self._skip()
            if self.pos < len(self.src) and self.src[self.pos] == ",":
                self.pos += 1

    def _object(self) -> dict:
        self.pos += 1  # {
        obj: dict[str, object] = {}
        while True:
            self._skip()
            if self.pos >= len(self.src):
                raise ValueError("unterminated object")
            if self.src[self.pos] == "}":
                self.pos += 1
                return obj
            key = self._string() if self.src[self.pos] in "\"'" else self._bare_key()
            self._skip()
            if self.pos < len(self.src) and self.src[self.pos] == ":":
                self.pos += 1
            obj[key] = self.value()
            self._skip()
            if self.pos < len(self.src) and self.src[self.pos] == ",":
                self.pos += 1

    def _bare_key(self) -> str:
        start = self.pos
        while self.pos < len(self.src) and (self.src[self.pos].isalnum() or self.src[self.pos] in "_$-"):
            self.pos += 1
        if start == self.pos:
            raise ValueError(f"expected a key at offset {self.pos}")
        return self.src[start : self.pos]

    _ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "0": "\0"}

    def _string(self) -> str:
        quote = self.src[self.pos]
        self.pos += 1
        out: list[str] = []
        while self.pos < len(self.src):
            char = self.src[self.pos]
            if char == "\\":
                self.pos += 1
                if self.pos >= len(self.src):
                    break
                escape = self.src[self.pos]
                if escape in "ux":
                    # \uXXXX and \xNN. A lone high surrogate is kept paired with
                    # its follower so the result stays encodable as UTF-8.
                    width = 4 if escape == "u" else 2
                    digits = self.src[self.pos + 1 : self.pos + 1 + width]
                    try:
                        code = int(digits, 16)
                    except ValueError:
                        out.append(escape)
                        self.pos += 1
                        continue
                    self.pos += 1 + width
                    if 0xD800 <= code <= 0xDBFF and self.src[self.pos : self.pos + 2] == "\\u":
                        low = self.src[self.pos + 2 : self.pos + 6]
                        try:
                            low_code = int(low, 16)
                        except ValueError:
                            low_code = 0
                        if 0xDC00 <= low_code <= 0xDFFF:
                            code = 0x10000 + ((code - 0xD800) << 10) + (low_code - 0xDC00)
                            self.pos += 6
                    if 0xD800 <= code <= 0xDFFF:
                        continue  # unpaired surrogate: unencodable, so drop it
                    out.append(chr(code))
                    continue
                out.append(self._ESCAPES.get(escape, escape))
                self.pos += 1
                continue
            if char == quote:
                self.pos += 1
                return "".join(out)
            out.append(char)
            self.pos += 1
        raise ValueError("unterminated string")

    def _word(self) -> object:
        start = self.pos
        while self.pos < len(self.src) and self.src[self.pos] not in ",]}: \t\r\n":
            self.pos += 1
        raw = self.src[start : self.pos]
        if raw == "true":
            return True
        if raw == "false":
            return False
        if raw in {"null", "undefined"}:
            return None
        try:
            return int(raw)
        except ValueError:
            pass
        try:
            return float(raw)
        except ValueError as exc:
            raise ValueError(f"unparsable token {raw!r}") from exc


def parse_asset(js: str) -> object | None:
    """The serialized DOM tree an asset registers, or None if unrecognized."""
    marker = _PAYLOAD_RE.search(js)
    if not marker:
        return None
    try:
        return _LiteralParser(js, marker.end() - 1).value()
    except (ValueError, RecursionError):
        return None


# Text inside these never belongs to the document.
_NON_CONTENT_TAGS = frozenset({"style", "script"})


def _walk(node: object, texts: list[str], links: list[str]) -> None:
    if not isinstance(node, list) or not node:
        return
    tag = node[0]
    # [0, "text"] — a text node.
    if tag == 0:
        if len(node) > 1 and isinstance(node[1], str):
            texts.append(node[1])
        return
    if not isinstance(tag, str):
        return
    attrs = node[1] if len(node) > 1 and isinstance(node[1], dict) else {}
    if tag == "a":
        href = attrs.get("href") or attrs.get("xlink:href")
        if isinstance(href, str) and href.startswith(("http://", "https://")):
            links.append(href)
    if tag in _NON_CONTENT_TAGS:
        return
    for child in node[2] if len(node) > 2 and isinstance(node[2], list) else []:
        _walk(child, texts, links)


def _has_text_layer(js: str) -> bool:
    """Whether an asset declares any SVG text element.

    Guides draw their wording as outlined paths, so this is normally false and
    the half-megabyte of path data never needs parsing. When it is true the
    asset is parsed properly and whatever text it holds is returned.
    """
    return '["text"' in js or '["tspan"' in js


def _clean_links(links: list[str]) -> list[str]:
    """Strip export artefacts and drop repeats, keeping first-seen order."""
    return list(dict.fromkeys(_ILLUSTRATOR_ID_SUFFIX_RE.sub("", link) for link in links))


_UNPARSED = object()


def read_page(js: str, tree: object = _UNPARSED) -> tuple[list[str], list[str]]:
    """``(texts, links)`` for one page asset.

    Pass ``tree`` when the asset has already been parsed — reconstructing the
    page needs the tree anyway, and parsing half a megabyte of path data twice
    for the same asset buys nothing.
    """
    links = _HTTP_HREF_RE.findall(js)
    if tree is _UNPARSED:
        if not _has_text_layer(js):
            return [], _clean_links(links)
        tree = parse_asset(js)

    texts: list[str] = []
    parsed_links: list[str] = []
    # The payload is ["<page id>", <tree>].
    if isinstance(tree, list) and len(tree) > 1:
        _walk(tree[1], texts, parsed_links)
    cleaned = [re.sub(r"[\s\xa0]+", " ", t).strip() for t in texts]
    return [t for t in cleaned if t], _clean_links(parsed_links or links)


def asset_url(base: str, filename: str) -> str:
    """Resolve a page asset against the guide directory, staying on ui.com."""
    resolved = urljoin(base, filename)
    host = (urlparse(resolved).hostname or "").lower()
    if not (host == "ui.com" or host.endswith(".ui.com")):
        raise ValueError(f"asset {filename!r} resolves off ui.com")
    return resolved


# --------------------------------------------------------------------------
# SVG reconstruction
# --------------------------------------------------------------------------

# Only simple single-class selectors, which is all these documents use: the
# artwork is Illustrator output, so every rule is one `.stNN { ... }`.
_CLASS_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_SIMPLE_CLASS_RE = re.compile(r"\.([A-Za-z_][A-Za-z0-9_-]*)$")

_XML_ESCAPES = str.maketrans({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"})

_GRADIENT_TAGS = frozenset({"linearGradient", "radialGradient"})
_STOP_COLOR_RE = re.compile(r"stop-color:\s*(#[0-9A-Fa-f]{3,6})")
_HEX_COLOR_RE = re.compile(r"^#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$")
# Only paint properties may be flattened; `clip-path:url(#…)` and `mask:url(#…)`
# are structural and must keep pointing at their definitions.
_PAINT_URL_RE = re.compile(r"\b(fill|stroke)\s*:\s*url\(#([^)]+)\)")


def _parse_hex(value: str) -> tuple[int, int, int] | None:
    if not _HEX_COLOR_RE.match(value):
        return None
    digits = value[1:]
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    return int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16)


def _gradient_stops(node: list) -> list[tuple[int, int, int]]:
    """The stop colours declared directly on one gradient element."""
    colors: list[tuple[int, int, int]] = []
    for child in node[2] if len(node) > 2 and isinstance(node[2], list) else []:
        if not isinstance(child, list) or not child or child[0] != "stop":
            continue
        attrs = child[1] if len(child) > 1 and isinstance(child[1], dict) else {}
        raw = attrs.get("stop-color")
        if not isinstance(raw, str):
            style = attrs.get("style")
            found = _STOP_COLOR_RE.search(style) if isinstance(style, str) else None
            raw = found.group(1) if found else None
        parsed = _parse_hex(raw.strip()) if isinstance(raw, str) else None
        if parsed:
            colors.append(parsed)
    return colors


def _collect_gradients(node: object, raw: dict[str, list], inherits: dict[str, str]) -> None:
    if not isinstance(node, list) or not node or not isinstance(node[0], str):
        return
    if node[0] in _GRADIENT_TAGS:
        attrs = node[1] if len(node) > 1 and isinstance(node[1], dict) else {}
        identifier = attrs.get("id")
        if isinstance(identifier, str):
            raw[identifier] = _gradient_stops(node)
            # Illustrator routinely defines a gradient once and re-points at it
            # with `xlink:href`, leaving the referring element with no stops.
            parent = attrs.get("xlink:href") or attrs.get("href")
            if isinstance(parent, str) and parent.startswith("#"):
                inherits[identifier] = parent[1:]
    for child in node[2] if len(node) > 2 and isinstance(node[2], list) else []:
        _collect_gradients(child, raw, inherits)


def _collect_ids(node: object, out: set[str]) -> None:
    if not isinstance(node, list) or not node or not isinstance(node[0], str):
        return
    attrs = node[1] if len(node) > 1 and isinstance(node[1], dict) else {}
    identifier = attrs.get("id")
    if isinstance(identifier, str):
        out.add(identifier)
    for child in node[2] if len(node) > 2 and isinstance(node[2], list) else []:
        _collect_ids(child, out)


def _flatten_gradients(svg: object) -> dict[str, str]:
    """Map each gradient id to a flat average colour.

    MuPDF's SVG renderer implements no gradients: every ``fill:url(#id)`` paints
    solid black, exactly as an unresolvable reference does, which turns the
    illustrated device in a guide into a black blob. Averaging the stops is an
    approximation, but a picture in roughly the right colour is a far better
    reproduction than a silhouette.
    """
    raw: dict[str, list] = {}
    inherits: dict[str, str] = {}
    _collect_gradients(svg, raw, inherits)

    flat: dict[str, str] = {}
    for identifier in raw:
        stops = raw[identifier]
        seen = {identifier}
        current = identifier
        # Follow href inheritance to whichever ancestor actually holds stops.
        while not stops and current in inherits:
            current = inherits[current]
            if current in seen or current not in raw:
                break
            seen.add(current)
            stops = raw[current]
        if not stops:
            continue
        count = len(stops)
        red = sum(s[0] for s in stops) // count
        green = sum(s[1] for s in stops) // count
        blue = sum(s[2] for s in stops) // count
        flat[identifier] = f"#{red:02X}{green:02X}{blue:02X}"

    # These guides carry paint references to ids that exist nowhere in the
    # document — 164 of them on one u6-pro page. SVG 1.1 is explicit that an
    # invalid paint reference with no fallback paints as `none`, so honouring
    # that removes a screenful of black shapes a browser never draws either.
    # A reference that *is* defined but unsupported (a pattern) is left alone
    # rather than silently erased.
    defined: set[str] = set()
    _collect_ids(svg, defined)
    for identifier in _referenced_paint_ids(svg):
        if identifier not in defined:
            flat[identifier] = "none"
    return flat


def _referenced_paint_ids(node: object, out: set[str] | None = None) -> set[str]:
    """Every id used as a fill/stroke paint reference anywhere in the tree."""
    found = set() if out is None else out
    if not isinstance(node, list) or not node or not isinstance(node[0], str):
        return found
    attrs = node[1] if len(node) > 1 and isinstance(node[1], dict) else {}
    for key in ("style", "fill", "stroke"):
        value = attrs.get(key)
        if not isinstance(value, str) or "url(#" not in value:
            continue
        if key == "style":
            found.update(match.group(2) for match in _PAINT_URL_RE.finditer(value))
        elif value.startswith("url(#"):
            found.add(value[5:].rstrip(")"))
    if node[0] == "style":
        for child in node[2] if len(node) > 2 and isinstance(node[2], list) else []:
            if isinstance(child, list) and len(child) > 1 and child[0] == 0 and isinstance(child[1], str):
                found.update(match.group(2) for match in _PAINT_URL_RE.finditer(child[1]))
        return found
    for child in node[2] if len(node) > 2 and isinstance(node[2], list) else []:
        _referenced_paint_ids(child, found)
    return found


def _collect_css(node: object, rules: dict[str, str]) -> None:
    """Gather ``.class { decls }`` rules from every <style> in the tree."""
    if not isinstance(node, list) or not node or not isinstance(node[0], str):
        return
    children = node[2] if len(node) > 2 and isinstance(node[2], list) else []
    if node[0] == "style":
        for child in children:
            if isinstance(child, list) and len(child) > 1 and child[0] == 0 and isinstance(child[1], str):
                for selector, body in _CLASS_RULE_RE.findall(child[1]):
                    declarations = body.strip().rstrip(";")
                    if not declarations:
                        continue
                    for part in selector.split(","):
                        simple = _SIMPLE_CLASS_RE.match(part.strip())
                        if simple:
                            name = simple.group(1)
                            rules[name] = f"{rules.get(name, '')}{declarations};"
        return
    for child in children:
        _collect_css(child, rules)


def _apply_flat_paint(value: str, gradients: dict[str, str]) -> str:
    """Swap `fill|stroke:url(#gradient)` for the gradient's flat colour."""

    def _replace(match: re.Match) -> str:
        flat = gradients.get(match.group(2))
        return f"{match.group(1)}:{flat}" if flat else match.group(0)

    return _PAINT_URL_RE.sub(_replace, value)


def _serialize(
    node: object,
    rules: dict[str, str],
    out: list[str],
    gradients: dict[str, str],
    *,
    raw_text: bool = False,
) -> None:
    if not isinstance(node, list) or not node:
        return
    tag = node[0]
    if tag == 0:
        if len(node) > 1 and isinstance(node[1], str):
            # A stylesheet body is character data; escaping it would turn every
            # `>` in a selector into an entity and break the rules.
            if raw_text:
                out.append(_apply_flat_paint(node[1], gradients) if gradients else node[1])
            else:
                out.append(node[1].translate(_XML_ESCAPES))
        return
    if not isinstance(tag, str) or not tag.replace(":", "").replace("-", "").isalnum():
        return

    attrs = dict(node[1]) if len(node) > 1 and isinstance(node[1], dict) else {}
    # MuPDF's SVG reader ignores CSS class selectors, so a document that styles
    # itself entirely through them renders as solid black shapes. Fold each
    # class's declarations into the element's own style, keeping any inline
    # style last so it still wins.
    class_names = attrs.get("class")
    if isinstance(class_names, str):
        declarations = "".join(rules.get(name, "") for name in class_names.split())
        if declarations:
            existing = attrs.get("style")
            attrs["style"] = declarations + (existing if isinstance(existing, str) else "")

    if gradients:
        style = attrs.get("style")
        if isinstance(style, str) and "url(#" in style:
            attrs["style"] = _apply_flat_paint(style, gradients)
        # Also as presentation attributes, e.g. fill="url(#SVGID_1_)".
        for paint in ("fill", "stroke"):
            value = attrs.get(paint)
            if isinstance(value, str) and value.startswith("url(#"):
                flat = gradients.get(value[5:].rstrip(")"))
                if flat:
                    attrs[paint] = flat

    rendered = "".join(
        f' {key}="{str(value).translate(_XML_ESCAPES)}"' for key, value in attrs.items() if value is not None
    )
    children = node[2] if len(node) > 2 and isinstance(node[2], list) else []
    if not children:
        out.append(f"<{tag}{rendered}/>")
        return
    out.append(f"<{tag}{rendered}>")
    for child in children:
        _serialize(child, rules, out, gradients, raw_text=tag in _NON_CONTENT_TAGS)
    out.append(f"</{tag}>")


def _find_svg(node: object, depth: int = 0) -> list | None:
    """The first <svg> at or under ``node``."""
    if not isinstance(node, list) or not node or not isinstance(node[0], str):
        return None
    if node[0] == "svg":
        return node
    if depth >= 4:
        return None
    for child in node[2] if len(node) > 2 and isinstance(node[2], list) else []:
        found = _find_svg(child, depth + 1)
        if found is not None:
            return found
    return None


def page_svg(tree: object) -> str | None:
    """Rebuild one guide page as a standalone SVG document.

    ``tree`` is what ``parse_asset`` returned: ``[page_id, <div wrapper>]``. The
    wrapper's ``<svg>`` child is the page, and it is emitted with class styles
    inlined so renderers that do not implement CSS still draw it correctly.
    """
    if not isinstance(tree, list) or len(tree) < 2:
        return None
    # The current runtime wraps the page in a <div data-page="X">; the older one
    # registers the <svg> directly. Accept either.
    svg = _find_svg(tree[1])
    if svg is None:
        return None

    rules: dict[str, str] = {}
    _collect_css(svg, rules)
    # CSS rules can carry the gradient reference too, so flatten after the
    # class declarations have been folded into each element's style.
    gradients = _flatten_gradients(svg)
    if gradients:
        rules = {name: _apply_flat_paint(body, gradients) for name, body in rules.items()}
    out: list[str] = ['<?xml version="1.0" encoding="UTF-8"?>']
    _serialize(svg, rules, out, gradients)
    return "".join(out)
