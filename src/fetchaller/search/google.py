"""Google web results from the public Google Docs research-search JSON API."""

import asyncio
import base64
import json
import os
import random
import re
import sys
import time
from datetime import UTC, datetime
from html import unescape
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from ..content._isolated import IsolatedProcessingError, run_isolated
from ..security.xss import redact_secrets_for_log, sanitize_for_log
from .models import SearchResult
from .pacing import retry_after_seconds as _retry_after

_COUNT = 10
_LANGUAGE = "en"
_MAX_RESPONSE_CHARS = 2 * 1024 * 1024
_MIN_INTERVAL = 6.0
_REQUEST_BUDGET = 3  # Total attempts, including document failover and transient retries.
_DOCUMENT_COOLDOWN = 900.0
_CURSOR_TTL = 300.0
_MAX_CURSORS = 1000
_document_unavailable: dict[str, float] = {}
_cursors: dict[tuple[str, str, int, int], tuple[str | None, float]] = {}
_lock = asyncio.Lock()
_last_request = 0.0
_last_document: str | None = None
_backoff_until = 0.0
_throttle_count = 0


def _log(message: str) -> None:
    bounded = sanitize_for_log(" ".join(message.splitlines()), max_length=1000)
    print(
        f"[{datetime.now(UTC).isoformat()}] {sanitize_for_log(redact_secrets_for_log(bounded), max_length=500)}",
        file=sys.stderr,
    )


def document_ids() -> tuple[str, ...]:
    """Load only the verified pool, or a deployment's explicit JSON-array override."""
    configured = os.environ.get("FETCHALLER_GOOGLE_DOCUMENT_IDS")
    if configured is None:
        values = [
            entry["id"]
            for entry in json.loads(Path(__file__).with_name("google_documents.json").read_text())["documents"]
        ]
    else:
        values = json.loads(configured)
    if (
        not isinstance(values, list)
        or not values
        or len(values) > 100
        or any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{20,200}", value) for value in values)
    ):
        raise ValueError("FETCHALLER_GOOGLE_DOCUMENT_IDS must be a JSON array of 1-100 valid document IDs")
    return tuple(dict.fromkeys(values))


def page_cursor(page: int) -> str:
    """Encode protobuf field 1 (unsigned varint offset), including offsets >=128."""
    offset = (page - 1) * _COUNT
    encoded = bytearray([8])
    while offset >= 128:
        encoded.append((offset & 127) | 128)
        offset >>= 7
    encoded.append(offset)
    return base64.b64encode(encoded).decode("ascii")


def _text(value: str, limit: int) -> str:
    return " ".join(BeautifulSoup(unescape(value), "html.parser").get_text(" ", strip=True).split())[:limit]


def parse_response(body: str) -> tuple[list[SearchResult], str | None]:
    """Validate the message before interpreting absent records as an honest zero."""
    body = body.lstrip()
    if body.startswith(")]}'"):
        _, separator, body = body.partition("\n")
        if not separator:
            raise ValueError("Google XSSI guard has no JSON payload")
    root = json.loads(body)
    if not isinstance(root, list) or not root or not isinstance(root[0], list):
        raise ValueError("Unexpected Google response envelope")
    message = root[0]
    if not message or message[0] != "documentsuggest.search.search_response":
        raise ValueError("Unexpected Google response message")
    records = message[1] if len(message) > 1 else None
    cursor = message[2] if len(message) > 2 else None
    if records is not None and not isinstance(records, list):
        raise ValueError("Unexpected Google records field")
    if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 4096):
        raise ValueError("Unexpected Google cursor field")
    results = []
    seen = set()
    for record in records or []:
        if not isinstance(record, list) or not record:
            raise ValueError("Unexpected Google record")
        if record[0] is None:
            continue  # Knowledge cards have no organic result array.
        organic = record[0]
        if not isinstance(organic, list) or len(organic) < 3 or not all(isinstance(v, str) for v in organic[:3]):
            raise ValueError("Unexpected Google organic result")
        url, title, snippet = organic[:3]
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or len(url) > 8192
            or any(ord(c) <= 32 or ord(c) == 127 for c in url)
        ):
            continue
        if url.split("#")[0] in seen:
            continue
        seen.add(url.split("#")[0])
        title = _text(title, 500)
        if title:
            results.append(SearchResult(title=title, url=url, snippet=_text(snippet, 1000)))
        if len(results) >= _COUNT:
            break
    return results, cursor or None


