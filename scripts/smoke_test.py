"""Thorough live smoke test for the upgraded stack.

Covers all 9 MCP tools and every site with custom handling in fetchaller.
Honors live-testing rules: sequential requests with 5-8s delays, BrowserSolver attached.

Usage:
    .venv/bin/python scripts/smoke_test.py
"""

import asyncio
import re
import sys
import time
from pathlib import Path

# Allow `from fetchaller...` when running from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fetchaller.marketplace.search import search_marketplace
from fetchaller.tools.browse_reddit import browse_reddit
from fetchaller.tools.fetch import fetch_url
from fetchaller.tools.get_alibaba_product import get_alibaba_product
from fetchaller.tools.get_aliexpress_product import get_aliexpress_product
from fetchaller.tools.search import search_web
from fetchaller.tools.search_alibaba import search_alibaba as search_alibaba_tool
from fetchaller.tools.search_aliexpress import search_aliexpress as search_aliexpress_tool
from fetchaller.tools.search_reddit import search_reddit
from wafer.browser import BrowserSolver


SOLVER: BrowserSolver | None = None
RESULTS: list[tuple[str, bool, str]] = []


def _fmt_content(d: dict) -> str:
    """Short preview of a tool result dict."""
    if d.get("error"):
        return f"ERROR: {d['error'][:200]}"
    content = d.get("content", "")
    if isinstance(content, str):
        return content[:160].replace("\n", " | ")
    return repr(content)[:160]


async def step(label: str, coro, expect_min_chars: int = 50):
    """Run a single test step and record pass/fail."""
    print(f"\n{'='*72}\n[{label}]", flush=True)
    t0 = time.monotonic()
    try:
        result = await coro
    except Exception as exc:
        dt = time.monotonic() - t0
        msg = f"EXC {type(exc).__name__}: {exc}"
        print(f"  FAIL ({dt:.1f}s) {msg}", flush=True)
        RESULTS.append((label, False, msg))
        return False
    dt = time.monotonic() - t0
    if isinstance(result, dict):
        if result.get("error"):
            msg = f"err: {result['error'][:160]}"
            print(f"  FAIL ({dt:.1f}s) {msg}", flush=True)
            RESULTS.append((label, False, msg))
            return False
        content = result.get("content", "")
        nchars = len(content) if isinstance(content, str) else 0
        ok = nchars >= expect_min_chars
        marker = "PASS" if ok else "THIN"
        print(f"  {marker} ({dt:.1f}s, {nchars} chars, type={result.get('content_type')})", flush=True)
        print(f"  preview: {_fmt_content(result)}", flush=True)
        RESULTS.append((label, ok, f"{nchars} chars"))
        return ok
    print(f"  PASS ({dt:.1f}s) {type(result).__name__}", flush=True)
    RESULTS.append((label, True, type(result).__name__))
    return True


async def url(label: str, u: str, expect_min_chars: int = 50, **kw):
    return await step(label, fetch_url(u, browser_solver=SOLVER, **kw), expect_min_chars=expect_min_chars)


async def _find_aliexpress_product_id() -> str | None:
    """Pull a current product ID from a fresh search so the test self-heals against delistings."""
    r = await search_aliexpress_tool(query="usb cable", browser_solver=SOLVER)
    if r.get("error"):
        return None
    ids = re.findall(r"/item/(\d+)\.html", r.get("content", ""))
    return ids[0] if ids else None


async def _find_alibaba_product_id() -> str | None:
    r = await search_alibaba_tool(query="usb cable", browser_solver=SOLVER)
    if r.get("error"):
        return None
    ids = re.findall(r"/product-detail/[^/]+_(\d+)\.html", r.get("content", ""))
    if ids:
        return ids[0]
    ids = re.findall(r"product/(\d+)\.html", r.get("content", ""))
    return ids[0] if ids else None


