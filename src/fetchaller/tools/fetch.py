"""Fetch tool - main URL fetching functionality."""

import asyncio
import json
import os
import re
import struct
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cached_property
from urllib.parse import urljoin, urlparse

import wafer

from ..cache.response_cache import ResponseCache
from ..config import Config, get_wafer_cache_dir
from ..content import js_render
from ..content._isolated import IsolatedProcessingError, run_isolated
from ..content.alibaba import (
    extract_product_id_from_url as extract_alibaba_product_id,
)
from ..content.alibaba import (
    is_alibaba_search_url,
)
from ..content.aliexpress import extract_product_id_from_url, is_aliexpress_search_url
from ..content.amazon import is_amazon_store
from ..content.ashby import (
    BOARD_MAX_RESPONSE_BYTES as ASHBY_BOARD_MAX_RESPONSE_BYTES,
)
from ..content.ashby import (
    AshbyBoardTooLargeError,
    extract_ashby_board_slug,
    fetch_ashby_board,
    is_ashby_board_url,
    is_ashby_embed_url,
    render_ashby_board,
    resolve_ashby_embed_url,
)
from ..content.bamboohr import (
    extract_bamboohr_board_params,
    extract_bamboohr_params,
    fetch_bamboohr_board,
    fetch_bamboohr_job,
    is_bamboohr_board_url,
    is_bamboohr_url,
    render_bamboohr_board,
    render_bamboohr_job,
)
from ..content.cornerstone import (
    fetch_cornerstone_board,
    fetch_cornerstone_job,
    is_cornerstone_board_url,
    is_cornerstone_url,
    render_cornerstone_board,
    render_cornerstone_job,
)
from ..content.costco import is_costco as _is_costco
from ..content.costco import is_costco_category_url as _is_costco_category
from ..content.costco import is_costco_search_url as _is_costco_search
from ..content.craigslist import is_craigslist_search_url as _is_craigslist_search
from ..content.dayforce import (
    fetch_dayforce_board,
    fetch_dayforce_job,
    is_dayforce_board_url,
    is_dayforce_url,
    render_dayforce_board,
    render_dayforce_job,
)
from ..content.digikey import is_digikey as _is_digikey
from ..content.facebook_marketplace import (
    extract_listing_id as _extract_fb_listing_id,
)
from ..content.facebook_marketplace import (
    is_facebook_marketplace_listing as _is_fb_listing,
)
from ..content.facebook_marketplace import (
    is_facebook_marketplace_search as _is_fb_search,
)
from ..content.forums import (
    is_thread_url,
    parse_and_format_feed,
    transform_forum_url,
)
from ..content.gem import (
    extract_gem_board_slug,
    extract_gem_params,
    fetch_gem_board,
    fetch_gem_job,
    is_gem_board_url,
    is_gem_url,
    render_gem_board,
    render_gem_job,
)
from ..content.github import transform_github_url
from ..content.greenhouse import (
    extract_greenhouse_params,
    extract_greenhouse_params_guess,
    fetch_greenhouse_job,
    is_greenhouse_url,
    render_greenhouse_job,
)
from ..content.html import (
    HtmlProcessingError,
    html_to_markdown,
    validate_html_input_size,
)
from ..content.html_preflight import HtmlPreflight, inspect_html_preflight
from ..content.hubspot_careers import (
    extract_hubspot_job_id,
    fetch_hubspot_job,
    is_hubspot_careers_url,
    render_hubspot_job,
)
from ..content.jazzhr import (
    extract_jazzhr_params,
    extract_jazzhr_tenant,
    fetch_jazzhr_board,
    fetch_jazzhr_job,
    is_jazzhr_board_url,
    is_jazzhr_url,
    render_jazzhr_board,
    render_jazzhr_boards,
    render_jazzhr_job,
)
from ..content.lever import (
    extract_lever_params,
    fetch_lever_job,
    is_lever_url,
    render_lever_job,
)
from ..content.mouser import is_mouser as _is_mouser
from ..content.pdf import extract_pdf
from ..content.reddit import (
    canonicalize_reddit_links,
    route_reddit_url,
    transform_reddit_url,
)
from ..content.soylent import is_soylent as _is_soylent
from ..content.ti import extract_ti_part_from_pdf_url, fetch_document_sections, is_ti_document_viewer
from ..content.url import normalize_url
from ..content.workday import (
    fetch_workday_board,
    fetch_workday_job,
    is_workday_board_url,
    is_workday_url,
    render_workday_board,
    render_workday_job,
)
from ..kijiji.api import is_kijiji as _is_kijiji
from ..queue.reddit_queue import RedditRequestQueue, parse_retry_after
from ..realtor.api import is_realtor as _is_realtor
from ..security.ssrf import check_host
from ..security.xss import redact_secrets_for_log
from ..wellfound.api import is_wellfound as _is_wellfound

MAX_RESPONSE_SIZE = 50 * 1024 * 1024  # Config permits PDFs up to 50MB.
MAX_REDIRECTS = 10  # Manual redirect cap (matches wafer's default max_redirects)
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)

# Methods this tool will issue.
#
# POST is here because plenty of search and listing APIs (Getro, Algolia,
# GraphQL gateways, ...) answer to nothing else, so a board that looks static
# reads as empty without it. PUT/PATCH/DELETE are excluded: they have no
# retrieval use at all, and their only effect is to mutate someone else's state.
#
# HEAD and OPTIONS are excluded for a different reason — wafer treats an empty
# 200 as a soft block and retries it, so a bodyless verb would burn up to six
# requests and then raise EmptyResponse. Supporting them would mean opting the
# whole request out of retry, which is a worse trade than not offering them.
ALLOWED_METHODS = frozenset({"GET", "POST"})
# Verbs that may carry a request body.
_BODY_METHODS = frozenset({"POST"})

MAX_REQUEST_BODY_BYTES = 1024 * 1024
MAX_REQUEST_HEADERS = 32
MAX_HEADER_NAME_LEN = 128
MAX_HEADER_VALUE_LEN = 8192

# RFC 9110 token characters — the only thing legal in a header field name.
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")