async def search_google(session, query: str, page: int = 1) -> tuple[list[SearchResult], bool, str | None]:
    """One globally paced Google search, with bounded document/transient failover.

    The bool indicates Google-wide throttling/backoff. No redirects, cookies,
    CAPTCHA solving or HTML SERP fallback are used.
    """
    global _last_request, _last_document, _backoff_until, _throttle_count
    async with _lock:
        now = time.monotonic()
        if now < _backoff_until:
            return [], True, f"Google rate-limit backoff ({_backoff_until - now:.0f}s remaining)"
        try:
            ids = document_ids()
        except (ValueError, KeyError, OSError, TypeError):
            return [], False, "Invalid Google document pool configuration"
        healthy = [doc for doc in ids if _document_unavailable.get(doc, 0) <= now]
        random.shuffle(healthy)
        if len(healthy) > 1 and healthy[0] == _last_document:
            healthy.append(healthy.pop(0))
        if not healthy:
            return [], False, "All Google document IDs are in cooldown"
        for key, (_, saved) in list(_cursors.items()):
            if now - saved >= _CURSOR_TTL:
                del _cursors[key]
        key = (query, _LANGUAGE, _COUNT, page)
        cursor = _cursors[key][0] if key in _cursors else page_cursor(page)
        if cursor is None:
            return [], False, None
        document = healthy.pop(0)
        error = "Google request budget exhausted"
        for attempt in range(_REQUEST_BUDGET):
            wait = _MIN_INTERVAL - (time.monotonic() - _last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            _last_request = time.monotonic()
            _last_document = document
            try:
                response = await session.post(
                    f"https://docs.google.com/document/d/{document}/explore/search",
                    form={
                        "request": json.dumps(
                            [
                                "documentsuggest.search.search_request",
                                query,
                                [_COUNT, cursor],
                                [_LANGUAGE, _LANGUAGE],
                                1,
                            ],
                            ensure_ascii=False,
                        )
                    },
                    timeout=10,
                )
            except Exception as exc:
                error = f"Google request failed ({type(exc).__name__})"
                _log(f"WARNING {error}; attempt={attempt + 1}")
                if attempt == 0:
                    continue  # At most one transient retry, same document/cursor.
                return [], False, error
            status = response.status_code
            if status == 429:
                duration = max(
                    _retry_after(response.headers.get("retry-after")), (120, 300, 900)[min(_throttle_count, 2)]
                )
                _throttle_count += 1
                _backoff_until = time.monotonic() + duration
                _log(f"WARNING Google HTTP 429; global backoff={duration:.0f}s")
                return [], True, f"HTTP 429; Google rate-limit backoff ({duration:.0f}s)"
            if status in {401, 403, 404} or 300 <= status < 400:
                _document_unavailable[document] = time.monotonic() + _DOCUMENT_COOLDOWN
                error = f"Google document unavailable (HTTP {status})"
                destination = ""
                if 300 <= status < 400:
                    target = urlsplit(response.headers.get("location", ""))
                    destination = f" redirect={target.hostname or ''}{target.path}"
                _log(f"WARNING {error}; document={document}{destination}; cooling down and skipping")
                if healthy:
                    document = healthy.pop(0)
                    continue
                return [], False, error
            if status != 200:
                error = f"Google HTTP {status}"
                if 500 <= status < 600 and attempt == 0:
                    _log(f"WARNING {error}; retrying once")
                    continue
                return [], False, error
            if len(response.text) > _MAX_RESPONSE_CHARS:
                return [], False, "Google response exceeded the safe JSON size limit"
            try:
                results, next_cursor = await run_isolated(parse_response, response.text, timeout=10)
            except (IsolatedProcessingError, ValueError, TypeError):
                _log(f"WARNING Google protocol error; HTTP 200; response_chars={len(response.text)}")
                return [], False, "Unexpected Google response structure (HTTP 200)"
            _document_unavailable.pop(document, None)
            _throttle_count = 0
            if len(_cursors) >= _MAX_CURSORS:
                del _cursors[min(_cursors, key=lambda k: _cursors[k][1])]
            _cursors[(query, _LANGUAGE, _COUNT, page + 1)] = (next_cursor, time.monotonic())
            return results, False, None
        return [], False, error
