"""Live end-to-end smoke test through the real stdio MCP protocol.

This is intentionally not a direct-Python component test. It starts the same
``fetchaller-mcp`` process clients use, initializes MCP, verifies the exact tool
surface/version, and calls all ten tools. A blocked browser/challenge response,
thin/empty payload, protocol error, or tool ``isError`` is a failed gate.

Run from the repository root:

    uv run python scripts/smoke_test.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from fetchaller import __version__
from fetchaller.content._price import has_positive_price

EXPECTED_TOOLS = [
    "fetch",
    "browse_reddit",
    "search_reddit",
    "search",
    "get_aliexpress_product",
    "search_aliexpress",
    "get_alibaba_product",
    "search_alibaba",
    "search_marketplace",
    "search_linkedin_jobs",
    "get_linkedin_job",
    "search_realtor",
]

_STDIO_COMMAND_ENV = "SMOKE_STDIO_COMMAND"

_BLOCKED_RESPONSE = re.compile(
    r"(?:please wait for verification|"
    r"you(?:'|’)ve been blocked by network security|"
    r"\baccess denied\b|"
    r"verify (?:that )?(?:you are|you're) (?:a )?human|"
    r"captcha (?:required|verification|challenge)|"
    r"protected by .*bot detection|"
    r"could not be bypassed|"
    r"blocked by tmd|"
    r"tmd bot protection|"
    r"browser solve (?:did not|timed out|failed)|"
    r"challenge (?:was )?not solved)",
    re.IGNORECASE,
)


@dataclass
class Result:
    name: str
    passed: bool
    detail: str
    text: str = ""


SemanticCheck = Callable[[str], str | None]


def _has_numbered_http_result(text: str) -> bool:
    return bool(re.search(r"(?m)^\d+\.\s+\S.*\n(?:[^\n]*\n){0,3}\s+https?://", text))


def _price_line_parts(value: str) -> list[str]:
    """Split one rendered price line back into the fields it was composed from.

    ``has_positive_price`` validates a single price field, but renderers compose
    a display line out of several: a sale price, an original ``(was ...)``
    price, a ``~~strikethrough~~`` original, a ``-NN%`` discount, and a
    ``per <unit>`` suffix. Offering the whole line to a field validator makes a
    discounted product look priceless, so the parts are recovered first. Prose
    with digits still fails, because every part must clear the same field rules.
    """

    parts = [value]
    parts.extend(re.findall(r"\(was\s+([^)]+)\)", value, re.IGNORECASE))
    parts.extend(re.findall(r"~~([^~]+)~~", value))
    # Leading sale price, before any of the composed suffixes. ``per <unit>``
    # is deliberately not a split point: a real per-unit price already clears
    # the field rules whole, so splitting there would let the prose in
    # ``$1.43 per minimum order 100 pieces`` through on its first token.
    head = re.split(r"\s*(?:\(was\b|~~|-\d+(?:\.\d+)?%)", value, maxsplit=1)[0]
    parts.append(head)
    return [part.strip() for part in parts if part.strip()]


def _has_real_price(text: str, prefix: str) -> bool:
    """Require a currency-bearing positive price, not prose with digits."""

    for matched in re.finditer(rf"(?mi)^\s*{re.escape(prefix)}\s*(.+)$", text):
        value = matched.group(1).strip()
        if any(
            has_positive_price(part, require_currency=True)
            for part in _price_line_parts(value)
        ):
            return True
    return False


def _validate_fetch(text: str) -> str | None:
    if "Example Domain" not in text:
        return "missing rendered Example Domain content"
    return None


def _validate_browse_reddit(text: str) -> str | None:
    if not re.search(r"(?m)^r/Python · hot · [1-9]\d* posts$", text):
        return "missing non-empty r/Python hot-listing header"
    if not re.search(r"https://www\.reddit\.com/r/Python/comments/[a-z0-9]+/", text):
        return "missing canonical Reddit post URL"
    return None


def _validate_search_reddit(text: str) -> str | None:
    if not re.search(
        r'(?m)^Search: "asyncio" in r/Python · [^·\n]+ · [^·\n]+ · [1-9]\d* results$',
        text,
    ):
        return "missing non-empty scoped Reddit search header"
    if not re.search(r"https://www\.reddit\.com/r/Python/comments/[a-z0-9]+/", text):
        return "missing canonical Reddit search-result URL"
    return None


def _validate_linkedin_search(text: str) -> str | None:
    """A real board answer: numbered jobs with stable canonical job URLs."""
    if "# LinkedIn jobs" not in text:
        return "missing LinkedIn results header"
    if not re.search(r"https://www\.linkedin\.com/jobs/view/\d+", text):
        return "no canonical linkedin.com/jobs/view URL"
    if not re.search(r"^1\. \*\*", text, re.M):
        return "no numbered job entries"
    if "trackingId" in text or "refId" in text:
        return "per-request tracking parameters leaked into output"
    return None


def _validate_linkedin_job(text: str) -> str | None:
    if not text.lstrip().startswith("#"):
        return "detail missing title heading"
    if "requires a LinkedIn account" not in text:
        return "missing apply caveat"
    return None


def _validate_web_search(text: str) -> str | None:
    if not re.search(
        r'(?m)^Search: "Python asyncio documentation".*\b[1-9]\d* total$',
        text,
    ):
        return "missing non-empty merged-search header"
    if not _has_numbered_http_result(text):
        return "missing a numbered web result with URL"
    return None


def _validate_aliexpress_search(text: str) -> str | None:
    if not re.search(
        r'(?m)^Search: "usb c cable" \| page 1 \| [1-9][\d,]* results$',
        text,
    ):
        return "missing non-empty AliExpress search header"
    if not _has_real_price(text, "Price:"):
        return "missing numeric AliExpress product prices"
    if not re.search(r"https://www\.aliexpress\.com/item/\d+\.html", text):
        return "missing AliExpress product URL"
    return None


def _validate_aliexpress_product(product_id: str) -> SemanticCheck:
    canonical_url = f"https://www.aliexpress.com/item/{product_id}.html"

    def validate(text: str) -> str | None:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if canonical_url not in text:
            return "missing canonical AliExpress product URL"
        title = lines[0].lstrip("#").strip() if lines else ""
        if (
            not lines
            or lines[0].startswith(("Rating:", "Recent reviews:", "Error:"))
            or title.lower() in {"unknown product", "product"}
            or not any(character.isalpha() for character in title)
        ):
            return "missing AliExpress product title"
        if not _has_real_price(text, "Price:"):
            return "missing numeric AliExpress core product price"
        if not re.search(
            r"(?m)^(?:Store:\s+\S|"
            r"(?:Variants|Specifications):\s*\n\s+\S)",
            text,
        ):
            return "missing AliExpress product attributes"
        return None

    return validate


def _validate_alibaba_search(text: str) -> str | None:
    if not re.search(
        r'(?m)^Search: "usb c cable" \| page 1 \| [1-9][\d,]* results$',
        text,
    ):
        return "missing non-empty Alibaba search header"
    if not _has_real_price(text, "Price:"):
        return "missing numeric Alibaba product prices"
    if not re.search(r"https://www\.alibaba\.com/(?:product-detail|product)/", text):
        return "missing Alibaba product URL"
    return None


def _validate_alibaba_product(product_id: str) -> SemanticCheck:
    canonical_url = f"https://www.alibaba.com/product-detail/_{product_id}.html"

    def validate(text: str) -> str | None:
        title_match = re.search(r"(?m)^#\s+(.+?)\s*$", text)
        title = title_match.group(1).strip() if title_match else ""
        if title.lower() in {"unknown product", "product"} or not any(character.isalpha() for character in title):
            return "missing Alibaba product title"
        if canonical_url not in text:
            return "missing canonical Alibaba product URL"
        if not _has_real_price(text, "**Price:**"):
            return "missing numeric Alibaba core product price"
        if not re.search(r"(?m)^\*\*Supplier:\*\*\s+\S", text):
            return "missing Alibaba supplier"
        if not re.search(
            r"(?m)^\*\*Specifications:\*\*[ \t]*\n[ \t]*-\s+\S[^:\n]*:\s+\S",
            text,
        ):
            return "missing substantive Alibaba specifications"
        return None

    return validate


def _validate_marketplace(text: str) -> str | None:
    expected = {
        "Kijiji": r"https://www\.kijiji\.ca/v-",
        "Craigslist": r"https://toronto\.craigslist\.org/",
        "Facebook Marketplace": r"https://www\.facebook\.com/marketplace/item/\d+/",
    }
    if not text.startswith('# Marketplace Search: "bicycle" | Toronto, ON'):
        return "missing marketplace search header"
    successful: set[str] = set()
    for platform, url_pattern in expected.items():
        heading = f"## {platform}"
        start = text.find(heading)
        if start < 0:
            continue
        later_heading = re.search(r"(?m)^##\s+", text[start + len(heading) :])
        errors_start = text.find("**Errors:**", start + len(heading))
        end_candidates = []
        if later_heading is not None:
            end_candidates.append(start + len(heading) + later_heading.start())
        if errors_start >= 0:
            end_candidates.append(errors_start)
        end = min(end_candidates) if end_candidates else len(text)
        if re.search(url_pattern, text[start:end]):
            successful.add(platform)
        else:
            return f"missing {platform} listing URL"
    if len(successful) < 2:
        return "fewer than two marketplace platforms returned listings"
    for platform in expected.keys() - successful:
        if not re.search(
            rf"(?m)^-\s+{re.escape(platform)}:\s+\S",
            text,
        ):
            return f"missing explicit {platform} error"
    return None


def _validate_realtor(text: str) -> str | None:
    if not re.search(
        r"(?m)^\*\*[1-9][\d,]* listings for sale\*\* in Toronto",
        text,
    ):
        return "missing non-empty Realtor search header"
    if not re.search(r"(?m)^\d+\. \*\*\$[\d,]+", text):
        return "missing priced Realtor listing"
    if not re.search(r"https://www\.realtor\.ca/real-estate/\d+/", text):
        return "missing Realtor listing URL"
    return None


async def _call(
    session: ClientSession,
    name: str,
    arguments: dict,
    *,
    minimum_chars: int = 40,
    semantic_check: SemanticCheck | None = None,
) -> Result:
    started = time.monotonic()
    try:
        response = await session.call_tool(
            name,
            arguments,
            read_timeout_seconds=timedelta(seconds=180),
        )
    except Exception as exc:
        return Result(
            name,
            False,
            f"{type(exc).__name__} after {time.monotonic() - started:.1f}s",
        )
    text = "\n".join(block.text for block in response.content if getattr(block, "type", None) == "text")
    elapsed = time.monotonic() - started
    blocked_match = _BLOCKED_RESPONSE.search(text)
    semantic_error = semantic_check(text) if semantic_check is not None else None
    passed = (
        not response.isError and len(text.strip()) >= minimum_chars and blocked_match is None and semantic_error is None
    )
    if passed:
        state = "ok"
    elif response.isError:
        state = "MCP isError"
    elif blocked_match:
        state = f"blocked/challenge response ({blocked_match.group(0)!r})"
    elif semantic_error:
        state = f"semantic contract failed ({semantic_error})"
    else:
        state = f"thin response ({len(text)} chars)"
    return Result(name, passed, f"{state}, {elapsed:.1f}s", text)


def _first_id(text: str, patterns: tuple[str, ...]) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


async def _pace_domain(last_call: dict[str, float], domain: str) -> None:
    """Keep live checks at least five seconds apart on the same service."""

    previous = last_call.get(domain)
    if previous is not None:
        remaining = 5.0 - (time.monotonic() - previous)
        if remaining > 0:
            await asyncio.sleep(remaining)
    last_call[domain] = time.monotonic()


async def run_live_tool_suite(session: ClientSession) -> list[Result]:
    """Exercise the exact public tool surface with semantic live assertions."""

    results: list[Result] = []
    last_call: dict[str, float] = {}
    initialized = await session.initialize()
    listed = await session.list_tools()
    names = [tool.name for tool in listed.tools]
    protocol_ok = (
        initialized.serverInfo.name == "fetchaller"
        and initialized.serverInfo.version == __version__
        and names == EXPECTED_TOOLS
    )
    results.append(
        Result(
            "initialize/list_tools",
            protocol_ok,
            (f"{initialized.serverInfo.name} {initialized.serverInfo.version}; {len(names)} tools"),
        )
    )

    results.append(
        await _call(
            session,
            "fetch",
            {
                "url": "https://example.com/",
                "maxTokens": 1000,
                "timeout": 30,
            },
            semantic_check=_validate_fetch,
        )
    )
    results.append(
        await _call(
            session,
            "browse_reddit",
            {"subreddit": "Python", "limit": 3, "timeout": 30},
            minimum_chars=100,
            semantic_check=_validate_browse_reddit,
        )
    )
    results.append(
        await _call(
            session,
            "search_reddit",
            {
                "query": "asyncio",
                "subreddit": "Python",
                "limit": 3,
                "timeout": 30,
            },
            minimum_chars=100,
            semantic_check=_validate_search_reddit,
        )
    )
    results.append(
        await _call(
            session,
            "search",
            {"query": "Python asyncio documentation", "page": 1},
            minimum_chars=100,
            semantic_check=_validate_web_search,
        )
    )

    await _pace_domain(last_call, "aliexpress.com")
    aliexpress_search = await _call(
        session,
        "search_aliexpress",
        {"query": "usb c cable", "page": 1},
        minimum_chars=100,
        semantic_check=_validate_aliexpress_search,
    )
    results.append(aliexpress_search)
    aliexpress_id = _first_id(
        aliexpress_search.text,
        (r"/item/(\d+)\.html", r"\b(\d{12,})\b"),
    )
    if not aliexpress_search.passed or aliexpress_id is None:
        results.append(
            Result(
                "get_aliexpress_product",
                False,
                "not called: no semantically valid product ID from live search",
            )
        )
    else:
        await _pace_domain(last_call, "aliexpress.com")
        results.append(
            await _call(
                session,
                "get_aliexpress_product",
                {"product_id": aliexpress_id},
                minimum_chars=100,
                semantic_check=_validate_aliexpress_product(aliexpress_id),
            )
        )

    await _pace_domain(last_call, "alibaba.com")
    alibaba_search = await _call(
        session,
        "search_alibaba",
        {"query": "usb c cable", "page": 1},
        minimum_chars=100,
        semantic_check=_validate_alibaba_search,
    )
    results.append(alibaba_search)
    alibaba_id = _first_id(
        alibaba_search.text,
        (
            r"/product-detail/[^/\s]+_(\d+)\.html",
            r"/product/(\d+)\.html",
            r"\b(\d{12,})\b",
        ),
    )
    if not alibaba_search.passed or alibaba_id is None:
        results.append(
            Result(
                "get_alibaba_product",
                False,
                "not called: no semantically valid product ID from live search",
            )
        )
    else:
        await _pace_domain(last_call, "alibaba.com")
        results.append(
            await _call(
                session,
                "get_alibaba_product",
                {"product_id": alibaba_id},
                minimum_chars=100,
                semantic_check=_validate_alibaba_product(alibaba_id),
            )
        )

    results.append(
        await _call(
            session,
            "search_marketplace",
            {
                "query": "bicycle",
                "location": "Toronto, ON",
                "platforms": ["kijiji", "craigslist", "facebook"],
            },
            minimum_chars=100,
            semantic_check=_validate_marketplace,
        )
    )
    linkedin_search = await _call(
        session,
        "search_linkedin_jobs",
        {"keywords": "software engineer", "location": "Toronto, Ontario, Canada", "limit": 5},
        minimum_chars=100,
        semantic_check=_validate_linkedin_search,
    )
    results.append(linkedin_search)
    linkedin_job_id = _first_id(linkedin_search.text, (r"/jobs/view/(\d+)",))
    if not linkedin_search.passed or linkedin_job_id is None:
        results.append(
            Result("get_linkedin_job", False, "not called: no job ID from live search")
        )
    else:
        results.append(
            await _call(
                session,
                "get_linkedin_job",
                {"job_id": linkedin_job_id},
                minimum_chars=100,
                semantic_check=_validate_linkedin_job,
            )
        )

    results.append(
        await _call(
            session,
            "search_realtor",
            {"location": "Toronto", "page": 1},
            minimum_chars=100,
            semantic_check=_validate_realtor,
        )
    )
    return results


def _stdio_server_parameters() -> StdioServerParameters:
    """Return the exact server process configured for the stdio gate.

    By default the smoke test reuses the current interpreter.  Release tests
    can set ``SMOKE_STDIO_COMMAND`` to a JSON string array so the same MCP
    client can exercise a built container, for example a ``docker run -i``
    command.  A JSON array avoids shell parsing and keeps every argument
    explicit.
    """

    encoded = os.environ.get(_STDIO_COMMAND_ENV)
    if encoded is None:
        command = [sys.executable, "-m", "fetchaller.main"]
    else:
        try:
            command = json.loads(encoded)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{_STDIO_COMMAND_ENV} must be a JSON string array") from exc
        if (
            not isinstance(command, list)
            or not command
            or len(command) > 128
            or any(not isinstance(part, str) or not part or len(part) > 8192 or "\x00" in part for part in command)
        ):
            raise ValueError(f"{_STDIO_COMMAND_ENV} must be a non-empty bounded JSON string array")

    return StdioServerParameters(
        command=command[0],
        args=command[1:],
        env=dict(os.environ),
    )


async def main() -> int:
    results: list[Result] = []
    protocol_noise: list[str] = []

    class ProtocolNoiseHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if "Failed to parse JSONRPC message from server" in record.getMessage():
                protocol_noise.append(record.getMessage())

    noise_handler = ProtocolNoiseHandler()
    stdio_logger = logging.getLogger("mcp.client.stdio")
    stdio_logger.addHandler(noise_handler)
    # Reuse this test process's exact environment by default.  This makes the
    # smoke exercise the dependency set that launched it (including a
    # candidate wafer wheel) instead of recursively asking uv to resolve a
    # second, potentially different environment from the lockfile.
    parameters = _stdio_server_parameters()
    try:
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                results.extend(await run_live_tool_suite(session))
    finally:
        stdio_logger.removeHandler(noise_handler)

    results.append(
        Result(
            "stdio protocol cleanliness",
            not protocol_noise,
            ("no non-JSON stdout" if not protocol_noise else f"{len(protocol_noise)} malformed stdout line(s)"),
        )
    )

    print("\nLive MCP smoke results")
    for result in results:
        print(f"{'PASS' if result.passed else 'FAIL'} {result.name}: {result.detail}")
        if not result.passed and result.text:
            print("  " + result.text[:300].replace("\n", " | "))
    failed = [result for result in results if not result.passed]
    print(f"\n{len(results) - len(failed)}/{len(results)} gates passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
