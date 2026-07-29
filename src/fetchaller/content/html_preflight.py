"""Bounded site-specific inspection performed before generic HTML rendering."""

from __future__ import annotations

from dataclasses import dataclass

from bs4 import BeautifulSoup

from .aliexpress import extract_search_products
from .ashby import extract_ashby_embed_slug_from_html
from .bamboohr import extract_bamboohr_embed_tenant
from .dayforce import extract_dayforce_canonical_board_url
from .forums import (
    discover_feed_url,
    is_discourse_html,
    is_forum_html,
)
from .github import extract_github_file_listing, extract_github_issue
from .greenhouse import (
    extract_greenhouse_params_from_html,
    is_greenhouse_html,
)
from .jazzhr import extract_jazzhr_embed_tenants

_MAX_PREFLIGHT_TEXT_CHARS = 4 * 1024 * 1024
_MAX_JAZZHR_TENANTS = 32


@dataclass(frozen=True)
class HtmlPreflight:
    """Picklable, bounded metadata needed by the async fetch orchestrator."""

    greenhouse_detected: bool = False
    greenhouse_params: tuple[str, str] | None = None
    dayforce_url: str | None = None
    ashby_slug: str | None = None
    bamboohr_tenant: str | None = None
    jazzhr_tenants: tuple[str, ...] = ()
    feed_url: str | None = None
    aliexpress_search: str | None = None
    github_issue: str | None = None
    github_file_listing: str | None = None


def _bounded(value: str | None) -> str | None:
    if value is None or len(value) <= _MAX_PREFLIGHT_TEXT_CHARS:
        return value
    marker = "\n\n[Structured extraction truncated at the safe processing limit]"
    return value[: _MAX_PREFLIGHT_TEXT_CHARS - len(marker)].rstrip() + marker


def inspect_html_preflight(
    html: str,
    page_url: str,
    known_forum_listing: bool,
    generic_forum_candidate: bool,
    is_aliexpress_search: bool,
    is_github: bool,
) -> HtmlPreflight:
    """Inspect untrusted markup inside a disposable parser process."""

    greenhouse_detected = False
    greenhouse_params = None
    if "grnhse" in html or "greenhouse.io/embed" in html:
        soup = BeautifulSoup(html, "lxml")
        greenhouse_detected = is_greenhouse_html(soup)
        if greenhouse_detected:
            greenhouse_params = extract_greenhouse_params_from_html(
                soup,
                page_url=page_url,
            )

    dayforce_url = extract_dayforce_canonical_board_url(html)
    ashby_slug = extract_ashby_embed_slug_from_html(html)
    bamboohr_tenant = extract_bamboohr_embed_tenant(html)
    jazzhr_tenants = tuple(extract_jazzhr_embed_tenants(html)[:_MAX_JAZZHR_TENANTS])

    feed_url = None
    if known_forum_listing:
        feed_url = discover_feed_url(html, page_url)
    elif generic_forum_candidate:
        quick_soup = BeautifulSoup(html[:4096], "lxml")
        if is_forum_html(quick_soup) or is_discourse_html(quick_soup):
            feed_url = discover_feed_url(html, page_url)

    aliexpress_search = None
    if is_aliexpress_search:
        aliexpress_search = _bounded(extract_search_products(html, page_url))

    github_issue = None
    github_file_listing = None
    if is_github:
        github_issue = _bounded(extract_github_issue(html, page_url))
        github_file_listing = _bounded(extract_github_file_listing(html, page_url))

    return HtmlPreflight(
        greenhouse_detected=greenhouse_detected,
        greenhouse_params=greenhouse_params,
        dayforce_url=dayforce_url,
        ashby_slug=ashby_slug,
        bamboohr_tenant=bamboohr_tenant,
        jazzhr_tenants=jazzhr_tenants,
        feed_url=feed_url,
        aliexpress_search=aliexpress_search,
        github_issue=github_issue,
        github_file_listing=github_file_listing,
    )
