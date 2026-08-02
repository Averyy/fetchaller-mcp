"""The cacheable request plan, and the code that replays it.

A plan is what discovery produces: a plain-HTTP request that returns the data,
with the build-coupled parts stripped out and the volatile parts replaced by
mint steps. It must be JSON round-trippable exactly — caching is the entire
point, since discovery costs a browser launch and tens of seconds while a
replay costs one request.

Most of this module is the traps. They are not incidental: every one of them
produces a *plausible-looking* failure, which is precisely the confusion
discovery exists to remove.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import wafer

logger = logging.getLogger(__name__)

# Headers wafer's transport sets itself. Sending a captured copy duplicates them
# under HTTP/2, which is a protocol error rather than a last-wins overwrite —
# and overriding the UA or client hints would contradict the session's
# fingerprint envelope. What ships is a *delta*, never the captured set.
TRANSPORT_HEADERS = frozenset(
    {
        "accept-encoding",
        "accept-language",
        "connection",
        "content-length",
        "cookie",
        "host",
        "keep-alive",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "user-agent",
    }
)
_TRANSPORT_PREFIXES = ("sec-ch-", "sec-fetch-", ":")

_MARKER_RE = re.compile(r"\{\{mint:([A-Za-z0-9_]+)\}\}")
_FORMISH_RE = re.compile(r"^[^=&\s]+=[^&]*(?:&[^=&\s]*=[^&]*)*$")


class PlanUnresolvedError(RuntimeError):
    """A mint marker survived into the request that was about to be sent.

    Raised rather than sent. A marker that reaches the origin comes back as an
    ordinary-looking rejection, which is exactly the ambiguity this whole
    exercise exists to remove.
    """


class MintFailedError(RuntimeError):
    """A mint step ran but did not yield its value."""


@dataclass(frozen=True)
class MintStep:
    """How to re-derive one volatile value."""

    name: str
    method: str
    url: str
    source: str  # "header" | "regex"
    selector: str  # header name, or a pattern whose group(1) is the value

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "method": self.method,
            "url": self.url,
            "source": self.source,
            "selector": self.selector,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> MintStep:
        return cls(
            name=raw["name"],
            method=raw.get("method", "GET"),
            url=raw["url"],
            source=raw.get("source", "regex"),
            selector=raw["selector"],
        )


@dataclass
class RequestPlan:
    """A reproducible plain-HTTP request that returns the page's data."""

    method: str
    url: str  # query values may contain {{mint:NAME}}
    headers: dict = field(default_factory=dict)  # delta only
    body: str | None = None  # serialized; may contain markers
    body_kind: str | None = None  # "json" | "form" | "raw" | None
    mint: tuple[MintStep, ...] = ()
    verified: bool = False
    required_fields: tuple[str, ...] = ()
    dropped_fields: tuple[str, ...] = ()
    # What the verified plan returned. This is what makes decay detectable at
    # runtime: a later replay returning far fewer records means the plan rotted,
    # not that the board is empty. Measured on Meta — the healthy plan returns
    # 128,515 bytes and 588 records; incrementing doc_id by one returns HTTP 200,
    # 141 bytes and 1 record. Trivially distinguishable.
    record_count: int = 0
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "method": self.method,
            "url": self.url,
            "headers": dict(self.headers),
            "body": self.body,
            "body_kind": self.body_kind,
            "mint": [step.to_dict() for step in self.mint],
            "verified": self.verified,
            "required_fields": list(self.required_fields),
            "dropped_fields": list(self.dropped_fields),
            "record_count": self.record_count,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> RequestPlan:
        return cls(
            method=raw["method"],
            url=raw["url"],
            headers=dict(raw.get("headers") or {}),
            body=raw.get("body"),
            body_kind=raw.get("body_kind"),
            mint=tuple(MintStep.from_dict(s) for s in raw.get("mint") or ()),
            verified=bool(raw.get("verified")),
            required_fields=tuple(raw.get("required_fields") or ()),
            dropped_fields=tuple(raw.get("dropped_fields") or ()),
            record_count=int(raw.get("record_count") or 0),
            notes=tuple(raw.get("notes") or ()),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> RequestPlan:
        return cls.from_dict(json.loads(text))


# ---------------------------------------------------------------------------
# Field handling
# ---------------------------------------------------------------------------


def header_delta(headers: dict) -> dict:
    """Drop the headers wafer's transport owns."""
    out = {}
    for name, value in (headers or {}).items():
        lowered = str(name).casefold()
        if lowered in TRANSPORT_HEADERS or lowered.startswith(_TRANSPORT_PREFIXES):
            continue
        out[lowered] = str(value)
    return out


def pairs_to_fields(pairs) -> dict:
    """Group query/form pairs, keeping repeats as a list.

    ``dict(parse_qsl(...))`` keeps only the last value, and Amazon's search
    route sends ``facets[]`` **twelve times**. Collapsing them loses eleven.
    """
    fields: dict = {}
    for name, value in pairs:
        if name in fields:
            existing = fields[name]
            fields[name] = existing + [value] if isinstance(existing, list) else [existing, value]
        else:
            fields[name] = value
    return fields


def encode_fields(fields: dict) -> str:
    """Encode a field mapping, preserving repeats."""
    encodable = {
        key: [str(item) for item in value] if isinstance(value, list) else str(value)
        for key, value in fields.items()
    }
    return urlencode(encodable, doseq=True)


def classify_body(body: str | None, content_type: str = "") -> tuple[str | None, dict]:
    """Decide how to treat a captured request body, and expose its fields."""
    if body is None or body == "":
        return None, {}
    content_type = (content_type or "").casefold()

    def as_json():
        try:
            return json.loads(body)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None

    if "json" in content_type:
        decoded = as_json()
        if isinstance(decoded, dict):
            return "json", dict(decoded)
        # A JSON *array* body is positional: Google addresses its arguments by
        # index, so dropping a slot shifts every argument after it. Treating it
        # as raw is the right answer, not a limitation.
        if isinstance(decoded, list):
            return "raw", {}

    if "x-www-form-urlencoded" in content_type:
        return "form", pairs_to_fields(parse_qsl(body, keep_blank_values=True))

    decoded = as_json()
    if isinstance(decoded, dict):
        return "json", dict(decoded)
    if isinstance(decoded, list):
        return "raw", {}

    if _FORMISH_RE.match(body):
        return "form", pairs_to_fields(parse_qsl(body, keep_blank_values=True))
    return "raw", {}


# ---------------------------------------------------------------------------
# Marker substitution
# ---------------------------------------------------------------------------


def substitute(value, minted: dict):
    """Replace ``{{mint:NAME}}`` markers throughout a structure."""
    if isinstance(value, str):
        def swap(match):
            name = match.group(1)
            return minted[name] if name in minted else match.group(0)

        return _MARKER_RE.sub(swap, value)
    if isinstance(value, dict):
        return {key: substitute(item, minted) for key, item in value.items()}
    if isinstance(value, list):
        return [substitute(item, minted) for item in value]
    return value


def unresolved_markers(value) -> set[str]:
    """Marker names still present, searched over *decoded* values."""
    if isinstance(value, str):
        return set(_MARKER_RE.findall(value))
    if isinstance(value, dict):
        found: set[str] = set()
        for key, item in value.items():
            found |= unresolved_markers(key)
            found |= unresolved_markers(item)
        return found
    if isinstance(value, (list, tuple)):
        found = set()
        for item in value:
            found |= unresolved_markers(item)
        return found
    return set()


def resolve_plan(plan: RequestPlan, minted: dict) -> tuple[str, dict, dict, set[str]]:
    """Turn a stored plan into concrete request arguments.

    Order matters: **decode, substitute, then re-encode**, and measure
    unresolved markers on the *decoded* values. A ``{{mint:X}}`` marker inside a
    form body is stored percent-encoded as ``%7B%7Bmint%3AX%7D%7D``, so
    substituting into the serialized string finds nothing and the marker itself
    goes out as the token. That bit twice — once in the replay path, and again
    in the unresolved-marker check, which scanned the already-encoded body and
    cheerfully reported everything resolved.
    """
    headers = {k: v for k, v in substitute(dict(plan.headers), minted).items()}
    send: dict = {}
    pending: set[str] = set()

    if plan.body_kind == "json" and plan.body is not None:
        payload = substitute(json.loads(plan.body), minted)
        pending |= unresolved_markers(payload)
        send["json"] = payload
        # wafer sets the JSON content-type itself; sending the captured one
        # alongside duplicates the header, and HTTP/2 rejects duplicates.
        headers.pop("content-type", None)
    elif plan.body_kind == "form" and plan.body is not None:
        fields = substitute(pairs_to_fields(parse_qsl(plan.body, keep_blank_values=True)), minted)
        pending |= unresolved_markers(fields)  # BEFORE encoding
        # Encoded here rather than passed to form=, which flattens repeats.
        send["body"] = encode_fields(fields)
        headers["content-type"] = "application/x-www-form-urlencoded"
    elif plan.body is not None:
        body = substitute(plan.body, minted)
        pending |= unresolved_markers(body)
        send["body"] = body

    parts = urlsplit(plan.url)
    path = substitute(parts.path, minted)
    if parts.query:
        query = substitute(pairs_to_fields(parse_qsl(parts.query, keep_blank_values=True)), minted)
        pending |= unresolved_markers(query)
        url = urlunsplit((parts.scheme, parts.netloc, path, encode_fields(query), parts.fragment))
    else:
        url = urlunsplit((parts.scheme, parts.netloc, path, "", parts.fragment))

    pending |= unresolved_markers([url, headers])
    return url, headers, send, pending


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def cookie_header(cookie: dict) -> str:
    """Render a browser cookie as a ``Set-Cookie`` header string."""
    parts = [f"{cookie.get('name', '')}={cookie.get('value', '')}"]
    domain = cookie.get("domain")
    if domain:
        parts.append(f"Domain={domain}")
    parts.append(f"Path={cookie.get('path') or '/'}")
    if cookie.get("secure"):
        parts.append("Secure")
    if cookie.get("httpOnly"):
        parts.append("HttpOnly")
    same_site = cookie.get("sameSite")
    if same_site in ("Strict", "Lax", "None"):
        parts.append(f"SameSite={same_site}")
    return "; ".join(parts)


def seed_cookies(session: wafer.AsyncSession, cookies) -> int:
    """Carry the observing browser's cookies into the replay session.

    Without this every probe goes out cookieless, so an origin sees a browser
    load the page with a session and then several dozen anonymous requests hit
    its data endpoint — which is a scrape signature, and draws exactly the
    throttling that then reads as "this board has no API". The browser already
    earned a session; the probes should continue it rather than arrive as
    strangers.
    """
    seeded = 0
    for cookie in cookies or ():
        name = cookie.get("name")
        domain = (cookie.get("domain") or "").lstrip(".")
        if not name or not domain:
            continue
        scheme = "https" if cookie.get("secure", True) else "http"
        url = f"{scheme}://{domain}{cookie.get('path') or '/'}"
        try:
            session.add_cookie(cookie_header(cookie), url)
            seeded += 1
        except (NotImplementedError, ValueError, TypeError):
            # Opera Mini has no jar, and a malformed cookie is not worth
            # failing a discovery pass over.
            continue
    return seeded


def _header_value(headers, name: str) -> str | None:
    if headers is None:
        return None
    try:
        direct = headers.get(name)
    except AttributeError:
        return None
    if direct:
        return direct
    lowered = name.casefold()
    for key, value in dict(headers).items():
        if str(key).casefold() == lowered:
            return value
    return None


async def mint_values(
    session: wafer.AsyncSession, steps, *, timeout: float = 30.0, throttle=None
) -> dict[str, str]:
    """Run each mint step and collect the values it yields.

    Steps sharing a URL fetch once: two copies of a CSRF token fetched
    separately can disagree, which fails in a way that looks like a bad token.
    """
    fetched: dict[str, tuple[object, str]] = {}
    minted: dict[str, str] = {}

    for step in steps:
        if step.url not in fetched:
            if throttle is not None:
                await throttle()
            try:
                response = await session.request(step.method or "GET", step.url, timeout=timeout)
                fetched[step.url] = (response.headers, response.text)
            except wafer.EmptyResponse as exc:
                # A token endpoint can legitimately answer 200 with an empty
                # body and the value in a header — Apple's GET /api/v1/CSRFToken
                # does exactly this — and wafer's empty-200 guard raises on it.
                if exc.response is None:
                    raise MintFailedError(f"{step.name}: {step.url} returned an empty response") from exc
                fetched[step.url] = (exc.response.headers, exc.response.text or "")

        headers, text = fetched[step.url]
        if step.source == "header":
            value = _header_value(headers, step.selector)
        else:
            match = re.search(step.selector, text or "")
            value = match.group(1) if match else None
        if not value:
            raise MintFailedError(f"{step.name}: {step.source} {step.selector!r} yielded nothing")
        minted[step.name] = value
    return minted


async def execute(
    session: wafer.AsyncSession,
    plan: RequestPlan,
    *,
    timeout: float = 45.0,
    minted: dict | None = None,
    throttle=None,
):
    """Replay a plan and return the wafer response.

    ``throttle`` is an optional awaitable called before each request. It is off
    by default because a cached replay is one request per user action, like
    every other client. Discovery passes one, because a discovery pass is a
    burst of ~50 replays at a single host.
    """
    if minted is None:
        minted = (
            await mint_values(session, plan.mint, timeout=timeout, throttle=throttle)
            if plan.mint
            else {}
        )
    url, headers, send, pending = resolve_plan(plan, minted)
    if pending:
        # Raise rather than send: a marker that reaches the origin comes back
        # as an ordinary-looking rejection.
        raise PlanUnresolvedError(f"unresolved mint markers: {', '.join(sorted(pending))}")
    if throttle is not None:
        await throttle()
    return await session.request(
        plan.method, url, headers=headers or None, timeout=timeout, **send
    )
