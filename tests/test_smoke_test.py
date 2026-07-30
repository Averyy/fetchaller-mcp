"""Semantic gates for the live MCP smoke test."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from scripts import smoke_test
from scripts.smoke_test import (
    _BLOCKED_RESPONSE,
    EXPECTED_TOOLS,
    Result,
    _has_real_price,
    _stdio_server_parameters,
    _validate_alibaba_product,
    _validate_alibaba_search,
    _validate_aliexpress_product,
    _validate_aliexpress_search,
    _validate_browse_reddit,
    _validate_fetch,
    _validate_marketplace,
    _validate_realtor,
    _validate_search_reddit,
    _validate_web_search,
    run_live_tool_suite,
)


def test_stdio_gate_defaults_to_current_interpreter(monkeypatch) -> None:
    monkeypatch.delenv("SMOKE_STDIO_COMMAND", raising=False)

    parameters = _stdio_server_parameters()

    assert parameters.command == smoke_test.sys.executable
    assert parameters.args == ["-m", "fetchaller.main"]


def test_stdio_gate_accepts_an_explicit_container_command(monkeypatch) -> None:
    monkeypatch.setenv(
        "SMOKE_STDIO_COMMAND",
        '["docker","run","--rm","-i","fetchaller-mcp:test"]',
    )

    parameters = _stdio_server_parameters()

    assert parameters.command == "docker"
    assert parameters.args == [
        "run",
        "--rm",
        "-i",
        "fetchaller-mcp:test",
    ]


@pytest.mark.parametrize(
    "encoded",
    [
        "{",
        "{}",
        "[]",
        '[""]',
        '["docker", 1]',
        '["docker", "bad\\u0000argument"]',
    ],
)
def test_stdio_gate_rejects_invalid_commands(monkeypatch, encoded) -> None:
    monkeypatch.setenv("SMOKE_STDIO_COMMAND", encoded)

    with pytest.raises(ValueError, match="JSON string array"):
        _stdio_server_parameters()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Price: $12.50", True),
        ("Price: $0.00", False),
        ("Price: unavailable", False),
        ("Price: $0.00-$1.20", True),
        ("Price: Minimum order 100 pieces", False),
        ("Price: 100", False),
        ("Price: USD 12.50", True),
        ("Price: $1.43-2.49 per pieces", True),
        ("Price: $1.43 per piece", True),
        ("Price: $1.43 per minimum order 100 pieces", False),
    ],
)
def test_price_gate_requires_a_positive_amount(value: str, expected: bool) -> None:
    assert _has_real_price(value, "Price:") is expected


def test_basic_tool_semantic_gates_accept_complete_results() -> None:
    assert _validate_fetch("# Example Domain\n\nExample content") is None
    assert (
        _validate_browse_reddit(
            "r/Python · hot · 3 posts\n\n1. A post\n   https://www.reddit.com/r/Python/comments/abc123/\n"
        )
        is None
    )
    assert (
        _validate_search_reddit(
            'Search: "asyncio" in r/Python · relevance · all · 3 results\n\n'
            "1. A post\n"
            "   https://www.reddit.com/r/Python/comments/abc123/\n"
        )
        is None
    )
    assert (
        _validate_web_search(
            'Search: "Python asyncio documentation" | google: 1 | 1 total\n\n'
            "1. asyncio\n"
            "   https://docs.python.org/3/library/asyncio.html\n"
        )
        is None
    )


def test_commerce_semantic_gates_accept_complete_results() -> None:
    aliexpress_id = "1005006727707575"
    assert (
        _validate_aliexpress_search(
            'Search: "usb c cable" | page 1 | 10 results\n\n'
            "1. Cable\n"
            "   Price: US $9.99\n"
            f"   https://www.aliexpress.com/item/{aliexpress_id}.html\n"
        )
        is None
    )
    assert (
        _validate_aliexpress_product(aliexpress_id)(
            "Cable title\n"
            f"https://www.aliexpress.com/item/{aliexpress_id}.html\n\n"
            "Price: US $9.99\n"
            "Store: Cable Store\n"
        )
        is None
    )

    alibaba_id = "1600486391522"
    assert (
        _validate_alibaba_search(
            'Search: "usb c cable" | page 1 | 10 results\n\n'
            "1. Cable\n"
            "   Price: US$0.50\n"
            "   https://www.alibaba.com/product-detail/cable_1600486391522.html\n"
        )
        is None
    )
    assert (
        _validate_alibaba_product(alibaba_id)(
            "# Cable\n\n"
            "**Price:** $1.43-2.49 per pieces\n"
            "**Supplier:** Example Supplier\n"
            "**Specifications:**\n"
            "- material: copper\n\n"
            f"https://www.alibaba.com/product-detail/_{alibaba_id}.html\n"
        )
        is None
    )


def test_aliexpress_reviews_only_is_not_a_product_success() -> None:
    validate = _validate_aliexpress_product("1005006727707575")
    reviews_only = "Rating: ★4.9 (100 reviews)\n\nRecent reviews:\n1. Great cable.\n"
    assert validate(reviews_only) is not None


def test_aliexpress_labeled_search_snapshot_is_a_product_success() -> None:
    aliexpress_id = "1005006727707575"
    validate = _validate_aliexpress_product(aliexpress_id)

    assert (
        validate(
            "Braided USB C Cable\n"
            f"https://www.aliexpress.com/item/{aliexpress_id}.html\n\n"
            "Price: US $9.99\n"
            "★4.8 | 5000+ sold\n"
            "Source: verified AliExpress search listing snapshot; "
            "full product modules were unavailable.\n"
        )
        is None
    )


def test_commerce_gates_reject_placeholder_or_empty_core_data() -> None:
    aliexpress_id = "1005006727707575"
    assert (
        _validate_aliexpress_product(aliexpress_id)(
            "Unknown Product\n"
            f"https://www.aliexpress.com/item/{aliexpress_id}.html\n"
            "Price: unavailable\n"
            "Specifications:\n"
        )
        is not None
    )
    assert (
        _validate_aliexpress_search(
            'Search: "usb c cable" | page 1 | 10 results\n'
            "Price: unavailable\n"
            f"https://www.aliexpress.com/item/{aliexpress_id}.html\n"
        )
        is not None
    )
    assert (
        _validate_aliexpress_product(aliexpress_id)(
            f"{aliexpress_id}\n"
            f"https://www.aliexpress.com/item/{aliexpress_id}.html\n"
            "Price: US $9.99\n"
            "Store: Cable Store\n"
        )
        is not None
    )
    # A title and price are a common challenge-shell shape.  The product tool
    # and the independent live smoke gate must agree that an actual detail
    # module is still required.
    assert (
        _validate_aliexpress_product(aliexpress_id)(
            f"Cable title\nhttps://www.aliexpress.com/item/{aliexpress_id}.html\nPrice: US $9.99\n"
        )
        is not None
    )
    # Shipping and SKU price maps can occur on a challenge/page shell and do
    # not satisfy the product parser's substantive-detail contract.
    for shell_detail in (
        "Shipping: Free delivery\n",
        "SKU Pricing:\n  SKU 1: US $9.99\n",
    ):
        assert (
            _validate_aliexpress_product(aliexpress_id)(
                f"Cable title\nhttps://www.aliexpress.com/item/{aliexpress_id}.html\nPrice: US $9.99\n{shell_detail}"
            )
            is not None
        )

    alibaba_id = "1600486391522"
    assert (
        _validate_alibaba_product(alibaba_id)(
            "# Product\n"
            "**Price:** unknown\n"
            "**Supplier:** Example Supplier\n"
            "**Specifications:**\n"
            f"https://www.alibaba.com/product-detail/_{alibaba_id}.html\n"
        )
        is not None
    )
    assert (
        _validate_alibaba_product(alibaba_id)(
            f"# {alibaba_id}\n"
            "**Price:** $0.50\n"
            "**Supplier:** Example Supplier\n"
            "**Specifications:**\n"
            "- material: copper\n"
            f"https://www.alibaba.com/product-detail/_{alibaba_id}.html\n"
        )
        is not None
    )
    assert (
        _validate_alibaba_search(
            'Search: "usb c cable" | page 1 | 10 results\n'
            "Price: N/A\n"
            "https://www.alibaba.com/product-detail/cable_1600486391522.html\n"
        )
        is not None
    )


def test_marketplace_gate_requires_multiple_sources_and_explicit_errors() -> None:
    complete = (
        '# Marketplace Search: "bicycle" | Toronto, ON\n\n'
        "## Kijiji\n"
        "https://www.kijiji.ca/v-bikes/example/1\n\n"
        "## Craigslist\n"
        "https://toronto.craigslist.org/tor/bik/d/example/1.html\n\n"
        "## Facebook Marketplace\n"
        "https://www.facebook.com/marketplace/item/1/\n"
    )
    assert _validate_marketplace(complete) is None
    assert _validate_marketplace(complete.replace("## Craigslist", "## Missing")) is not None
    partial = (
        '# Marketplace Search: "bicycle" | Toronto, ON\n\n'
        "## Kijiji\n"
        "https://www.kijiji.ca/v-bikes/example/1\n\n"
        "## Craigslist\n"
        "https://toronto.craigslist.org/tor/bik/d/example/1.html\n\n"
        "---\n"
        "**Errors:**\n\n"
        "- Facebook Marketplace: request denied\n"
    )
    assert _validate_marketplace(partial) is None
    assert _validate_marketplace(partial.replace("- Facebook", "- Missing")) is not None
    one_source = partial.replace(
        "## Craigslist\nhttps://toronto.craigslist.org/tor/bik/d/example/1.html",
        "- Craigslist: request denied",
    )
    assert _validate_marketplace(one_source) is not None


def test_realtor_gate_requires_priced_listing_and_url() -> None:
    complete = (
        "**10,801 listings for sale** in Toronto, ON, Canada\n\n"
        "1. **$549,900** — Example Street\n"
        "   https://www.realtor.ca/real-estate/30080910/example\n"
    )
    assert _validate_realtor(complete) is None
    assert _validate_realtor(complete.replace("https://", "about:")) is not None


def test_semantic_gates_reject_empty_or_error_shaped_text() -> None:
    validators = [
        _validate_fetch,
        _validate_browse_reddit,
        _validate_search_reddit,
        _validate_web_search,
        _validate_aliexpress_search,
        _validate_alibaba_search,
        _validate_marketplace,
        _validate_realtor,
    ]
    for validate in validators:
        assert validate("An upstream call returned some long but irrelevant text.") is not None


@pytest.mark.parametrize(
    "text",
    [
        "You've been blocked by network security",
        "Access Denied",
        "Verify that you are a human",
        "CAPTCHA verification",
        "Browser solve timed out",
    ],
)
def test_blocked_response_gate_recognizes_live_failure_shapes(text) -> None:
    assert _BLOCKED_RESPONSE.search(text)


@pytest.mark.asyncio
async def test_shared_live_suite_calls_every_tool_and_uses_search_ids() -> None:
    class FakeSession:
        async def initialize(self):
            return SimpleNamespace(
                serverInfo=SimpleNamespace(
                    name="fetchaller",
                    version=smoke_test.__version__,
                )
            )

        async def list_tools(self):
            return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in EXPECTED_TOOLS])

    calls: list[tuple[str, dict]] = []

    async def fake_call(session, name, arguments, **kwargs):
        del session, kwargs
        calls.append((name, arguments))
        if name == "search_aliexpress":
            text = "https://www.aliexpress.com/item/1005001234567890.html"
        elif name == "search_alibaba":
            text = "https://www.alibaba.com/product-detail/cable_1600123456789.html"
        elif name == "search_linkedin_jobs":
            text = "https://www.linkedin.com/jobs/view/4445926062"
        else:
            text = "semantic response"
        return Result(name, True, "ok", text)

    with (
        patch("scripts.smoke_test._call", side_effect=fake_call),
        patch(
            "scripts.smoke_test._pace_domain",
            new_callable=AsyncMock,
        ) as pace,
    ):
        results = await run_live_tool_suite(FakeSession())

    assert [name for name, _ in calls] == [
        "fetch",
        "browse_reddit",
        "search_reddit",
        "search",
        "search_aliexpress",
        "get_aliexpress_product",
        "search_alibaba",
        "get_alibaba_product",
        "search_marketplace",
        "search_linkedin_jobs",
        "get_linkedin_job",
        "search_realtor",
    ]
    assert len(results) == len(EXPECTED_TOOLS) + 1
    assert calls[5] == (
        "get_aliexpress_product",
        {"product_id": "1005001234567890"},
    )
    assert calls[10] == ("get_linkedin_job", {"job_id": "4445926062"})
    assert calls[7] == (
        "get_alibaba_product",
        {"product_id": "1600123456789"},
    )
    assert pace.await_count == 4


@pytest.mark.asyncio
async def test_shared_live_suite_does_not_guess_product_ids_after_failed_search() -> None:
    class FakeSession:
        async def initialize(self):
            return SimpleNamespace(
                serverInfo=SimpleNamespace(
                    name="fetchaller",
                    version=smoke_test.__version__,
                )
            )

        async def list_tools(self):
            return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in EXPECTED_TOOLS])

    calls: list[str] = []

    async def fake_call(session, name, arguments, **kwargs):
        del session, arguments, kwargs
        calls.append(name)
        if name in {"search_aliexpress", "search_alibaba"}:
            return Result(name, False, "blocked", "Access Denied")
        return Result(name, True, "ok", "semantic response")

    with (
        patch("scripts.smoke_test._call", side_effect=fake_call),
        patch("scripts.smoke_test._pace_domain", new_callable=AsyncMock),
    ):
        results = await run_live_tool_suite(FakeSession())

    assert "get_aliexpress_product" not in calls
    assert "get_alibaba_product" not in calls
    by_name = {result.name: result for result in results}
    assert not by_name["get_aliexpress_product"].passed
    assert "not called" in by_name["get_aliexpress_product"].detail
    assert not by_name["get_alibaba_product"].passed
    assert "not called" in by_name["get_alibaba_product"].detail