# Headers the caller may not set: they describe the connection or the framing of
# the body, and letting a caller-supplied value contradict what wafer actually
# puts on the wire is how request smuggling starts. `host` is here because it
# decides which virtual host is addressed, which would sidestep the SSRF pin.
_FORBIDDEN_REQUEST_HEADERS = frozenset(
    {
        "connection",
        "content-length",
        "expect",
        "host",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

# A redirect target is selected by the origin server, not by the caller. On an
# origin change, retain only headers that describe the response representation;
# an arbitrary caller header may carry a credential even when its name is not
# one of the usual Authorization/X-API-Key spellings.
_CROSS_ORIGIN_REDIRECT_SAFE_HEADERS = frozenset(
    {
        "accept",
        "accept-language",
    }
)


_DEFAULT_PORTS = {"http": 80, "https": 443}


# Challenge types that are a final answer, not a state to wait out. Told to try
# again, a caller retries a request that is denied by rule and can never
# succeed — so the advice has to depend on which kind of block this was.
#
# Matched by string rather than by a wafer enum member on purpose: the value is
# part of wafer's public surface, and this stays correct on versions that
# predate the constant instead of needing a floor bump to say something true.
TERMINAL_CHALLENGE_TYPES = frozenset({"cloudflare_block"})


def describe_challenge(challenge_type: str | None) -> str:
    """Caller-facing text for a challenge wafer could not get past."""
    label = challenge_type or "unknown"
    if label in TERMINAL_CHALLENGE_TYPES:
        return (
            f"Blocked by a {label} rule (HTTP 403). This is a firewall rule denying the "
            f"request outright, not a challenge to solve — retrying, waiting, or a different "
            f"identity all return the same answer. Check whether the URL is right (a parked "
            f"or misspelled domain often blocks everything) and try the site's API or a "
            f"different host if one exists."
        )
    return (
        f"Protected by {label} bot detection and could not be bypassed. "
        f"Try again — this sometimes resolves on retry."
    )


def _origin_of(parsed) -> tuple[str, str, int | None]:
    """(scheme, host, effective port) — the unit credentials are scoped to.

    Two URLs sharing a hostname are not the same origin if the scheme or port
    differs, and a header the caller aimed at one must not follow a redirect to
    the other.
    """
    scheme = (parsed.scheme or "").lower()
    try:
        port = parsed.port
    except ValueError:
        port = None
    return (scheme, _canon_host(parsed.hostname or ""), port or _DEFAULT_PORTS.get(scheme))


def validate_request_headers(headers: object) -> tuple[dict[str, str] | None, str | None]:
    """Validate caller-supplied request headers.

    Returns ``(normalized_headers, None)`` or ``(None, error_message)``. Names are
    lowercased so the forbidden-header and credential checks cannot be dodged by
    casing, and both names and values are checked for the control characters that
    would let one header inject another.
    """
    if headers is None:
        return {}, None
    if not isinstance(headers, dict):
        return None, "headers must be an object mapping header names to string values."
    if len(headers) > MAX_REQUEST_HEADERS:
        return None, f"Too many headers (max {MAX_REQUEST_HEADERS})."

    normalized: dict[str, str] = {}
    for name, value in headers.items():
        if not isinstance(name, str) or not isinstance(value, str):
            return None, "Header names and values must both be strings."
        if not name or len(name) > MAX_HEADER_NAME_LEN or not _HEADER_NAME_RE.match(name):
            return None, f"Invalid header name: {name[:64]!r}"
        if len(value) > MAX_HEADER_VALUE_LEN:
            return None, f"Header value too long for {name[:64]!r}."
        # Tab is legal inside a field value; every other control character is not.
        if any(character != "\t" and (ord(character) < 32 or ord(character) == 127) for character in value):
            return None, f"Header value for {name[:64]!r} contains control characters."
        lowered = name.lower()
        if lowered in _FORBIDDEN_REQUEST_HEADERS:
            return None, f"Header {lowered!r} cannot be set; it is controlled by the transport."
        if lowered in normalized:
            return None, f"Duplicate header: {lowered!r}"
        normalized[lowered] = value
    return normalized, None


def validate_request_method(method: object) -> tuple[str | None, str | None]:
    """Normalize and validate the HTTP method. Returns ``(method, error)``."""
    if method is None:
        return "GET", None
    if not isinstance(method, str):
        return None, "method must be a string."
    normalized = method.strip().upper()
    if normalized not in ALLOWED_METHODS:
        allowed = ", ".join(sorted(ALLOWED_METHODS))
        return None, f"Unsupported method {method[:16]!r}. Supported methods: {allowed}."
    return normalized, None


def validate_request_body(method: str, body: object) -> tuple[str | None, str | None]:
    """Validate the request body against the chosen method. Returns ``(body, error)``."""
    if body is None or body == "":
        return None, None
    if not isinstance(body, str):
        return None, "body must be a string."
    if method not in _BODY_METHODS:
        return None, f"A request body is only supported with {'/'.join(sorted(_BODY_METHODS))}."
    if len(body.encode("utf-8", "surrogatepass")) > MAX_REQUEST_BODY_BYTES:
        return None, f"Request body exceeds {MAX_REQUEST_BODY_BYTES // 1024}KB."
    return body, None


def default_content_type(body: str) -> str:
    """Content-Type to send when the caller supplied a body but not the header.

    Nearly every POST-only search endpoint speaks JSON, so a body that parses as
    JSON is labelled as such; anything else falls back to plain text rather than
    guessing at a form encoding the caller did not ask for.
    """
    try:
        json.loads(body)
    except (ValueError, TypeError):
        return "text/plain; charset=utf-8"
    return "application/json"


def _canon_host(host: str) -> str:
    """Canonicalize a hostname the way wafer's resolve/URL layer does — lowercase,
    strip a trailing dot, IDNA-encode — so our validated-host set matches the host
    wafer actually puts on the wire (``resp.url``), not just a lowercased variant.
    Without this, ``Example.COM.`` validates as ``example.com.`` while wafer reports
    ``example.com``, and the final-host check would falsely reject a valid response.

    MUST mirror wafer's private ``_base._canonical_host`` (same stdlib IDNA codec).
    If wafer changes its host canonicalization, update this to match or the pin
    and final-host checks will diverge. Do NOT swap in the stricter ``idna`` PyPI
    lib here — that would diverge from what wafer actually dials.
    """
    h = (host or "").strip().rstrip(".").lower()
    try:
        h = h.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        pass  # already-ASCII or un-encodable: keep the lowercased form
    return h


@dataclass
class FetchResult:
    """Result from fetching a URL."""

    content: bytes
    content_type: str
    status_code: int
    final_url: str
    headers: dict[str, str]

    @cached_property
    def text(self) -> str:
        """Charset-aware decode of the body (Content-Type charset -> <meta> -> utf-8).

        Delegates to wafer's decoder — a strict refinement of the old hand-rolled
        sniffing (validates the codec name, restricts <meta> sniffing to HTML,
        never raises). Lazy + cached, so binary bodies (PDF/images) that only
        read ``.content`` never pay to decode.
        """
        # Guarantee a single canonical content-type so charset resolution never
        # depends on header casing/duplication; content_type is the source of truth.
        headers = {k: v for k, v in self.headers.items() if k.lower() != "content-type"}
        headers["content-type"] = self.content_type
        return wafer.WaferResponse(
            status_code=self.status_code,
            headers=headers,
            url=self.final_url,
            content=self.content,
        ).text


def _log(msg: str) -> None:
    print(
        f"[{datetime.now(UTC).isoformat()}] {redact_secrets_for_log(msg)}",
        file=sys.stderr,
    )


def truncate(text: str, max_tokens: int, chars_per_token: int = 4) -> str:
    """Truncate text without ever exceeding the configured character budget."""
    max_chars = max(0, max_tokens * chars_per_token)
    if len(text) <= max_chars:
        return text
    marker = f"\n\n[Truncated at ~{max_tokens} tokens]"
    if max_chars <= len(marker):
        return text[:max_chars]
    return (text[: max_chars - len(marker)].rstrip() + marker)[:max_chars]


def _compact_json(value: object) -> str:
    """Serialize a JSON value without whitespace under our character budget."""

    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _reject_nonstandard_json_constant(_value: str) -> object:
    """Reject NaN/Infinity so a reported JSON result is standards-compliant."""

    raise ValueError("non-standard JSON constant")


_JSON_TRUNCATION_KEY = "_fetchaller_truncated"
_JSON_BUDGET_ERROR = "JSON exceeds maxTokens; increase maxTokens to return a useful whole-value prefix."


class _JsonBudgetExceededError(ValueError):
    pass


class _JsonProcessingError(ValueError):
    pass


def _encode_json_prefix(
    value: object,
    max_chars: int,
) -> tuple[str, bool, bool]:
    """Encode one structural prefix in one pass.

    Returns ``(encoded, complete, contains_source_value)``. Each scalar/key is
    serialized once; unlike the old candidate-dump loop, a flat array is
    linear rather than quadratic.
    """

    if isinstance(value, dict):
        if max_chars < 2:
            return "", False, False
        pieces = ["{"]
        length = 1
        included = False
        complete = True
        for key, child in value.items():
            key_text = _compact_json(key)
            separator = "" if length == 1 else ","
            fixed_cost = len(separator) + len(key_text) + 1
            child_budget = max_chars - length - fixed_cost - 1
            if child_budget < 0:
                complete = False
                break
            child_text, child_complete, child_included = _encode_json_prefix(
                child,
                child_budget,
            )
            if not child_text or (not child_complete and not child_included):
                complete = False
                break
            pieces.extend((separator, key_text, ":", child_text))
            length += fixed_cost + len(child_text)
            included = True
            if not child_complete:
                complete = False
                break
        pieces.append("}")
        return "".join(pieces), complete, included or not value

    if isinstance(value, list):
        if max_chars < 2:
            return "", False, False
        pieces = ["["]
        length = 1
        included = False
        complete = True
        for child in value:
            separator = "" if length == 1 else ","
            child_budget = max_chars - length - len(separator) - 1
            if child_budget < 0:
                complete = False
                break
            child_text, child_complete, child_included = _encode_json_prefix(
                child,
                child_budget,
            )
            if not child_text or (not child_complete and not child_included):
                complete = False
                break
            pieces.extend((separator, child_text))
            length += len(separator) + len(child_text)
            included = True
            if not child_complete:
                complete = False
                break
        pieces.append("]")
        return "".join(pieces), complete, included or not value

    encoded = _compact_json(value)
    if len(encoded) > max_chars:
        return "", False, False
    return encoded, True, True


def truncate_json(text: str, max_tokens: int, chars_per_token: int = 4) -> str | None:
    """Fit JSON to a budget without ever returning syntactically broken JSON.

    Explicit Reddit ``.json`` is a representation contract, not Markdown. A
    byte/character slice therefore turns an otherwise successful response into
    an unusable document. Parse before returning it, compact where possible,
    then retain only a valid structural prefix if it still exceeds the budget.
    """

    try:
        value = json.loads(text, parse_constant=_reject_nonstandard_json_constant)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    max_chars = max(0, max_tokens * chars_per_token)
    if len(text) <= max_chars:
        return text
    compact, complete, _ = _encode_json_prefix(value, max_chars)
    if complete:
        return compact
    if isinstance(value, dict):
        if _JSON_TRUNCATION_KEY in value:
            # The reserved top-level key already belongs to the source. Never
            # overwrite caller data or misclassify valid JSON as malformed;
            # the representation cannot be safely marked within this budget.
            raise _JsonBudgetExceededError
        marker_cost = len(_compact_json({_JSON_TRUNCATION_KEY: True})) - len("{}") + 1
    elif isinstance(value, list):
        marker_cost = len(_compact_json([{_JSON_TRUNCATION_KEY: True}])) - len("[]") + 1
    else:
        # A top-level scalar cannot be shortened without changing its value.
        raise _JsonBudgetExceededError
    if max_chars < marker_cost + 2:
        raise _JsonBudgetExceededError
    bounded, _, included = _encode_json_prefix(
        value,
        max_chars - marker_cost,
    )
    if not bounded or not included:
        raise _JsonBudgetExceededError
    marker = _compact_json({_JSON_TRUNCATION_KEY: True})[1:-1]
    if isinstance(value, dict):
        inner = bounded[1:-1]
        bounded = "{" + inner + ("," if inner else "") + marker + "}"
    else:
        inner = bounded[1:-1]
        marker_object = "{" + marker + "}"
        bounded = "[" + inner + ("," if inner else "") + marker_object + "]"
    if len(bounded) > max_chars:
        raise _JsonBudgetExceededError
    return bounded


def _truncate_json_worker(
    text: str,
    max_tokens: int,
    chars_per_token: int,
) -> tuple[str, str | None]:
    """Preserve the expected budget outcome across the process boundary."""

    try:
        return "result", truncate_json(text, max_tokens, chars_per_token)
    except _JsonBudgetExceededError:
        return "budget", None


async def _truncate_json_isolated(
    text: str,
    max_tokens: int,
    chars_per_token: int = 4,
    *,
    timeout: float = 20.0,
) -> str | None:
    """Parse and structurally bound untrusted JSON outside the event loop."""

    try:
        kind, result = await run_isolated(
            _truncate_json_worker,
            text,
            max_tokens,
            chars_per_token,
            timeout=timeout,
        )
    except IsolatedProcessingError as exc:
        raise _JsonProcessingError from exc
    if kind == "budget":
        raise _JsonBudgetExceededError
    return result


def _format_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    size = float(n)
    for unit in ("KB", "MB", "GB"):
        size /= 1024
        if size < 1024:
            return f"{size:.1f} {unit}"
    return f"{size / 1024:.1f} TB"


def _image_dimensions(data: bytes, content_type: str) -> tuple[int, int] | None:
    """Parse pixel dimensions from the file header. PNG, GIF, JPEG, WebP."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
            w, h = struct.unpack(">II", data[16:24])
            return w, h
        if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
            w, h = struct.unpack("<HH", data[6:10])
            return w, h
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
            fourcc = data[12:16]
            if fourcc == b"VP8 ":  # lossy
                w, h = struct.unpack("<HH", data[26:30])
                return w & 0x3FFF, h & 0x3FFF
            if fourcc == b"VP8L" and len(data) >= 25:  # lossless
                b0, b1, b2, b3 = data[21], data[22], data[23], data[24]
                w = ((b1 & 0x3F) << 8 | b0) + 1
                h = ((b3 & 0x0F) << 10 | b2 << 2 | (b1 & 0xC0) >> 6) + 1
                return w, h
            if fourcc == b"VP8X" and len(data) >= 30:  # extended
                w = (data[24] | data[25] << 8 | data[26] << 16) + 1
                h = (data[27] | data[28] << 8 | data[29] << 16) + 1
                return w, h
        if data[:3] == b"\xff\xd8\xff":  # JPEG: scan for SOFn marker
            i = 2
            while i < len(data) - 8:
                if data[i] != 0xFF:
                    break
                marker = data[i + 1]
                if marker in (0xD8, 0xD9):
                    break
                sof = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
                if marker in sof:
                    h, w = struct.unpack(">HH", data[i + 5 : i + 9])
                    return w, h
                seg_len = struct.unpack(">H", data[i + 2 : i + 4])[0]
                i += 2 + seg_len
    except (struct.error, IndexError):
        pass
    return None


_FILENAME_RE = re.compile(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', re.IGNORECASE)


def _format_image_summary(result: "FetchResult", url: str) -> str:
    """Build a short text summary for binary images — they can't be rendered as text."""
    lines = [f"[Image: {result.content_type or 'unknown'}]"]

    filename = None
    disp = result.headers.get("content-disposition", "")
    m = _FILENAME_RE.search(disp) if disp else None
    if m:
        filename = m.group(1).strip()
    else:
        path = urlparse(result.final_url or url).path
        tail = path.rsplit("/", 1)[-1] if path else ""
        if tail:
            filename = tail
    if filename:
        lines.append(f"Filename: {filename}")

    cl = result.headers.get("content-length", "")
    size = int(cl) if cl.isdigit() else len(result.content)
    lines.append(f"Size: {_format_bytes(size)}")

    dims = _image_dimensions(result.content, result.content_type)
    if dims:
        lines.append(f"Dimensions: {dims[0]}x{dims[1]}")

    lm = result.headers.get("last-modified")
    if lm:
        lines.append(f"Modified: {lm}")

    etag = result.headers.get("etag")
    if etag:
        lines.append(f"ETag: {etag}")

    lines.append(f"URL: {result.final_url or url}")
    return "\n".join(lines)


async def _fetch_url_impl(
    url: str,
    max_tokens: int = 25000,
    timeout: int = 10,
    raw: bool = False,
    cache: ResponseCache | None = None,
    config: Config | None = None,
    browser_solver=None,
    reddit_queue: RedditRequestQueue | None = None,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: str | None = None,
    _skip_aliexpress_intercept: bool = False,
    _skip_alibaba_intercept: bool = False,
    _skip_craigslist_intercept: bool = False,
) -> dict:
    """
    Fetch a URL and return its content.

    Args:
        url: URL to fetch
        max_tokens: Maximum tokens to return (default: 25000)
        timeout: Request timeout in seconds (default: 10)
        raw: Return raw HTML instead of markdown (default: False)
        cache: Optional ResponseCache instance
        config: Optional Config instance
        browser_solver: Optional BrowserSolver for browser-based challenges
        reddit_queue: Optional shared RedditRequestQueue for mapped Reddit JSON
        method: HTTP method (see ALLOWED_METHODS; default GET)
        headers: Extra request headers to send
        body: Request body, POST only

    Returns:
        Dict with:
        - content: The fetched content
        - content_type: Type of content (markdown, json, pdf, etc.)
        - url: Final URL (after redirects)
        - error: Error message if failed
    """
    start = time.monotonic()

    method, method_error = validate_request_method(method)
    if method_error:
        return {"error": method_error}
    request_headers, header_error = validate_request_headers(headers)
    if header_error:
        return {"error": header_error}
    body, body_error = validate_request_body(method, body)
    if body_error:
        return {"error": body_error}
    if body is not None and "content-type" not in request_headers:
        request_headers["content-type"] = default_content_type(body)

    # Gate for the site interceptors and the response cache below.
    #
    # `raw` already disables both. Two more conditions have to disable them:
    #
    # - A non-GET request, because every interceptor answers a URL pattern by
    #   issuing its OWN request against a structured API, which would silently
    #   discard the caller's method, headers and body and return a GET's answer
    #   to a POST. The cache is keyed by URL alone, so a POST would also serve
    #   and poison the GET entry for the same address.
    # - ANY caller-supplied header, because the response cache is keyed by URL
    #   alone. A GET carrying `Authorization` would otherwise store its
    #   authenticated body under the plain URL and hand it to the next caller,
    #   and would read back a cached anonymous body as if it were its own. An
    #   interceptor would equally drop the header and answer a different
    #   request than the one asked for.
    #
    # In short: shared-cache and structured handling are only valid for a
    # plain, unmodified GET.
    structured = not raw and method == "GET" and not request_headers

    # Validate URL once before any cache/interceptor work. Accessing `.port`
    # is intentional: urllib defers malformed/out-of-range port errors until
    # that property is read, which previously let them escape from cache-key
    # normalization as an unexpected MCP exception.
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return {"error": f"Invalid protocol: {parsed.scheme}. Only http/https supported."}
        if not parsed.hostname:
            return {"error": "Invalid URL format. Expected http:// or https:// URL."}
        _ = parsed.port
        if parsed.username is not None or parsed.password is not None:
            return {"error": "Invalid URL: embedded credentials are not supported."}
        if len(url) > 8192 or any(ord(char) < 32 or ord(char) == 127 for char in url):
            return {"error": "Invalid URL format. Control characters or excessive length."}
    except (TypeError, ValueError):
        return {"error": "Invalid URL format. Expected http:// or https:// URL."}

    # SSRF protection: early reject on the input host. The actual fetch host is
    # re-validated and pinned below (after URL transforms), which is what closes
    # the DNS-rebinding window; this is just a fast pre-check on the raw input.
    hostname = parsed.hostname or ""
    verdict = await check_host(hostname)
    if verdict.blocked:
        return {"error": verdict.message}

    # Amazon store pages are JS-rendered SPAs — return helpful message
    if is_amazon_store(url) and structured:
        return {
            "error": "Amazon store/brand pages are JavaScript-rendered and not supported. "
            "Use the search tool to find products by brand instead: "
            "search('Brand Name products site:amazon.ca'). "
            "Or fetch individual product pages directly (e.g. amazon.ca/dp/ASIN)."
        }

    # Structured-intercept caching helpers. The site interceptors below return
    # pre-rendered text (product/search/listing pages fetched via API). They
    # historically wrote the cache but never read it back — and stored the
    # *truncated* string, so a later fetch with a larger maxTokens would be
    # capped by an earlier truncation. These helpers make the cache functional:
    # read-before-fetch, and store the FULL content.
    #
    # The key is NAMESPACED (not plain normalize_url(url)): the generic HTML path
    # caches under normalize_url(fetch_url_str), and a marketplace URL whose
    # structured fetch failed once falls through and caches generic-HTML markdown
    # under that same key. Without the namespace, the interceptor would then read
    # that generic fallback back as if it were a structured result and keep
    # serving it (suppressing a retry of the real API) until the TTL expires.
    def _intercept_cache_key(u: str) -> str:
        return "\x00intercept\x00" + normalize_url(u)

    def _intercept_cache_get(u: str) -> dict | None:
        if not (cache and structured):
            return None
        hit = cache.get(_intercept_cache_key(u))
        if not hit:
            return None
        _log(f"FETCH {u} -> CACHED intercept ({time.monotonic() - start:.1f}s)")
        return {
            "content": truncate(hit.content, max_tokens),
            "content_type": hit.content_type,
            "url": u,
            "cached": True,
        }

    def _intercept_cache_set(u: str, full_content: str, content_type: str = "text") -> None:
        if cache:
            cache.set(_intercept_cache_key(u), full_content, content_type)

    # AliExpress product pages — use product tool which tries MTop API first
    ae_product_id = extract_product_id_from_url(url) if structured and not _skip_aliexpress_intercept else None
    if ae_product_id:
        _hit = _intercept_cache_get(url)
        if _hit:
            return _hit
        from ..aliexpress.product import get_product

        result = await get_product(
            ae_product_id,
            cache=cache,
            config=config,
            browser_solver=browser_solver,
            timeout=min(timeout, 180),
        )
        if "content" in result:
            _intercept_cache_set(url, result["content"])
            content = truncate(result["content"], max_tokens)
            _log(f"FETCH {url} -> AliExpress product ({len(content)} chars, {time.monotonic() - start:.1f}s)")
            return {"content": content, "content_type": "text", "url": url}
        return result  # Error dict

    # Alibaba.com product pages — SSR with embedded JSON
    alibaba_product_id = extract_alibaba_product_id(url) if structured and not _skip_alibaba_intercept else None
    if alibaba_product_id:
        _hit = _intercept_cache_get(url)
        if _hit:
            return _hit
        from ..alibaba.product import get_product as get_alibaba_product

        result = await get_alibaba_product(
            alibaba_product_id,
            timeout=timeout,
            cache=cache,
            config=config,
            browser_solver=browser_solver,
        )
        if "content" in result:
            _intercept_cache_set(url, result["content"])
            content = truncate(result["content"], max_tokens)
            _log(f"FETCH {url} -> Alibaba product ({len(content)} chars, {time.monotonic() - start:.1f}s)")
            return {"content": content, "content_type": "text", "url": url}
        return result  # Error dict

    # Alibaba.com search pages
    if structured and not _skip_alibaba_intercept and is_alibaba_search_url(url):
        _hit = _intercept_cache_get(url)
        if _hit:
            return _hit
        from urllib.parse import parse_qs

        from ..alibaba.search import search_alibaba

        qs = parse_qs(urlparse(url).query)
        query = qs.get("SearchText", [""])[0]
        try:
            page_num = int(qs.get("page", ["1"])[0])
        except (ValueError, IndexError):
            page_num = 1
        sort = {
            "PRICE_ASC": "price_asc",
            "PRICE_DESC": "price_desc",
        }.get(qs.get("sortType", [""])[0], "default")
        try:
            min_price = float(qs["minPrice"][0]) if "minPrice" in qs else None
        except (ValueError, IndexError):
            min_price = None
        try:
            max_price = float(qs["maxPrice"][0]) if "maxPrice" in qs else None
        except (ValueError, IndexError):
            max_price = None

        result = await search_alibaba(
            query=query or "alibaba",
            page=page_num,
            sort=sort,
            min_price=min_price,
            max_price=max_price,
            timeout=timeout,
            cache=cache,
            config=config,
            browser_solver=browser_solver,
        )
        if "content" in result:
            _intercept_cache_set(url, result["content"])
            content = truncate(result["content"], max_tokens)
            _log(f"FETCH {url} -> Alibaba search ({len(content)} chars, {time.monotonic() - start:.1f}s)")
            return {"content": content, "content_type": "text", "url": url}
        return result  # Error dict

    # AliExpress search pages
    if structured and not _skip_aliexpress_intercept and is_aliexpress_search_url(url):
        _hit = _intercept_cache_get(url)
        if _hit:
            return _hit
        from urllib.parse import parse_qs

        from ..aliexpress.search import search_aliexpress

        qs = parse_qs(urlparse(url).query)
        try:
            page_num = int(qs.get("page", ["1"])[0])
        except (ValueError, IndexError):
            page_num = 1
        sort = {
            "total_tranpro_desc": "orders",
            "price_asc": "price_asc",
            "price_desc": "price_desc",
        }.get(qs.get("sortType", ["default"])[0], "default")
        try:
            min_price = float(qs["minPrice"][0]) if "minPrice" in qs else None
        except (ValueError, IndexError):
            min_price = None
        try:
            max_price = float(qs["maxPrice"][0]) if "maxPrice" in qs else None
        except (ValueError, IndexError):
            max_price = None

        from urllib.parse import unquote

        path = urlparse(url).path
        query = "aliexpress"
        import re

        m = re.search(r"/w/wholesale-(.+?)\.html", path)
        if m:
            query = unquote(m.group(1).replace("-", " "))

        result = await search_aliexpress(
            query=query,
            page=page_num,
            sort=sort,
            min_price=min_price,
            max_price=max_price,
            timeout=timeout,
            cache=cache,
            config=config,
            browser_solver=browser_solver,
        )
        if "content" in result:
            _intercept_cache_set(url, result["content"])
            content = truncate(result["content"], max_tokens)
            _log(f"FETCH {url} -> AliExpress search ({len(content)} chars, {time.monotonic() - start:.1f}s)")
            return {"content": content, "content_type": "text", "url": url}
        return result  # Error dict

    # Mouser product/search pages — use API when key is configured
    if _is_mouser(url) and structured:
        mouser_key = os.environ.get("MOUSER_API_KEY")
        if mouser_key:
            _hit = _intercept_cache_get(url)
            if _hit:
                return _hit
            from ..mouser.api import get_product as get_mouser_product

            result = await get_mouser_product(url, api_key=mouser_key)
            if "content" in result:
                _intercept_cache_set(url, result["content"])
                content = truncate(result["content"], max_tokens)
                _log(f"FETCH {url} -> Mouser API ({len(content)} chars, {time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": "text", "url": url}
            if "Could not extract" not in result.get("error", ""):
                return result
            _log(f"FETCH {url} -> Mouser API couldn't parse URL, falling through to HTML")
        else:
            _log(f"FETCH {url} -> Mouser API key absent, falling through to HTML")

    # DigiKey product/search pages — use API when credentials are configured
    if _is_digikey(url) and structured:
        dk_client_id = os.environ.get("DIGIKEY_CLIENT_ID")
        dk_client_secret = os.environ.get("DIGIKEY_CLIENT_SECRET")
        if dk_client_id and dk_client_secret:
            _hit = _intercept_cache_get(url)
            if _hit:
                return _hit
            from ..digikey.api import get_product as get_digikey_product

            result = await get_digikey_product(url, client_id=dk_client_id, client_secret=dk_client_secret)
            if "content" in result:
                _intercept_cache_set(url, result["content"])
                content = truncate(result["content"], max_tokens)
                _log(f"FETCH {url} -> DigiKey API ({len(content)} chars, {time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": "text", "url": url}
            if "Could not extract" not in result.get("error", ""):
                return result
            _log(f"FETCH {url} -> DigiKey API couldn't parse URL, falling through to HTML")
        else:
            _log(f"FETCH {url} -> DigiKey credentials absent, falling through to HTML")

    # Kijiji search/listing pages — use GraphQL API
    if _is_kijiji(url) and structured:
        from ..kijiji.api import get_listing as get_kijiji_listing
        from ..kijiji.api import is_kijiji_listing, is_kijiji_search

        if is_kijiji_search(url) or is_kijiji_listing(url):
            _hit = _intercept_cache_get(url)
            if _hit:
                return _hit
            result = await get_kijiji_listing(url)
            if "content" in result:
                _intercept_cache_set(url, result["content"])
                content = truncate(result["content"], max_tokens)
                _log(f"FETCH {url} -> Kijiji GraphQL ({len(content)} chars, {time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": "text", "url": url}
            if "Could not extract" not in result.get("error", ""):
                return result
            _log(f"FETCH {url} -> Kijiji GraphQL couldn't parse URL, falling through to HTML")

    # realtor.ca — individual listing pages (SSR) + search/SEO/map pages (api2).
    # api2 is Imperva-protected; wafer 0.2.4 handles it transparently. For a raw
    # listing page we fall through so the generic path returns the SSR HTML;
    # search pages are CSR shells, so always use the structured renderer.
    if _is_realtor(url):
        from ..realtor.api import is_realtor_listing, is_realtor_search
        from ..realtor.search import get_realtor

        if structured and (is_realtor_listing(url) or is_realtor_search(url)):
            _hit = _intercept_cache_get(url)
            if _hit:
                return _hit
            result = await get_realtor(url, browser_solver=browser_solver)
            if "content" in result:
                _intercept_cache_set(url, result["content"])
                content = truncate(result["content"], max_tokens)
                _log(f"FETCH {url} -> realtor.ca ({len(content)} chars, {time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": "text", "url": url}
            return result

    # wellfound.com — startup job search, job detail, company pages. All SSR;
    # wafer 0.2.4 passes DataDome. raw=True falls through for the raw HTML.
    if _is_wellfound(url) and structured:
        from ..wellfound.api import is_wellfound_company, is_wellfound_job, is_wellfound_search
        from ..wellfound.page import get_wellfound

        if is_wellfound_job(url) or is_wellfound_company(url) or is_wellfound_search(url):
            _hit = _intercept_cache_get(url)
            if _hit:
                return _hit
            result = await get_wellfound(url, browser_solver=browser_solver, timeout=float(timeout))
            if "content" in result:
                _intercept_cache_set(url, result["content"])
                content = truncate(result["content"], max_tokens)
                _log(f"FETCH {url} -> wellfound.com ({len(content)} chars, {time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": "text", "url": url}
            return result

    # LinkedIn job permalinks (linkedin.com/jobs/view/{id}): the logged-out page
    # is a sign-in wall, but the guest detail fragment carries the full public
    # posting. Map the URL to that instead of returning the wall.
    if structured:
        from ..linkedin.url import extract_linkedin_job_id

        _li_job_id = extract_linkedin_job_id(url)
        if _li_job_id:
            _hit = _intercept_cache_get(url)
            if _hit:
                return _hit
            from ..linkedin.search import get_linkedin_job

            result = await get_linkedin_job(
                _li_job_id,
                max_tokens=max_tokens,
                timeout=float(timeout),
                browser_solver=browser_solver,
            )
            if "content" in result:
                _intercept_cache_set(url, result["content"])
                content = truncate(result["content"], max_tokens)
                _log(f"FETCH {url} -> LinkedIn job ({len(content)} chars, {time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": "markdown", "url": url}
            return result

    # Costco search/category pages — CSR, use search API when available
    _costco_redirect_fallback = False
    _costco_fallback_url: str | None = None
    if structured and (_is_costco_search(url) or _is_costco_category(url)):
        try:
            _hit = _intercept_cache_get(url)
            if _hit:
                return _hit
            from ..costco.search import search_costco

            result = await search_costco(url, cache=cache, config=config, browser_solver=browser_solver)
            if "content" in result:
                content = result["content"]
                # If returned 0 results, try category page fallback.
                # Costco's JS frontend redirects some searches (e.g. "macbook",
                # "samsung") to brand/category pages. We can't follow JS redirects
                # so we construct the likely category URL: /{query}.html
                if "No products found" in content:
                    from ..costco.search import extract_search_params

                    sp = extract_search_params(url)
                    if sp:
                        q = sp["query"].strip().lower().replace(" ", "-")
                        d = sp["domain"]
                        _costco_fallback_url = f"https://www.costco.{d}/{q}.html"
                        _costco_redirect_fallback = True
                        _log(
                            f"FETCH {url} -> Costco search returned 0 results, trying category fallback: {_costco_fallback_url}"
                        )
                    else:
                        _log(f"FETCH {url} -> Costco search returned 0 results, no fallback available")
                else:
                    _intercept_cache_set(url, content)
                    content = truncate(content, max_tokens)
                    _log(f"FETCH {url} -> Costco search ({len(content)} chars, {time.monotonic() - start:.1f}s)")
                    return {"content": content, "content_type": "text", "url": url}
            if not _costco_redirect_fallback:
                if "Could not extract" not in result.get("error", ""):
                    return result
                _log(f"FETCH {url} -> Costco search failed, falling through to HTML")
        except ImportError:
            _log(f"FETCH {url} -> Costco search module not yet implemented, falling through to HTML")

    # Craigslist search pages — CSR, use SAPI
    if structured and not _skip_craigslist_intercept and _is_craigslist_search(url):
        _hit = _intercept_cache_get(url)
        if _hit:
            return _hit
        from ..craigslist.search import search_craigslist

        result = await search_craigslist(
            url,
            cache=cache,
            config=config,
            browser_solver=browser_solver,
        )
        if "content" in result:
            _intercept_cache_set(url, result["content"])
            content = truncate(result["content"], max_tokens)
            _log(f"FETCH {url} -> Craigslist search ({len(content)} chars, {time.monotonic() - start:.1f}s)")
            return {"content": content, "content_type": "text", "url": url}
        if "Could not extract" not in result.get("error", ""):
            return result
        _log(f"FETCH {url} -> Craigslist search failed, falling through to HTML")

    # Facebook Marketplace — 100% CSR, use GraphQL API
    if _is_fb_search(url) and structured:
        _hit = _intercept_cache_get(url)
        if _hit:
            return _hit
        from ..facebook_marketplace.search import search_marketplace

        result = await search_marketplace(url)
        if "content" in result:
            _intercept_cache_set(url, result["content"])
            content = truncate(result["content"], max_tokens)
            _log(f"FETCH {url} -> FB Marketplace search ({len(content)} chars, {time.monotonic() - start:.1f}s)")
            return {"content": content, "content_type": "text", "url": url}
        return result

    if _is_fb_listing(url) and structured:
        listing_id = _extract_fb_listing_id(url)
        if listing_id:
            _hit = _intercept_cache_get(url)
            if _hit:
                return _hit
            from ..facebook_marketplace.listing import get_listing as get_fb_listing

            result = await get_fb_listing(listing_id)
            if "content" in result:
                _intercept_cache_set(url, result["content"])
                content = truncate(result["content"], max_tokens)
                _log(f"FETCH {url} -> FB Marketplace listing ({len(content)} chars, {time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": "text", "url": url}
            return result

    # HubSpot careers (www.hubspot.com/careers/jobs/{id}): SPA backed by a
    # GraphQL endpoint. URLs carry a vestigial ``?gh_jid=`` that's the HubSpot
    # job id, NOT a Greenhouse one — the ``hubspot`` Greenhouse board is empty,
    # so this MUST run before the greenhouse guess to skip a wasted 404.
    if is_hubspot_careers_url(url) and structured:
        _hs_job_id = extract_hubspot_job_id(url)
        if _hs_job_id:
            _hs_cache_key = _intercept_cache_key(url) if cache else None
            if cache and structured and _hs_cache_key:
                _hs_cached = cache.get(_hs_cache_key)
                if _hs_cached:
                    content = truncate(_hs_cached.content, max_tokens)
                    _log(f"FETCH {url} -> HubSpot careers CACHED ({time.monotonic() - start:.1f}s)")
                    return {"content": content, "content_type": _hs_cached.content_type, "url": url, "cached": True}
            _hs_session = wafer.AsyncSession(
                browser_solver=browser_solver,
                timeout=timedelta(seconds=timeout),
                cache_dir=get_wafer_cache_dir(),
                max_response_size=10 * 1024 * 1024,
            )
            _hs_data = await fetch_hubspot_job(_hs_job_id, _hs_session)
            if _hs_data:
                markdown = render_hubspot_job(_hs_data, source_url=url)
                if cache and _hs_cache_key:
                    cache.set(_hs_cache_key, markdown, "markdown")
                content = truncate(markdown, max_tokens)
                _log(f"FETCH {url} -> HubSpot careers GraphQL ({len(content)} chars, {time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": "markdown", "url": url}
            # API failed — fall through to normal HTML fetch.

    # Greenhouse direct URL (boards.greenhouse.io / job-boards.greenhouse.io
    # / embed iframe URL / any URL carrying ?gh_jid=&gh_src=): fetch the
    # public JSON API directly and render. Skips the SPA body entirely.
    # Also handles company-site URLs like ``dropbox.jobs/en/jobs/{id}?gh_src=X``
    # via a hostname-derived board-token guess that's probed against the API.
    gh_params = extract_greenhouse_params(url) if structured else None
    gh_guess = None
    if structured and not gh_params:
        gh_guess = extract_greenhouse_params_guess(url)
    if gh_params and is_greenhouse_url(url):
        _gh_cache_key = _intercept_cache_key(url) if cache else None
        if cache and structured and _gh_cache_key:
            _gh_cached = cache.get(_gh_cache_key)
            if _gh_cached:
                content = truncate(_gh_cached.content, max_tokens)
                _log(f"FETCH {url} -> Greenhouse CACHED ({time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": _gh_cached.content_type, "url": url, "cached": True}
        _gh_session = wafer.AsyncSession(
            browser_solver=browser_solver,
            timeout=timedelta(seconds=timeout),
            cache_dir=get_wafer_cache_dir(),
            max_response_size=10 * 1024 * 1024,
        )
        _gh_token, _gh_jid = gh_params
        _gh_data = await fetch_greenhouse_job(_gh_token, _gh_jid, _gh_session)
        if _gh_data:
            markdown = render_greenhouse_job(_gh_data, source_url=url)
            if cache and _gh_cache_key:
                cache.set(_gh_cache_key, markdown, "markdown")
            content = truncate(markdown, max_tokens)
            _log(f"FETCH {url} -> Greenhouse API ({len(content)} chars, {time.monotonic() - start:.1f}s)")
            return {"content": content, "content_type": "markdown", "url": url}
        # API failed — fall through to normal HTML fetch.

    # Greenhouse hostname-derived guess (e.g. dropbox.jobs/en/jobs/{id}?gh_src=X).
    # The board token isn't in the page HTML, so we derive it from the hostname
    # and probe the API. A 404 falls through to normal HTML fetch.
    if gh_guess:
        _gh_cache_key = _intercept_cache_key(url) if cache else None
        if cache and structured and _gh_cache_key:
            _gh_cached = cache.get(_gh_cache_key)
            if _gh_cached:
                content = truncate(_gh_cached.content, max_tokens)
                _log(f"FETCH {url} -> Greenhouse CACHED ({time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": _gh_cached.content_type, "url": url, "cached": True}
        _gh_session = wafer.AsyncSession(
            browser_solver=browser_solver,
            timeout=timedelta(seconds=timeout),
            cache_dir=get_wafer_cache_dir(),
            max_response_size=10 * 1024 * 1024,
        )
        _gh_token, _gh_jid = gh_guess
        _gh_data = await fetch_greenhouse_job(_gh_token, _gh_jid, _gh_session)
        if _gh_data:
            markdown = render_greenhouse_job(_gh_data, source_url=url)
            if cache and _gh_cache_key:
                cache.set(_gh_cache_key, markdown, "markdown")
            content = truncate(markdown, max_tokens)
            _log(
                f"FETCH {url} -> Greenhouse API (hostname guess: {_gh_token}, {len(content)} chars, {time.monotonic() - start:.1f}s)"
            )
            return {"content": content, "content_type": "markdown", "url": url}
        # Guess wrong or API unavailable — fall through to normal HTML fetch.

    # Ashby board index (jobs.ashbyhq.com/{org}): fetch listings via public REST.
    # Must run before the generic HTML fetch — without this, board pages return
    # just the SPA spinner and our postprocessor strips it to the page title.
    if is_ashby_board_url(url) and structured:
        _ab_cache_key = _intercept_cache_key(url) if cache else None
        if cache and structured and _ab_cache_key:
            _ab_cached = cache.get(_ab_cache_key)
            if _ab_cached:
                content = truncate(_ab_cached.content, max_tokens)
                _log(f"FETCH {url} -> Ashby board CACHED ({time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": _ab_cached.content_type, "url": url, "cached": True}
        _ab_session = wafer.AsyncSession(
            browser_solver=browser_solver,
            timeout=timedelta(seconds=timeout),
            cache_dir=get_wafer_cache_dir(),
            max_response_size=ASHBY_BOARD_MAX_RESPONSE_BYTES,
        )
        _ab_org = extract_ashby_board_slug(url)
        try:
            _ab_data = await fetch_ashby_board(_ab_org, _ab_session)
        except AshbyBoardTooLargeError:
            # The board exists; falling through would render the SPA title as
            # an empty job board instead of reporting the real limit.
            return {
                "error": (
                    f"Ashby job board '{_ab_org}' is larger than the "
                    f"{ASHBY_BOARD_MAX_RESPONSE_BYTES // (1024 * 1024)}MB "
                    "read limit and was not truncated into a partial board."
                )
            }
        if _ab_data is not None:
            markdown = render_ashby_board(_ab_data, _ab_org, source_url=url)
            if cache and _ab_cache_key:
                cache.set(_ab_cache_key, markdown, "markdown")
            content = truncate(markdown, max_tokens)
            _log(
                f"FETCH {url} -> Ashby board ({len(_ab_data.get('jobs') or [])} jobs, {len(content)} chars, {time.monotonic() - start:.1f}s)"
            )
            return {"content": content, "content_type": "markdown", "url": url}
        # API 404 (not an Ashby-hosted board) or transient failure — fall through.

    # Gem direct URL (jobs.gem.com/{board}/{extId}): fetch via public GraphQL.
    if is_gem_url(url) and structured:
        _gm_cache_key = _intercept_cache_key(url) if cache else None
        if cache and structured and _gm_cache_key:
            _gm_cached = cache.get(_gm_cache_key)
            if _gm_cached:
                content = truncate(_gm_cached.content, max_tokens)
                _log(f"FETCH {url} -> Gem CACHED ({time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": _gm_cached.content_type, "url": url, "cached": True}
        _gm_session = wafer.AsyncSession(
            browser_solver=browser_solver,
            timeout=timedelta(seconds=timeout),
            cache_dir=get_wafer_cache_dir(),
            max_response_size=10 * 1024 * 1024,
        )
        _gm_board, _gm_ext = extract_gem_params(url)
        _gm_data = await fetch_gem_job(_gm_board, _gm_ext, _gm_session)
        if _gm_data and _gm_data.get("oatsExternalJobPosting"):
            markdown = render_gem_job(_gm_data, source_url=url)
            if cache and _gm_cache_key:
                cache.set(_gm_cache_key, markdown, "markdown")
            content = truncate(markdown, max_tokens)
            _log(f"FETCH {url} -> Gem GraphQL ({len(content)} chars, {time.monotonic() - start:.1f}s)")
            return {"content": content, "content_type": "markdown", "url": url}
        # Gem API failed — fall through to normal HTML fetch.

    # Gem board index (jobs.gem.com/{board}): fetch listings via public REST.
    if is_gem_board_url(url) and structured:
        _gmb_cache_key = _intercept_cache_key(url) if cache else None
        if cache and structured and _gmb_cache_key:
            _gmb_cached = cache.get(_gmb_cache_key)
            if _gmb_cached:
                content = truncate(_gmb_cached.content, max_tokens)
                _log(f"FETCH {url} -> Gem board CACHED ({time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": _gmb_cached.content_type, "url": url, "cached": True}
        _gmb_session = wafer.AsyncSession(
            browser_solver=browser_solver,
            timeout=timedelta(seconds=timeout),
            cache_dir=get_wafer_cache_dir(),
            max_response_size=10 * 1024 * 1024,
        )
        _gmb_slug = extract_gem_board_slug(url)
        _gmb_jobs = await fetch_gem_board(_gmb_slug, _gmb_session)
        if _gmb_jobs is not None:
            markdown = render_gem_board(_gmb_jobs, _gmb_slug, source_url=url)
            if cache and _gmb_cache_key:
                cache.set(_gmb_cache_key, markdown, "markdown")
            content = truncate(markdown, max_tokens)
            _log(
                f"FETCH {url} -> Gem board ({len(_gmb_jobs)} jobs, {len(content)} chars, {time.monotonic() - start:.1f}s)"
            )
            return {"content": content, "content_type": "markdown", "url": url}
        # API 404 (not a Gem-hosted board) or transient failure — fall through.

    # Lever direct URL (jobs.lever.co/{company}/{id}): fetch JSON API +
    # parse /apply page for the application form, then render as markdown.
    if is_lever_url(url) and structured:
        _lv_cache_key = _intercept_cache_key(url) if cache else None
        if cache and structured and _lv_cache_key:
            _lv_cached = cache.get(_lv_cache_key)
            if _lv_cached:
                content = truncate(_lv_cached.content, max_tokens)
                _log(f"FETCH {url} -> Lever CACHED ({time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": _lv_cached.content_type, "url": url, "cached": True}
        _lv_session = wafer.AsyncSession(
            browser_solver=browser_solver,
            timeout=timedelta(seconds=timeout),
            cache_dir=get_wafer_cache_dir(),
            max_response_size=10 * 1024 * 1024,
        )
        _lv_company, _lv_id = extract_lever_params(url)
        _lv_data = await fetch_lever_job(_lv_company, _lv_id, _lv_session)
        if _lv_data and _lv_data.get("posting"):
            markdown = render_lever_job(_lv_data, source_url=url)
            if cache and _lv_cache_key:
                cache.set(_lv_cache_key, markdown, "markdown")
            content = truncate(markdown, max_tokens)
            _log(f"FETCH {url} -> Lever API ({len(content)} chars, {time.monotonic() - start:.1f}s)")
            return {"content": content, "content_type": "markdown", "url": url}
        # API failed — fall through to normal HTML fetch.

    # Dayforce posting (jobs.dayforcehcm.com/{lang}/{namespace}/{board}/jobs/{id}):
    # the page is SSR'd with full jobData in __NEXT_DATA__. Without this block
    # the generic HTML pipeline strips the Next.js shell down to "Sign In".
    if is_dayforce_url(url) and structured:
        _df_cache_key = _intercept_cache_key(url) if cache else None
        if cache and structured and _df_cache_key:
            _df_cached = cache.get(_df_cache_key)
            if _df_cached:
                content = truncate(_df_cached.content, max_tokens)
                _log(f"FETCH {url} -> Dayforce CACHED ({time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": _df_cached.content_type, "url": url, "cached": True}
        _df_session = wafer.AsyncSession(
            browser_solver=browser_solver,
            timeout=timedelta(seconds=timeout),
            cache_dir=get_wafer_cache_dir(),
            max_response_size=10 * 1024 * 1024,
        )
        _df_data = await fetch_dayforce_job(url, _df_session)
        if _df_data:
            markdown = render_dayforce_job(_df_data, source_url=url)
            if cache and _df_cache_key:
                cache.set(_df_cache_key, markdown, "markdown")
            content = truncate(markdown, max_tokens)
            _log(f"FETCH {url} -> Dayforce posting ({len(content)} chars, {time.monotonic() - start:.1f}s)")
            return {"content": content, "content_type": "markdown", "url": url}
        # Parse failed — fall through to normal HTML fetch.

    # Dayforce board index: list of postings comes from a CSRF-protected
    # POST to /api/geo/{namespace}/jobposting/search after hydration.
    if is_dayforce_board_url(url) and structured:
        _dfb_cache_key = _intercept_cache_key(url) if cache else None
        if cache and structured and _dfb_cache_key:
            _dfb_cached = cache.get(_dfb_cache_key)
            if _dfb_cached:
                content = truncate(_dfb_cached.content, max_tokens)
                _log(f"FETCH {url} -> Dayforce board CACHED ({time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": _dfb_cached.content_type, "url": url, "cached": True}
        _dfb_session = wafer.AsyncSession(
            browser_solver=browser_solver,
            timeout=timedelta(seconds=timeout),
            cache_dir=get_wafer_cache_dir(),
            max_response_size=10 * 1024 * 1024,
        )
        _dfb_data = await fetch_dayforce_board(url, _dfb_session)
        if _dfb_data is not None:
            markdown = render_dayforce_board(_dfb_data, source_url=url)
            if cache and _dfb_cache_key:
                cache.set(_dfb_cache_key, markdown, "markdown")
            content = truncate(markdown, max_tokens)
            _log(
                f"FETCH {url} -> Dayforce board ({len(_dfb_data.get('jobPostings') or [])} jobs, {len(content)} chars, {time.monotonic() - start:.1f}s)"
            )
            return {"content": content, "content_type": "markdown", "url": url}
        # Search call failed — fall through to normal HTML fetch.

    # Cornerstone OnDemand (CSOD) posting: SPA shell with a JWT in
    # csod.context. Hit services/x/job-requisition/v2/requisitions/{id}/jobDetails.
    if is_cornerstone_url(url) and structured:
        _cs_cache_key = _intercept_cache_key(url) if cache else None
        if cache and structured and _cs_cache_key:
            _cs_cached = cache.get(_cs_cache_key)
            if _cs_cached:
                content = truncate(_cs_cached.content, max_tokens)
                _log(f"FETCH {url} -> Cornerstone CACHED ({time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": _cs_cached.content_type, "url": url, "cached": True}
        _cs_session = wafer.AsyncSession(
            browser_solver=browser_solver,
            timeout=timedelta(seconds=timeout),
            cache_dir=get_wafer_cache_dir(),
            max_response_size=10 * 1024 * 1024,
        )
        _cs_data = await fetch_cornerstone_job(url, _cs_session)
        if _cs_data:
            markdown = render_cornerstone_job(_cs_data, source_url=url)
            if cache and _cs_cache_key:
                cache.set(_cs_cache_key, markdown, "markdown")
            content = truncate(markdown, max_tokens)
            _log(f"FETCH {url} -> Cornerstone posting ({len(content)} chars, {time.monotonic() - start:.1f}s)")
            return {"content": content, "content_type": "markdown", "url": url}
        # API failed — fall through to normal HTML fetch.

    # Cornerstone board index: rec-job-search/external/jobs on the regional
    # cloud host carried in csod.context.endpoints.cloud.
    if is_cornerstone_board_url(url) and structured:
        _csb_cache_key = _intercept_cache_key(url) if cache else None
        if cache and structured and _csb_cache_key:
            _csb_cached = cache.get(_csb_cache_key)
            if _csb_cached:
                content = truncate(_csb_cached.content, max_tokens)
                _log(f"FETCH {url} -> Cornerstone board CACHED ({time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": _csb_cached.content_type, "url": url, "cached": True}
        _csb_session = wafer.AsyncSession(
            browser_solver=browser_solver,
            timeout=timedelta(seconds=timeout),
            cache_dir=get_wafer_cache_dir(),
            max_response_size=10 * 1024 * 1024,
        )
        _csb_data = await fetch_cornerstone_board(url, _csb_session)
        if _csb_data is not None:
            markdown = render_cornerstone_board(_csb_data, source_url=url)
            if cache and _csb_cache_key:
                cache.set(_csb_cache_key, markdown, "markdown")
            content = truncate(markdown, max_tokens)
            _log(
                f"FETCH {url} -> Cornerstone board ({len(_csb_data.get('requisitions') or [])} jobs, {len(content)} chars, {time.monotonic() - start:.1f}s)"
            )
            return {"content": content, "content_type": "markdown", "url": url}
        # Search call failed — fall through to normal HTML fetch.

    # Workday posting ({tenant}.wd{N}.myworkdayjobs.com/[lang/]{site}/job/{path}):
    # the page is a SPA shell; the full posting JSON lives at
    # /wday/cxs/{tenant}/{site}/job{externalPath}.
    if is_workday_url(url) and structured:
        _wd_cache_key = _intercept_cache_key(url) if cache else None
        if cache and structured and _wd_cache_key:
            _wd_cached = cache.get(_wd_cache_key)
            if _wd_cached:
                content = truncate(_wd_cached.content, max_tokens)
                _log(f"FETCH {url} -> Workday CACHED ({time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": _wd_cached.content_type, "url": url, "cached": True}
        _wd_session = wafer.AsyncSession(
            browser_solver=browser_solver,
            timeout=timedelta(seconds=timeout),
            cache_dir=get_wafer_cache_dir(),
            max_response_size=10 * 1024 * 1024,
        )
        _wd_data = await fetch_workday_job(url, _wd_session)
        if _wd_data:
            markdown = render_workday_job(_wd_data, source_url=url)
            if cache and _wd_cache_key:
                cache.set(_wd_cache_key, markdown, "markdown")
            content = truncate(markdown, max_tokens)
            _log(f"FETCH {url} -> Workday posting ({len(content)} chars, {time.monotonic() - start:.1f}s)")
            return {"content": content, "content_type": "markdown", "url": url}
        # API failed — fall through to normal HTML fetch.

    # Workday board index: paginated POST to /wday/cxs/{tenant}/{site}/jobs.
    if is_workday_board_url(url) and structured:
        _wdb_cache_key = _intercept_cache_key(url) if cache else None
        if cache and structured and _wdb_cache_key:
            _wdb_cached = cache.get(_wdb_cache_key)
            if _wdb_cached:
                content = truncate(_wdb_cached.content, max_tokens)
                _log(f"FETCH {url} -> Workday board CACHED ({time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": _wdb_cached.content_type, "url": url, "cached": True}
        _wdb_session = wafer.AsyncSession(
            browser_solver=browser_solver,
            timeout=timedelta(seconds=timeout),
            cache_dir=get_wafer_cache_dir(),
            max_response_size=10 * 1024 * 1024,
        )
        _wdb_data = await fetch_workday_board(url, _wdb_session)
        if _wdb_data is not None:
            markdown = render_workday_board(_wdb_data, source_url=url)
            if cache and _wdb_cache_key:
                cache.set(_wdb_cache_key, markdown, "markdown")
            content = truncate(markdown, max_tokens)
            _log(
                f"FETCH {url} -> Workday board ({len(_wdb_data.get('jobPostings') or [])} jobs, {len(content)} chars, {time.monotonic() - start:.1f}s)"
            )
            return {"content": content, "content_type": "markdown", "url": url}
        # Search call failed — fall through to normal HTML fetch.

    # BambooHR posting ({tenant}.bamboohr.com/careers/{id}): JSON at
    # /careers/{id}/detail returns the full jobOpening incl. description HTML.
    if is_bamboohr_url(url) and structured:
        _bh_cache_key = _intercept_cache_key(url) if cache else None
        if cache and structured and _bh_cache_key:
            _bh_cached = cache.get(_bh_cache_key)
            if _bh_cached:
                content = truncate(_bh_cached.content, max_tokens)
                _log(f"FETCH {url} -> BambooHR CACHED ({time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": _bh_cached.content_type, "url": url, "cached": True}
        _bh_session = wafer.AsyncSession(
            browser_solver=browser_solver,
            timeout=timedelta(seconds=timeout),
            cache_dir=get_wafer_cache_dir(),
            max_response_size=10 * 1024 * 1024,
        )
        _bh_tenant, _bh_id = extract_bamboohr_params(url)
        _bh_data = await fetch_bamboohr_job(_bh_tenant, _bh_id, _bh_session)
        if _bh_data:
            markdown = render_bamboohr_job(_bh_data, source_url=url)
            if cache and _bh_cache_key:
                cache.set(_bh_cache_key, markdown, "markdown")
            content = truncate(markdown, max_tokens)
            _log(f"FETCH {url} -> BambooHR posting ({len(content)} chars, {time.monotonic() - start:.1f}s)")
            return {"content": content, "content_type": "markdown", "url": url}
        # API failed — fall through to normal HTML fetch.

    # BambooHR board index: GET /careers/list returns the full job list as JSON.
    if is_bamboohr_board_url(url) and structured:
        _bhb_cache_key = _intercept_cache_key(url) if cache else None
        if cache and structured and _bhb_cache_key:
            _bhb_cached = cache.get(_bhb_cache_key)
            if _bhb_cached:
                content = truncate(_bhb_cached.content, max_tokens)
                _log(f"FETCH {url} -> BambooHR board CACHED ({time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": _bhb_cached.content_type, "url": url, "cached": True}
        _bhb_session = wafer.AsyncSession(
            browser_solver=browser_solver,
            timeout=timedelta(seconds=timeout),
            cache_dir=get_wafer_cache_dir(),
            max_response_size=10 * 1024 * 1024,
        )
        _bhb_tenant = extract_bamboohr_board_params(url)
        _bhb_data = await fetch_bamboohr_board(_bhb_tenant, _bhb_session)
        if _bhb_data is not None:
            markdown = render_bamboohr_board(_bhb_data, _bhb_tenant, source_url=url)
            if cache and _bhb_cache_key:
                cache.set(_bhb_cache_key, markdown, "markdown")
            content = truncate(markdown, max_tokens)
            _log(
                f"FETCH {url} -> BambooHR board ({len(_bhb_data.get('result') or [])} jobs, {len(content)} chars, {time.monotonic() - start:.1f}s)"
            )
            return {"content": content, "content_type": "markdown", "url": url}
        # Search call failed — fall through to normal HTML fetch.

    # JazzHR posting ({tenant}.applytojob.com/apply/{id}/{slug}): the page
    # carries a schema.org JobPosting JSON-LD block we can render directly.
    if is_jazzhr_url(url) and structured:
        _jz_cache_key = _intercept_cache_key(url) if cache else None
        if cache and structured and _jz_cache_key:
            _jz_cached = cache.get(_jz_cache_key)
            if _jz_cached:
                content = truncate(_jz_cached.content, max_tokens)
                _log(f"FETCH {url} -> JazzHR CACHED ({time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": _jz_cached.content_type, "url": url, "cached": True}
        _jz_session = wafer.AsyncSession(
            browser_solver=browser_solver,
            timeout=timedelta(seconds=timeout),
            cache_dir=get_wafer_cache_dir(),
            max_response_size=10 * 1024 * 1024,
        )
        _jz_tenant, _jz_id = extract_jazzhr_params(url)
        _jz_data = await fetch_jazzhr_job(_jz_tenant, _jz_id, _jz_session)
        if _jz_data:
            markdown = render_jazzhr_job(_jz_data, source_url=url)
            if cache and _jz_cache_key:
                cache.set(_jz_cache_key, markdown, "markdown")
            content = truncate(markdown, max_tokens)
            _log(f"FETCH {url} -> JazzHR posting ({len(content)} chars, {time.monotonic() - start:.1f}s)")
            return {"content": content, "content_type": "markdown", "url": url}
        # Parse failed — fall through to normal HTML fetch.

    # JazzHR board index ({tenant}.applytojob.com/apply): parse SSR'd listing.
    if is_jazzhr_board_url(url) and structured:
        _jzb_cache_key = _intercept_cache_key(url) if cache else None
        if cache and structured and _jzb_cache_key:
            _jzb_cached = cache.get(_jzb_cache_key)
            if _jzb_cached:
                content = truncate(_jzb_cached.content, max_tokens)
                _log(f"FETCH {url} -> JazzHR board CACHED ({time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": _jzb_cached.content_type, "url": url, "cached": True}
        _jzb_session = wafer.AsyncSession(
            browser_solver=browser_solver,
            timeout=timedelta(seconds=timeout),
            cache_dir=get_wafer_cache_dir(),
            max_response_size=10 * 1024 * 1024,
        )
        _jzb_tenant = extract_jazzhr_tenant(url)
        _jzb_jobs = await fetch_jazzhr_board(_jzb_tenant, _jzb_session)
        if _jzb_jobs is not None:
            markdown = render_jazzhr_board(_jzb_jobs, _jzb_tenant, source_url=url)
            if cache and _jzb_cache_key:
                cache.set(_jzb_cache_key, markdown, "markdown")
            content = truncate(markdown, max_tokens)
            _log(
                f"FETCH {url} -> JazzHR board ({len(_jzb_jobs)} jobs, {len(content)} chars, {time.monotonic() - start:.1f}s)"
            )
            return {"content": content, "content_type": "markdown", "url": url}
        # Parse failed — fall through to normal HTML fetch.

    # Canonicalize Reddit URLs to New Reddit. Normal mapped URLs use compact
    # anonymous JSON; explicit .json stays raw and raw=True fetches New Reddit
    # HTML through the generic path below.
    reddit_result = transform_reddit_url(url)
    fetch_url_str = reddit_result.url
    is_reddit = reddit_result.is_reddit
    reddit_route = (
        route_reddit_url(url, max_tokens=max_tokens)
        if is_reddit
        else None
    )
    reddit_explicit_json = bool(
        reddit_route and reddit_route.is_explicit_json
    )
    if is_reddit and structured:
        if reddit_route and reddit_route.is_mapped and not reddit_route.is_explicit_json:
            from .reddit_fetch import fetch_mapped_reddit

            result = await fetch_mapped_reddit(
                reddit_route,
                max_tokens=max_tokens,
                timeout=float(timeout),
                chars_per_token=config.chars_per_token if config else 4,
                config=config,
                queue=reddit_queue,
                browser_solver=browser_solver,
            )
            if "content" in result:
                _log(
                    f"FETCH {url} -> Reddit {reddit_route.kind} "
                    f"({len(result['content'])} chars, {time.monotonic() - start:.1f}s)"
                )
            return result

    # Transform GitHub blob URLs to raw.githubusercontent.com
    github_result = transform_github_url(url)
    is_github = github_result.is_github
    if github_result.is_blob and structured:
        fetch_url_str = github_result.url

    # Tier 1: Transform known forum URLs to RSS/Atom feeds
    forum_result = transform_forum_url(fetch_url_str)
    is_forum_feed = forum_result.is_forum_feed
    if is_forum_feed and structured:
        fetch_url_str = forum_result.url

    # Costco fallback: when search API returns 0 results, fetch the category page
    if _costco_redirect_fallback and _costco_fallback_url:
        fetch_url_str = _costco_fallback_url

    # Compute normalized URL once for cache operations
    # Gated on `structured`: the write path below keys off cache_key alone,
    # so a non-structured request must not have one to write under.
    cache_key = normalize_url(fetch_url_str) if cache and structured else None

    # Check cache (structured mode only: raw and non-GET requests bypass it)
    if cache and structured and cache_key:
        cached = cache.get(cache_key)
        if cached:
            cached_mime = cached.content_type.lower().partition(";")[0].strip()
            is_cached_json = cached_mime in {
                "json",
                "application/json",
            } or cached_mime.endswith("+json")
            try:
                content = (
                    None
                    if reddit_explicit_json and not is_cached_json
                    else
                    await _truncate_json_isolated(
                        cached.content,
                        max_tokens,
                        config.chars_per_token if config else 4,
                        timeout=float(timeout),
                    )
                    if is_cached_json
                    else truncate(cached.content, max_tokens)
                )
            except _JsonBudgetExceededError:
                return {
                    "error": _JSON_BUDGET_ERROR,
                    "url": fetch_url_str,
                    "cached": True,
                }
            except _JsonProcessingError:
                return {
                    "error": "JSON processing failed within its safety limits.",
                    "url": fetch_url_str,
                    "cached": True,
                }
            if content is None:
                # A stale/bad JSON cache entry must never turn into a success
                # response. Invalidate it and obtain a fresh representation.
                cache.invalidate(cache_key)
            else:
                result_dict = {
                    "content": content,
                    "content_type": cached.content_type,
                    "url": fetch_url_str,
                    "cached": True,
                }
                if is_cached_json:
                    result_dict["_json_budget_applied"] = True
                if is_reddit and fetch_url_str != url and not is_cached_json:
                    result_dict["content"] = truncate(
                        f"[Fetched via: {fetch_url_str}]\n\n{cached.content}",
                        max_tokens,
                    )
                _log(f"FETCH {url} -> CACHED ({time.monotonic() - start:.1f}s)")
                return result_dict

    # Per-domain rate limiting for sites that aggressively block rapid requests
    if _is_costco(fetch_url_str):
        from ..ratelimit import costco_limiter

        await costco_limiter.wait()
    elif _is_soylent(fetch_url_str):
        from ..ratelimit import soylent_limiter

        await soylent_limiter.wait()

    # SSRF-safe fetch. Resolve + validate the fetch host, then pin the wafer
    # session to those exact IPs so wafer cannot re-resolve to an internal
    # address between our check and its connect (DNS rebinding / TOCTOU). We
    # follow redirects manually (follow_redirects=False) so every hop is
    # validated and pinned *before* we connect to it, and rebuild the session
    # when a redirect introduces a new host (resolve= is snapshotted at
    # construction). Pins accumulate across hops so a rebuilt session covers
    # every host seen so far.
    pins: dict[str, list[str]] = {}
    validated_hosts: set[str] = set()  # every host we vetted (DNS + IP-literal)

    async def _validate_and_pin(target_url: str) -> str | None:
        """Validate ``target_url``'s host, pin it, and record it as vetted.

        Returns an error message if the host is private/unresolvable, else None.
        IP-literal hosts need no pin (the URL already targets a fixed address).
        """
        try:
            target = urlparse(target_url)
            if (
                target.scheme not in ("http", "https")
                or not target.hostname
                or target.username is not None
                or target.password is not None
                or len(target_url) > 8192
                or any(ord(char) < 32 or ord(char) == 127 for char in target_url)
            ):
                return "Invalid URL."
            _ = target.port
        except (TypeError, ValueError):
            return "Invalid URL."
        h = _canon_host(target.hostname)
        if not h:
            return "Invalid URL (no host to resolve)."
        if h in validated_hosts:
            return None
        verdict = await check_host(h)
        if verdict.blocked:
            return verdict.message
        if verdict.ips:  # DNS host -> pin to validated IPs; IP literal -> nothing to pin
            pins[h] = verdict.ips
        validated_hosts.add(h)
        return None

    def _make_session(cookie_url: str, *, solver=browser_solver) -> wafer.AsyncSession:
        # Plain construction (no `async with`): the session is rebuilt across
        # redirect hops and reused by downstream handlers, so its lifetime spans
        # beyond this helper — and wafer sessions need no cleanup (they never
        # close an injected browser_solver).
        # timeout= is the TOTAL call budget (retries + rotations + browser solve),
        # not per-attempt: bound each try with attempt_timeout=(caller's timeout)
        # and floor the total so a challenge solve isn't starved (default 10s is
        # far too little). max_response_size streams + aborts early (bomb guard).
        s = wafer.AsyncSession(
            browser_solver=solver,
            timeout=timedelta(seconds=timeout),
            attempt_timeout=timedelta(seconds=timeout),
            cache_dir=get_wafer_cache_dir(),
            follow_redirects=False,  # we validate + pin each hop ourselves
            resolve=dict(pins),
            max_response_size=MAX_RESPONSE_SIZE,
            # Reddit's anonymous session is deliberately stable: rotations
            # would clear its durable cookies and hide 429/Retry-After from our
            # shared queue/limiter. Other domains retain wafer's defaults.
            max_rotations=0 if is_reddit else 2,
        )
        return s

    async def _session_for(target_url: str) -> wafer.AsyncSession:
        if transform_reddit_url(target_url).is_reddit:
            from .browse_reddit import _get_session

            return await _get_session(browser_solver)
        return _make_session(target_url)

    async def _safe_feed_get(feed_url: str):
        """SSRF-guarded GET for an autodiscovered feed URL. Its host comes from the
        fetched page's ``<link rel="alternate">``, so it was never vetted by the
        main loop — validate + pin it (fail closed) and fetch with a fresh pinned,
        no-redirect session. Returns the response, or None if the host is
        private/unresolvable or the fetch fails.
        """
        if await _validate_and_pin(feed_url) is not None:
            return None  # private / unresolvable feed host -> refuse to fetch
        feed_host = _canon_host(urlparse(feed_url).hostname or "")
        try:
            # No browser_solver: a feed is XML; a challenge-solve navigation could
            # itself escape to an internal host. Skip the feed if it's challenged.
            resp = await _make_session(feed_url, solver=None).get(feed_url, timeout=timedelta(seconds=timeout))
        except Exception:
            return None
        # Defense in depth: the feed response must come from the validated host.
        if _canon_host(urlparse(str(resp.url)).hostname or "") != feed_host:
            return None
        return resp

    err = await _validate_and_pin(fetch_url_str)
    if err:
        return {"error": err}
    session = await _session_for(fetch_url_str)
    current_host = _canon_host(urlparse(fetch_url_str).hostname or "")

    # Resolve Ashby embed URLs (e.g. company.com/careers?ashby_jid=<uuid>) to
    # the canonical jobs.ashbyhq.com/{org}/{jid} form before fetching. The embed
    # fetch reuses the already-pinned session (same host); the canonical URL is a
    # new host, so re-validate + re-pin + rebuild before the main fetch.
    if structured and is_ashby_embed_url(fetch_url_str):
        canonical = await resolve_ashby_embed_url(fetch_url_str, session)
        if canonical:
            _log(f"FETCH {url} -> Ashby embed resolved to {canonical}")
            fetch_url_str = canonical
            cache_key = normalize_url(fetch_url_str) if cache and structured else None
            if cache and structured and cache_key:
                cached = cache.get(cache_key)
                if cached:
                    content = truncate(cached.content, max_tokens)
                    _log(f"FETCH {url} -> CACHED via embed resolution ({time.monotonic() - start:.1f}s)")
                    return {
                        "content": content,
                        "content_type": cached.content_type,
                        "url": fetch_url_str,
                        "cached": True,
                    }
            err = await _validate_and_pin(fetch_url_str)
            if err:
                return {"error": err}
            session = await _session_for(fetch_url_str)
            current_host = _canon_host(urlparse(fetch_url_str).hostname or "")

    current_url = fetch_url_str
    # One deadline for the whole fetch (all hops share it) so a redirect chain
    # can't multiply the budget: each hop is a separate session request, each of
    # which would otherwise reset wafer's total-call timer.
    fetch_deadline = time.monotonic() + timeout

    # Mutated as redirects are followed. A 303 — and, matching what every browser
    # and curl actually do, a 301/302 — turns the follow-up into a bodyless GET;
    # only 307/308 replay the original method and body.
    current_method = method
    current_body = body
    current_headers = dict(request_headers)

    async def _fetch_hop(request_url: str, remaining: float):
        """Fetch one hop, charging Reddit requests to the shared budget."""

        is_reddit_request = transform_reddit_url(request_url).is_reddit

        async def _get():
            kwargs: dict = {"timeout": timedelta(seconds=remaining)}
            hop_headers = dict(current_headers)
            if is_reddit_request:
                # Wafer's durable browser identity can retain its last
                # Referer. Pin it to a harmless origin so one caller's path
                # and query can never leak into another anonymous request.
                # The caller's own Referer is dropped rather than merged: two
                # values would go on the wire, and the point of pinning is that
                # this header is not caller-controlled.
                hop_headers.pop("referer", None)
                hop_headers["Referer"] = "https://www.reddit.com/"
            if hop_headers:
                kwargs["headers"] = hop_headers
            if current_method == "GET":
                return await session.get(request_url, **kwargs)
            if current_body is not None:
                kwargs["body"] = current_body
            return await session.request(current_method, request_url, **kwargs)

        if not is_reddit_request:
            return await _get()
        if reddit_queue is not None:
            try:
                return await reddit_queue.enqueue(
                    _get,
                    _queue_timeout=min(remaining, float(timeout)),
                )
            except TimeoutError:
                raise wafer.WaferTimeout(request_url, min(remaining, float(timeout))) from None

        from ..ratelimit import reddit_limiter

        try:
            await asyncio.wait_for(
                reddit_limiter.wait(),
                timeout=min(remaining, float(timeout)),
            )
        except TimeoutError:
            raise wafer.WaferTimeout(request_url, min(remaining, float(timeout))) from None
        remaining_after_wait = fetch_deadline - time.monotonic()
        if remaining_after_wait <= 0:
            raise wafer.WaferTimeout(request_url, remaining)
        return await _fetch_hop_reddit_limited(request_url, remaining_after_wait)

    async def _fetch_hop_reddit_limited(request_url: str, remaining: float):
        """Post-rate-limit Reddit hop, preserving the caller's method/body."""
        kwargs: dict = {"timeout": timedelta(seconds=remaining)}
        hop_headers = dict(current_headers)
        hop_headers.pop("referer", None)  # pinned, not caller-controlled
        hop_headers["Referer"] = "https://www.reddit.com/"
        kwargs["headers"] = hop_headers
        if current_method == "GET":
            return await session.get(request_url, **kwargs)
        if current_body is not None:
            kwargs["body"] = current_body
        return await session.request(current_method, request_url, **kwargs)

    try:
        for _hop in range(MAX_REDIRECTS + 1):
            remaining = fetch_deadline - time.monotonic()
            if remaining <= 0:
                return {
                    "error": f"Request timed out after {timeout}s. Try increasing the timeout parameter for slow servers."
                }
            resp = await _fetch_hop(current_url, remaining)
            location = resp.headers.get("location", "")
            if resp.status_code in _REDIRECT_STATUSES and location:
                next_url = urljoin(current_url, location)
                try:
                    next_parsed = urlparse(next_url)
                    if next_parsed.scheme not in ("http", "https"):
                        return {"error": (f"Redirect to unsupported protocol: {next_parsed.scheme}.")}
                    _ = next_parsed.port
                except (TypeError, ValueError):
                    return {"error": "Redirect target is not a valid HTTP(S) URL."}

                # Reddit short/share links redirect to the actual public
                # permalink. Reclassify every hop so entering Reddit receives
                # the same compact JSON treatment as a direct permalink, and
                # leaving Reddit no longer receives Reddit-specific cleanup.
                next_reddit = transform_reddit_url(next_url)
                if next_reddit.is_reddit:
                    next_url = next_reddit.url
                    next_parsed = urlparse(next_url)
                if reddit_explicit_json:
                    from .browse_reddit import (
                        _is_route_preserving_json_redirect,
                    )

                    if (
                        not next_reddit.is_reddit
                        or not _is_route_preserving_json_redirect(
                            current_url,
                            next_url,
                        )
                    ):
                        return {
                            "error": (
                                "Reddit explicit JSON redirected to a "
                                "different or non-JSON route."
                            )
                        }
                # Validate + pin the redirect target BEFORE connecting to it.
                err = await _validate_and_pin(next_url)
                if err:
                    return {"error": "Redirect to private/internal host is not allowed."}
                is_reddit = next_reddit.is_reddit
                if is_reddit and structured:
                    redirected_route = route_reddit_url(
                        next_url,
                        max_tokens=max_tokens,
                    )
                    if redirected_route and redirected_route.is_mapped and not redirected_route.is_explicit_json:
                        from .reddit_fetch import fetch_mapped_reddit

                        redirected_remaining = fetch_deadline - time.monotonic()
                        if redirected_remaining <= 0:
                            return {
                                "error": (
                                    f"Request timed out after {timeout}s. "
                                    "Try increasing the timeout parameter for slow servers."
                                )
                            }
                        return await fetch_mapped_reddit(
                            redirected_route,
                            max_tokens=max_tokens,
                            timeout=redirected_remaining,
                            chars_per_token=config.chars_per_token if config else 4,
                            config=config,
                            queue=reddit_queue,
                            browser_solver=browser_solver,
                        )
                next_host = _canon_host(next_parsed.hostname or "")
                origin_changed = _origin_of(next_parsed) != _origin_of(urlparse(current_url))
                if current_method != "GET":
                    if resp.status_code in (307, 308):
                        if origin_changed:
                            return {
                                "error": (
                                    "Cross-origin 307/308 redirect refused because "
                                    "replaying the request could expose its body or headers."
                                )
                            }
                    else:
                        # 301/302/303 -> the follow-up is a plain GET. Drop the
                        # body and the headers that only described it, or the
                        # next hop advertises a payload it will not send.
                        current_method = "GET"
                        current_body = None
                        current_headers.pop("content-type", None)
                # Caller headers were aimed at the ORIGIN they named. Use a
                # positive allowlist on an origin change: custom names can carry
                # secrets too, and scheme/port changes may cross a trust
                # boundary even when the hostname is unchanged.
                if origin_changed:
                    current_headers = {
                        name: value
                        for name, value in current_headers.items()
                        if name in _CROSS_ORIGIN_REDIRECT_SAFE_HEADERS
                    }
                if next_host != current_host:
                    # New host: rebuild so the pin covers it. Cookies persist
                    # via the shared cache_dir.
                    session = await _session_for(next_url)
                    current_host = next_host
                current_url = next_url
                continue
            # Final response (not a redirect, or a 3xx without a Location).
            result = FetchResult(
                content=resp.content,  # capped by max_response_size on the session
                content_type=resp.headers.get("content-type", ""),
                status_code=resp.status_code,
                final_url=str(resp.url),
                headers=dict(resp.headers),
            )
            break
        else:
            return {"error": "Too many redirects (redirect loop detected)."}
    except wafer.ChallengeDetected as e:
        return {"error": describe_challenge(e.challenge_type)}
    except wafer.RateLimited as e:
        if is_reddit:
            retry_after = e.retry_after
            if reddit_queue is not None:
                reddit_queue.set_backoff(429, retry_after=retry_after)
            else:
                from ..ratelimit import reddit_limiter

                reddit_limiter.defer(60.0 if retry_after is None else retry_after)
        retry_msg = f" Retry after {e.retry_after:.0f} seconds." if e.retry_after else ""
        return {"error": f"Rate limited (HTTP 429).{retry_msg}"}
    except wafer.EmptyResponse:
        return {"error": "Server returned an empty response after retries."}
    except wafer.TooManyRedirects:
        return {"error": "Too many redirects (redirect loop detected)."}
    except wafer.ConnectionFailed as e:
        return {"error": f"Connection error: {e.reason}"}
    except wafer.WaferTimeout:
        return {"error": f"Request timed out after {timeout}s. Try increasing the timeout parameter for slow servers."}
    except wafer.ResponseTooLarge:
        return {"error": f"Response too large (exceeds {MAX_RESPONSE_SIZE // (1024 * 1024)}MB limit)."}
    except wafer.WaferError as e:
        return {"error": f"Request failed: {e}"}
    except Exception as e:
        return {"error": f"Fetch failed ({type(e).__name__}): {e}"}

    # Defense in depth: the response's final host MUST be one we vetted above.
    # Per-hop validation pins every host we deliberately connect to, but a browser
    # solve or native-TLS passthrough could surface a response whose URL host we
    # never validated — reject it. This is host-set membership, NOT a re-resolve,
    # so it does not reopen the TOCTOU window the pinning closes. (Supersedes the
    # old post-redirect check, which re-resolved and ran after the body was read.)
    final_host = _canon_host(urlparse(result.final_url).hostname or "")
    if not final_host or final_host not in validated_hosts:
        _log(f"FETCH {url} -> ERROR: final host {final_host!r} was not validated")
        return {"error": "Response from an unvalidated host is not allowed."}

    # Downstream board/embed handlers (Greenhouse/Dayforce/Ashby/BambooHR/JazzHR
    # embeds, the TI doc viewer) reuse this session for SECONDARY fetches to OTHER
    # hosts. Deliberately do NOT re-enable redirect-following: those hosts are not
    # in our pin set and this session still carries the browser solver, so a 3xx
    # from an attacker-registered SaaS tenant to an internal IP (169.254.169.254,
    # etc.) would otherwise be followed with no SSRF re-validation. Keeping
    # follow_redirects=False (as set in _make_session) closes that vector; a
    # secondary fetch that legitimately redirects just yields a non-200 and the
    # caller falls through to the generic HTML path. Valid embed APIs return 200
    # from their canonical HTTPS URLs, so this does not regress them. (Feeds use
    # _safe_feed_get, which validates+pins its own no-redirect, solver-free session.)

    # Handle rate limiting (wafer passes 429 through when max_rotations=0)
    if result.status_code == 429:
        retry_header = result.headers.get("retry-after", "")
        retry_after = parse_retry_after(retry_header)
        if is_reddit:
            if reddit_queue is not None:
                reddit_queue.set_backoff(429, retry_after=retry_after)
            else:
                from ..ratelimit import reddit_limiter

                reddit_limiter.defer(60.0 if retry_after is None else retry_after)
        retry_msg = f" Retry after {retry_header} seconds." if retry_header else ""
        _log(f"FETCH {url} -> ERROR: 429 rate limited ({time.monotonic() - start:.1f}s)")
        return {"error": f"Rate limited (HTTP 429).{retry_msg}"}

    content_type = result.content_type.lower()
    mime = content_type.partition(";")[0].strip()
    if reddit_explicit_json and not (
        mime == "application/json" or mime.endswith("+json")
    ):
        return {
            "error": "Reddit explicit JSON returned a non-JSON representation."
        }

    # The generic path covers caller-selected Reddit representations
    # (explicit JSON, raw New Reddit HTML, RSS, and unmapped HTML fallback).
    # Keep its 403 behavior inside the same shared identity budget as mapped
    # JSON. Structured private/quarantined/gated states are content responses,
    # not a transport block, and therefore must not poison that budget.
    if result.status_code == 403 and is_reddit:
        payload = None
        if mime == "application/json" or mime.endswith("+json"):
            try:
                payload = await run_isolated(
                    json.loads,
                    result.text,
                    timeout=min(float(timeout), 20.0),
                )
            except (TypeError, ValueError, IsolatedProcessingError):
                pass
        from .browse_reddit import format_reddit_http_error

        _, should_backoff = format_reddit_http_error(403, payload)
        if should_backoff:
            retry_after = parse_retry_after(result.headers.get("retry-after"))
            if reddit_queue is not None:
                reddit_queue.set_backoff(403, retry_after=retry_after)
            else:
                from ..ratelimit import reddit_limiter

                reddit_limiter.defer(300.0 if retry_after is None else retry_after)

    # Handle errors
    if result.status_code >= 400:
        # Costco fallback 404: the category page doesn't exist, return 0-results message
        if _costco_redirect_fallback and result.status_code == 404:
            _log(f"FETCH {url} -> Costco category fallback 404, returning no-results")
            return {
                "content": f"Costco search returned no results for this query. "
                f"The category page ({_costco_fallback_url}) also does not exist.",
                "content_type": "text",
                "url": url,
            }
        # Named apart from the request `body` parameter, which is still live in
        # this scope.
        error_body = result.text[:1000]
        _log(f"FETCH {url} -> ERROR: HTTP {result.status_code} ({time.monotonic() - start:.1f}s)")
        return {"error": f"HTTP {result.status_code}", "body": error_body}

    # JSON
    if mime == "application/json" or mime.endswith("+json"):
        text = result.text
        try:
            content = await _truncate_json_isolated(
                text,
                max_tokens,
                config.chars_per_token if config else 4,
                timeout=float(timeout),
            )
        except _JsonBudgetExceededError:
            return {"error": _JSON_BUDGET_ERROR}
        except _JsonProcessingError:
            return {"error": "JSON processing failed within its safety limits."}
        if content is None:
            return {"error": "Received invalid JSON response."}
        return {
            "content": content,
            "content_type": "json",
            "url": result.final_url,
            "_json_budget_applied": True,
        }

    # Plain text
    if "text/plain" in content_type:
        text = result.text
        return {
            "content": truncate(text, max_tokens),
            "content_type": "text",
            "url": result.final_url,
        }

    # XML/RSS/Atom — try structured feed parsing first (unless raw mode)
    if any(t in content_type for t in ("text/xml", "application/xml", "application/rss+xml", "application/atom+xml")):
        text = result.text
        if raw:
            return {
                "content": truncate(text, max_tokens),
                "content_type": "xml",
                "url": result.final_url,
            }
        try:
            rendered_feed = await run_isolated(
                parse_and_format_feed,
                text,
                timeout=min(float(timeout), 20.0),
            )
        except IsolatedProcessingError:
            return {"error": "Feed parsing failed within its safety limits."}
        if rendered_feed:
            markdown, feed_item_count = rendered_feed
            if is_reddit:
                markdown = canonicalize_reddit_links(markdown)
            content = truncate(markdown, max_tokens)
            response = {
                "content": content,
                "content_type": "markdown",
                "url": result.final_url,
            }
            if is_forum_feed and forum_result.url != forum_result.original_url:
                response["content"] = f"[Feed: {forum_result.original_url}]\n\n{content}"
            _log(
                f"FETCH {url} -> feed ({feed_item_count} items, {len(response['content'])} chars, {time.monotonic() - start:.1f}s)"
            )
            return response
        # Not a feed — return raw XML
        return {
            "content": truncate(text, max_tokens),
            "content_type": "xml",
            "url": result.final_url,
        }

    # CSV
    if "text/csv" in content_type:
        text = result.text
        return {
            "content": truncate(text, max_tokens),
            "content_type": "csv",
            "url": result.final_url,
        }

    # PDF
    if "application/pdf" in content_type:
        # TI datasheets: try HTML document viewer
        ti_part = extract_ti_part_from_pdf_url(fetch_url_str)
        if not ti_part and result.final_url:
            ti_part = extract_ti_part_from_pdf_url(result.final_url)
        if ti_part:
            viewer_url = f"https://www.ti.com/document-viewer/{ti_part}/datasheet"
            try:
                viewer_resp = await session.get(viewer_url)
                if viewer_resp.status_code == 200:
                    viewer_html = viewer_resp.text
                    combined = await fetch_document_sections(session, viewer_html, float(timeout))
                    if combined:
                        markdown, _ = await html_to_markdown(combined, url=viewer_url)
                        if cache and cache_key:
                            cache.set(
                                cache_key,
                                markdown,
                                "markdown",
                                cache_control=viewer_resp.headers.get("cache-control"),
                                vary=viewer_resp.headers.get("vary"),
                            )
                        content = truncate(markdown, max_tokens)
                        _log(
                            f"FETCH {url} -> TI doc viewer upgrade ({len(content)} chars, {time.monotonic() - start:.1f}s)"
                        )
                        return {
                            "content": f"[Upgraded from PDF to HTML datasheet]\n\n{content}",
                            "content_type": "markdown",
                            "url": viewer_url,
                        }
            except Exception:
                pass  # Fall through to PDF extraction

        pdf_result = await extract_pdf(result.content, config)

        if pdf_result.error:
            return {"error": pdf_result.error}

        if pdf_result.is_empty:
            content = f"[PDF: {pdf_result.page_count} pages. No extractable text found - this may be a scanned document or image-based PDF.]"
        else:
            header = f"[PDF: {pdf_result.page_count} pages.]\n\n"
            chars_per_token = config.chars_per_token if config else 4
            reserved_tokens = len(header) // chars_per_token + 10
            if max_tokens <= reserved_tokens:
                content = truncate(header, max_tokens, chars_per_token)
            else:
                content = header + truncate(
                    pdf_result.text,
                    max_tokens - reserved_tokens,
                    chars_per_token,
                )

        return {
            "content": content,
            "content_type": "pdf",
            "url": result.final_url,
        }

    # HTML - convert to markdown (unless raw mode)
    if "text/html" in content_type or "application/xhtml" in content_type:
        if raw:
            html = result.text
            return {
                "content": truncate(html, max_tokens),
                "content_type": "html",
                "url": result.final_url,
            }

        try:
            validate_html_input_size(result.content)
        except HtmlProcessingError as exc:
            return {"error": str(exc)}
        html = result.text

        effective_url = result.final_url or url
        # The URL-pattern interceptors above are not the only ones: every field
        # this preflight returns arms a handler below that issues its OWN
        # secondary request and REPLACES the body with somebody else's board,
        # feed, or listing. On a POST that is exactly wrong — the caller would
        # get a GET of an embedded job board instead of their own response — so
        # a non-GET skips the inspection entirely rather than each handler
        # having to remember to opt out.
        if not structured:
            preflight = HtmlPreflight()
        else:
            try:
                preflight = await run_isolated(
                    inspect_html_preflight,
                    html,
                    effective_url,
                    bool(not is_forum_feed and forum_result.forum_software and not forum_result.is_thread),
                    bool(not is_forum_feed and not forum_result.forum_software and not is_thread_url(effective_url)),
                    bool(not _skip_aliexpress_intercept and is_aliexpress_search_url(effective_url)),
                    is_github,
                    timeout=min(float(timeout), 20.0),
                )
            except IsolatedProcessingError as exc:
                return {"error": (f"HTML site-specific inspection failed within its safety limits: {exc}")}

        # Every parser above runs in the disposable worker. Only bounded,
        # validated metadata crosses back into the async network orchestrator.
        _structured_embed_detected = preflight.greenhouse_detected
        if preflight.greenhouse_params:
            _gh_params = preflight.greenhouse_params
            if _gh_params:
                _gh_token, _gh_jid = _gh_params
                _gh_data = await fetch_greenhouse_job(_gh_token, _gh_jid, session)
                if _gh_data:
                    markdown = render_greenhouse_job(_gh_data, source_url=result.final_url or url)
                    if cache and cache_key:
                        cache.set(cache_key, markdown, "markdown")
                    content = truncate(markdown, max_tokens)
                    _log(f"FETCH {url} -> Greenhouse embed API ({len(content)} chars, {time.monotonic() - start:.1f}s)")
                    return {"content": content, "content_type": "markdown", "url": result.final_url}

        # White-label Dayforce: company sites host the Dayforce Next.js
        # candidate portal under their own domain (e.g.
        # synaptivemedical.com/job-openings). __NEXT_DATA__ carries the
        # canonical clientNamespace + careerSiteXRefCode, which we use to
        # rewrite to the jobs.dayforcehcm.com board flow.
        _df_canonical = preflight.dayforce_url
        if _df_canonical:
            _structured_embed_detected = True
            _df_data = await fetch_dayforce_board(_df_canonical, session)
            if _df_data is not None:
                markdown = render_dayforce_board(_df_data, source_url=result.final_url or url)
                if cache and cache_key:
                    cache.set(cache_key, markdown, "markdown")
                content = truncate(markdown, max_tokens)
                _log(
                    f"FETCH {url} -> Dayforce white-label board via {_df_canonical} ({len(_df_data.get('jobPostings') or [])} jobs, {len(content)} chars, {time.monotonic() - start:.1f}s)"
                )
                return {
                    "content": f"[Dayforce-hosted board: {_df_canonical}]\n\n{content}",
                    "content_type": "markdown",
                    "url": result.final_url,
                }

        # Ashby script-tag embed on a company career site
        # (<script src="https://jobs.ashbyhq.com/{org}/embed">): pull the
        # canonical board listing instead of returning the empty spinner.
        _ab_embed_slug = preflight.ashby_slug
        if _ab_embed_slug:
            _structured_embed_detected = True
            try:
                _ab_data = await fetch_ashby_board(_ab_embed_slug, session)
            except AshbyBoardTooLargeError:
                # Same reasoning as the direct-board path: the board exists, so
                # falling through would render the host page's empty spinner as
                # the job list. This path reads on the shared fetch session, so
                # the limit that tripped is the general one, not Ashby's.
                return {
                    "error": (
                        f"Ashby job board '{_ab_embed_slug}' embedded in {url} is "
                        f"larger than the {MAX_RESPONSE_SIZE // (1024 * 1024)}MB "
                        "read limit and was not truncated into a partial board."
                    )
                }
            if _ab_data is not None:
                markdown = render_ashby_board(_ab_data, _ab_embed_slug, source_url=result.final_url or url)
                if cache and cache_key:
                    cache.set(cache_key, markdown, "markdown")
                content = truncate(markdown, max_tokens)
                _log(
                    f"FETCH {url} -> Ashby embed board {_ab_embed_slug} ({len(_ab_data.get('jobs') or [])} jobs, {len(content)} chars, {time.monotonic() - start:.1f}s)"
                )
                return {
                    "content": f"[Ashby-hosted board: jobs.ashbyhq.com/{_ab_embed_slug}]\n\n{content}",
                    "content_type": "markdown",
                    "url": result.final_url,
                }

        # BambooHR widget on a company career site
        # (<div id="BambooHR" data-domain="{tenant}.bamboohr.com">): pull
        # the full job list via /careers/list on the BambooHR subdomain.
        _bh_embed = preflight.bamboohr_tenant
        if _bh_embed:
            _structured_embed_detected = True
            _bh_data = await fetch_bamboohr_board(_bh_embed, session)
            if _bh_data is not None:
                markdown = render_bamboohr_board(_bh_data, _bh_embed, source_url=result.final_url or url)
                if cache and cache_key:
                    cache.set(cache_key, markdown, "markdown")
                content = truncate(markdown, max_tokens)
                _log(
                    f"FETCH {url} -> BambooHR embed board {_bh_embed} ({len(_bh_data.get('result') or [])} jobs, {len(content)} chars, {time.monotonic() - start:.1f}s)"
                )
                return {
                    "content": f"[BambooHR-hosted board: {_bh_embed}.bamboohr.com]\n\n{content}",
                    "content_type": "markdown",
                    "url": result.final_url,
                }

        # JazzHR embed: company sites pull jobs from one or more
        # ``*.applytojob.com`` boards via JS (e.g. earthdaily.com/job-openings
        # pulls from earthdaily + earthdailyagro). Aggregate any tenants we
        # find in the page markup.
        _jz_embed_tenants = preflight.jazzhr_tenants
        if _jz_embed_tenants:
            _structured_embed_detected = True
            _jz_boards: list[tuple[str, list[dict]]] = []
            for _jz_t in _jz_embed_tenants:
                _jz_jobs = await fetch_jazzhr_board(_jz_t, session)
                if _jz_jobs is not None:
                    _jz_boards.append((_jz_t, _jz_jobs))
            if _jz_boards:
                if len(_jz_boards) == 1:
                    _jz_t, _jz_jobs = _jz_boards[0]
                    markdown = render_jazzhr_board(_jz_jobs, _jz_t, source_url=result.final_url or url)
                else:
                    markdown = render_jazzhr_boards(_jz_boards, source_url=result.final_url or url)
                if cache and cache_key:
                    cache.set(cache_key, markdown, "markdown")
                content = truncate(markdown, max_tokens)
                _tot = sum(len(j) for _, j in _jz_boards)
                _log(
                    f"FETCH {url} -> JazzHR embed ({len(_jz_boards)} tenants, {_tot} jobs, {len(content)} chars, {time.monotonic() - start:.1f}s)"
                )
                _hint = ", ".join(f"{t}.applytojob.com" for t, _ in _jz_boards)
                return {
                    "content": f"[JazzHR-hosted boards: {_hint}]\n\n{content}",
                    "content_type": "markdown",
                    "url": result.final_url,
                }

        # Tier 2: Forum autodiscovery
        if preflight.feed_url:
            try:
                feed_resp = await _safe_feed_get(preflight.feed_url)
                if feed_resp is not None and feed_resp.status_code < 400:
                    feed_text = feed_resp.text
                    try:
                        rendered_feed = await run_isolated(
                            parse_and_format_feed,
                            feed_text,
                            timeout=min(float(timeout), 20.0),
                        )
                    except IsolatedProcessingError:
                        rendered_feed = None
                    if rendered_feed:
                        markdown, feed_item_count = rendered_feed
                        content = truncate(markdown, max_tokens)
                        response = {
                            "content": f"[Feed: {url}]\n\n{content}",
                            "content_type": "markdown",
                            "url": result.final_url,
                        }
                        _log(
                            f"FETCH {url} -> autodiscovered feed ({feed_item_count} items, {len(response['content'])} chars, {time.monotonic() - start:.1f}s)"
                        )
                        return response
            except Exception:
                pass

        # TI document viewer
        if is_ti_document_viewer(effective_url):
            combined = await fetch_document_sections(session, html, float(timeout))
            if combined:
                html = combined

        # AliExpress search extraction
        if preflight.aliexpress_search:
            search_content = preflight.aliexpress_search
            if search_content:
                content = truncate(search_content, max_tokens)
                if cache and cache_key:
                    cache.set(cache_key, search_content, "text")
                _log(
                    f"FETCH {url} -> AliExpress search extraction ({len(content)} chars, {time.monotonic() - start:.1f}s)"
                )
                return {"content": content, "content_type": "text", "url": effective_url}

        # GitHub issue/PR/discussion extraction
        if is_github:
            issue_content = preflight.github_issue
            if issue_content:
                content = truncate(issue_content, max_tokens)
                if cache and cache_key:
                    cache.set(cache_key, issue_content, "text")
                _log(f"FETCH {url} -> GitHub issue extraction ({len(content)} chars, {time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": "text", "url": effective_url}

        # GitHub file listing extraction
        file_listing = preflight.github_file_listing if is_github else None

        try:
            markdown, _ = await html_to_markdown(
                html,
                is_reddit=is_reddit,
                url=result.final_url,
            )
        except HtmlProcessingError as exc:
            return {"error": str(exc)}
        if is_reddit:
            markdown = canonicalize_reddit_links(markdown)

        # Prepend file listing if found
        if file_listing:
            if "\n---\n" in file_listing:
                markdown = file_listing
            elif len(markdown) > 200:
                markdown = file_listing + "\n\n---\n\n" + markdown
            else:
                markdown = file_listing

        # A page whose content arrives by JavaScript extracts "successfully" to
        # a shell, and the caller records "this site has nothing" instead of
        # "this needs a browser". We cannot render it, but we can refuse to let
        # it pass as a complete answer. The DOM scan is gated (see
        # needs_dom_scan) so ordinary pages never pay for a second parse.
        js_render_note = None
        if structured and js_render.needs_dom_scan(html, markdown):
            try:
                markers, shell_metadata = await run_isolated(
                    js_render.collect_shell_evidence,
                    html,
                    timeout=min(float(timeout), 20.0),
                )
            except IsolatedProcessingError:
                markers, shell_metadata = (), {}
            js_render_note = js_render.describe(markers, len(html), markdown, shell_metadata)

        # An HTML document that extracts to nothing is a gap, not a success.
        # Returning "" leaves the caller unable to tell "this page has no
        # content" from "we were served an interstitial and stripped it", and
        # it would poison the cache with an empty entry. Name it instead.
        if not markdown.strip():
            return {
                "error": (
                    f"No readable content could be extracted from {url} "
                    f"({len(html)} bytes of HTML). The page is most likely a "
                    "JavaScript-gated shell or an interstitial rather than an "
                    "empty document."
                )
            }

        # Prepended before caching so a cache hit carries the same warning as
        # the fetch that produced it — the page does not stop being a shell.
        if js_render_note:
            markdown = f"{js_render_note}\n\n{markdown}"

        # Cache full content, truncate only for response
        if cache and cache_key and not _structured_embed_detected:
            cache.set(
                cache_key,
                markdown,
                "markdown",
                cache_control=result.headers.get("cache-control"),
                vary=result.headers.get("vary"),
            )

        content = truncate(markdown, max_tokens)

        response = {
            "content": content,
            "content_type": "markdown",
            "url": result.final_url,
        }

        # Note if we transformed the URL
        if _costco_redirect_fallback:
            note = "[Costco search returned no direct results and redirected to a category page]"
            if result.final_url and result.final_url != fetch_url_str:
                note += f"\n[Redirected to: {result.final_url}]"
            response["content"] = f"{note}\n\n{content}"
        elif is_reddit and fetch_url_str != url:
            response["content"] = truncate(
                f"[Fetched via: {fetch_url_str}]\n\n{markdown}",
                max_tokens,
            )
        elif github_result.is_blob and fetch_url_str != url:
            response["content"] = f"[Fetched raw: {fetch_url_str}]\n\n{content}"
        elif result.final_url and result.final_url != fetch_url_str:
            response["content"] = f"[Redirected to: {result.final_url}]\n\n{content}"

        _log(
            f"FETCH {url} -> {response['content_type']} ({len(response['content'])} chars, {time.monotonic() - start:.1f}s)"
        )
        return response

    # SVG - textual XML, return as raw
    if "image/svg+xml" in content_type or "image/svg" in content_type:
        text = result.text
        return {
            "content": truncate(text, max_tokens),
            "content_type": "svg",
            "url": result.final_url,
        }

    # Binary images - return metadata only (can't render bytes as text)
    if content_type.startswith("image/"):
        summary = _format_image_summary(result, fetch_url_str)
        return {
            "content": summary,
            "content_type": "text",
            "url": result.final_url,
        }

    # Any other text-based content type
    if content_type.startswith("text/") or content_type.startswith("application/javascript"):
        text = result.text
        return {
            "content": truncate(text, max_tokens),
            "content_type": "text",
            "url": result.final_url,
        }

    # Unsupported content type
    return {"error": f"Unsupported content type: {content_type}"}


async def fetch_url(
    url: str,
    max_tokens: int = 25000,
    timeout: int = 10,
    raw: bool = False,
    cache: ResponseCache | None = None,
    config: Config | None = None,
    browser_solver=None,
    reddit_queue: RedditRequestQueue | None = None,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: str | None = None,
    _skip_aliexpress_intercept: bool = False,
    _skip_alibaba_intercept: bool = False,
    _skip_craigslist_intercept: bool = False,
) -> dict:
    """Fetch a URL within the caller's end-to-end timeout budget."""
    if timeout <= 0:
        return {"error": "timeout must be greater than zero."}
    try:
        async with asyncio.timeout(float(timeout)):
            result = await _fetch_url_impl(
                url=url,
                max_tokens=max_tokens,
                timeout=timeout,
                raw=raw,
                cache=cache,
                config=config,
                browser_solver=browser_solver,
                reddit_queue=reddit_queue,
                method=method,
                headers=headers,
                body=body,
                _skip_aliexpress_intercept=_skip_aliexpress_intercept,
                _skip_alibaba_intercept=_skip_alibaba_intercept,
                _skip_craigslist_intercept=_skip_craigslist_intercept,
            )
            if isinstance(result.get("content"), str):
                chars_per_token = config.chars_per_token if config else 4
                result_mime = str(result.get("content_type") or "").lower().partition(";")[0].strip()
                is_result_json = result_mime in {"json", "application/json"} or result_mime.endswith("+json")
                json_budget_applied = bool(result.pop("_json_budget_applied", False))
                if is_result_json and not json_budget_applied:
                    try:
                        content = await _truncate_json_isolated(
                            result["content"],
                            max_tokens,
                            chars_per_token,
                            timeout=float(timeout),
                        )
                    except _JsonBudgetExceededError:
                        return {"error": _JSON_BUDGET_ERROR}
                    except _JsonProcessingError:
                        return {"error": ("JSON processing failed within its safety limits.")}
                    if content is None:
                        return {"error": "Received invalid JSON response."}
                    result["content"] = content
                elif not is_result_json:
                    result["content"] = truncate(result["content"], max_tokens, chars_per_token)
            if isinstance(result.get("body"), str):
                chars_per_token = config.chars_per_token if config else 4
                total_chars = max(0, max_tokens * chars_per_token)
                prefix = f"Error: {result.get('error', '')}\n\nPartial content:\n"
                remaining_chars = max(0, total_chars - len(prefix))
                if remaining_chars:
                    result["body"] = result["body"][:remaining_chars]
                else:
                    result.pop("body", None)
            return result
    except TimeoutError:
        return {
            "error": (f"Request timed out after {timeout}s. Try increasing the timeout parameter for slow servers.")
        }