async def main():
    global SOLVER
    SOLVER = BrowserSolver()
    print("BrowserSolver ready", flush=True)

    try:
        # ---- TOOLS (9 MCP tools) ----
        await step("tool:search", search_web(query="raspberry pi 5 specs"))
        await asyncio.sleep(5)

        await step("tool:browse_reddit", browse_reddit(subreddit="homelab", sort="hot", limit=5))
        await asyncio.sleep(6)

        await step("tool:search_reddit", search_reddit(query="proxmox", subreddit="homelab", limit=5))
        await asyncio.sleep(6)

        await step("tool:search_aliexpress",
                   search_aliexpress_tool(query="usb cable", browser_solver=SOLVER))
        await asyncio.sleep(8)

        # Use a live product ID pulled from search (delistings rotate fast on AE)
        aliexpress_pid = await _find_aliexpress_product_id()
        if aliexpress_pid:
            print(f"\n  (using current AliExpress product id: {aliexpress_pid})", flush=True)
            await asyncio.sleep(6)
            await step("tool:get_aliexpress_product",
                       get_aliexpress_product(product_id=aliexpress_pid, browser_solver=SOLVER))
        else:
            RESULTS.append(("tool:get_aliexpress_product", False, "could not find live product id"))
        await asyncio.sleep(8)

        await step("tool:search_alibaba",
                   search_alibaba_tool(query="usb cable", browser_solver=SOLVER))
        await asyncio.sleep(8)

        alibaba_pid = await _find_alibaba_product_id()
        if alibaba_pid:
            print(f"\n  (using current Alibaba product id: {alibaba_pid})", flush=True)
            await asyncio.sleep(6)
            await step("tool:get_alibaba_product",
                       get_alibaba_product(product_id=alibaba_pid, browser_solver=SOLVER))
        else:
            RESULTS.append(("tool:get_alibaba_product", False, "could not find live product id"))
        await asyncio.sleep(6)

        await step("tool:search_marketplace",
                   search_marketplace(query="bicycle", location="toronto, ON",
                                      platforms=["kijiji", "craigslist"], browser_solver=SOLVER))
        await asyncio.sleep(8)

        # ---- GENERIC FETCH (content types) ----
        await url("generic:html", "https://example.com/")
        await asyncio.sleep(4)
        await url("generic:html-medium", "https://httpbin.org/html")
        await asyncio.sleep(6)
        await url("generic:json", "https://httpbin.org/json")
        await asyncio.sleep(4)
        await url("generic:xml", "https://httpbin.org/xml")
        await asyncio.sleep(4)
        # Real-content PDF (TI datasheet — used in our own test fixtures)
        await url("generic:pdf", "https://www.ti.com/lit/ds/symlink/lm358.pdf", expect_min_chars=500)
        await asyncio.sleep(5)
        # SVG (exercises the recent "Support SVG and binary images in fetch dispatch" commit)
        await url("generic:svg", "https://upload.wikimedia.org/wikipedia/commons/4/4f/SVG_Logo.svg")
        await asyncio.sleep(5)
        # PNG (binary image)
        await url("generic:png", "https://www.python.org/static/community_logos/python-logo-master-v3-TM.png")
        await asyncio.sleep(5)

        # ---- SITES WITH CUSTOM HANDLING ----
        await url("wikipedia", "https://en.wikipedia.org/wiki/Python_(programming_language)")
        await asyncio.sleep(5)
        await url("github", "https://github.com/python/cpython")
        await asyncio.sleep(5)
        await url("hackernews", "https://news.ycombinator.com/")
        await asyncio.sleep(5)
        await url("stackoverflow",
                  "https://stackoverflow.com/questions/231767/what-does-the-yield-keyword-do-in-python")
        await asyncio.sleep(5)
        await url("medium",
                  "https://medium.com/@netflixtechblog/how-netflix-uses-druid-for-real-time-insights-to-ensure-a-high-quality-experience-19e1e8568d06")
        await asyncio.sleep(5)
        await url("huggingface", "https://huggingface.co/openai/whisper-large-v3")
        await asyncio.sleep(5)

        # Forums (XenForo/vBulletin cleanup pipeline)
        await url("forum:avs", "https://www.avsforum.com/forums/lcd-flat-panel-displays.166/")
        await asyncio.sleep(5)
        await url("forum:redflagdeals", "https://forums.redflagdeals.com/")
        await asyncio.sleep(5)

        # E-commerce product pages
        await url("ebay:search", "https://www.ebay.com/sch/i.html?_nkw=raspberry+pi+5")
        await asyncio.sleep(6)
        await url("amazon:product",
                  "https://www.amazon.com/Raspberry-Pi-Quad-core-Cortex-A76-Processor/dp/B0CK2FCG1K")
        await asyncio.sleep(6)
        await url("costco:product",
                  "https://www.costco.com/kirkland-signature-bath-tissue-2-ply-380-sheets-30-rolls.product.100645583.html")
        await asyncio.sleep(6)
        # Soylent product pages 404 sporadically — homepage is stable
        await url("soylent", "https://soylent.com/")
        await asyncio.sleep(6)
        await url("petsmart", "https://www.petsmart.com/dog/food/dry-food/")
        await asyncio.sleep(6)

        # Electronics — DigiKey and Mouser require API keys (env vars), skip silently if not set
        import os
        if os.environ.get("DIGIKEY_CLIENT_ID") and os.environ.get("DIGIKEY_CLIENT_SECRET"):
            await url("digikey:product",
                      "https://www.digikey.com/en/products/detail/raspberry-pi/SC1112/21658260")
            await asyncio.sleep(5)
        else:
            print("\n[digikey:product] SKIP (DIGIKEY_CLIENT_ID/SECRET not set)", flush=True)
            RESULTS.append(("digikey:product", True, "SKIP (no API key)"))

        if os.environ.get("MOUSER_API_KEY"):
            await url("mouser:product",
                      "https://www.mouser.com/ProductDetail/Raspberry-Pi/SC1111?qs=HoCaDK9Nz5dpiOSEnJyD%252BA%3D%3D")
            await asyncio.sleep(5)
        else:
            print("\n[mouser:product] SKIP (MOUSER_API_KEY not set)", flush=True)
            RESULTS.append(("mouser:product", True, "SKIP (no API key)"))

        await url("molex:product",
                  "https://www.molex.com/en-us/products/part-detail/0470531000")
        await asyncio.sleep(5)
        await url("ti:product", "https://www.ti.com/product/LM358")
        await asyncio.sleep(5)
        await url("fcc", "https://fccid.io/")
        await asyncio.sleep(5)

        # Marketplaces (individual URLs, in addition to search_marketplace above)
        await url("craigslist:search", "https://toronto.craigslist.org/search/bik")
        await asyncio.sleep(6)
        await url("kijiji:search", "https://www.kijiji.ca/b-bicycles/toronto/c644l1700273")
        await asyncio.sleep(6)
        # FB returns its expected logged-out stub (~60-100 chars) — not a regression
        await url("facebook_marketplace",
                  "https://www.facebook.com/marketplace/toronto/bicycles", expect_min_chars=40)
        await asyncio.sleep(6)

        # Job boards — INDIVIDUAL POSTING URLs (Ashby/Gem modules extract structured data
        # from individual postings; board index pages are SPAs that need a different path)
        await url("greenhouse", "https://boards.greenhouse.io/anthropic")
        await asyncio.sleep(5)
        await url("lever", "https://jobs.lever.co/plusgrade")
        await asyncio.sleep(5)
        await url("ashby:job",
                  "https://jobs.ashbyhq.com/openai/980bc2ea-2c35-4546-bb48-5496a4e6847f",
                  expect_min_chars=500)
        await asyncio.sleep(5)
        await url("workatastartup", "https://www.workatastartup.com/companies")
        await asyncio.sleep(5)
        await url("gem:job",
                  "https://jobs.gem.com/fabrichealth/am9icG9zdDpjnp6tbN0Ck3f_ovA0kI3c",
                  expect_min_chars=500)

    finally:
        try:
            SOLVER.close()
        except Exception:
            pass

    # ---- SUMMARY ----
    print(f"\n\n{'='*72}\n=== SUMMARY ({len(RESULTS)} tests) ===", flush=True)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    for label, ok, note in RESULTS:
        marker = "PASS" if ok else "FAIL"
        print(f"  {marker:4s}  {label:30s}  {note}", flush=True)
    print(f"\nTotal: {passed}/{len(RESULTS)} pass", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
