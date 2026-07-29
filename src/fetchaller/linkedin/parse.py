"""Extraction from LinkedIn's guest HTML fragments.

Selectors here were confirmed against live logged-out responses. Where the spec
work could not confirm something — a salary element, in particular — it is
absent rather than guessed at: emitting a field we never saw would be inventing
data, and a job search is exactly where that costs someone real time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup

_MAX_FIELD_CHARS = 300
_MAX_DESCRIPTION_CHARS = 20_000
_MAX_CRITERIA = 12
_MAX_BADGES = 4
_MAX_BADGE_CHARS = 40

# `urn:li:jobPosting:4445926062` -> 4445926062
_JOB_URN_RE = re.compile(r"urn:li:jobPosting:(\d+)")
# .../jobs/view/software-developer-at-vbk-4445926062?... -> 4445926062
_JOB_PATH_ID_RE = re.compile(r"/jobs/view/(?:[^/?#]*-)?(\d+)")


@dataclass
class JobCard:
    """One search result."""

    job_id: str = ""
    title: str = ""
    company: str = ""
    company_url: str = ""
    location: str = ""
    posted_label: str = ""
    posted_date: str = ""
    url: str = ""
    # "Be an early applicant", "Actively Hiring". Present on a large share of
    # cards (46 of 60 on one live query) and the single strongest ordering
    # signal a logged-out searcher gets — LinkedIn publishes no applicant count
    # or salary here.
    badges: tuple[str, ...] = ()


@dataclass
class JobDetail:
    """One posting's public detail."""

    job_id: str = ""
    title: str = ""
    company: str = ""
    company_url: str = ""
    location: str = ""
    posted_label: str = ""
    applicants: str = ""
    description: str = ""
    criteria: dict[str, str] = field(default_factory=dict)
    url: str = ""


# Employer-controlled text lands in markdown headings, bold runs and list
# items. A job title containing "[click here](http://evil)" or a stray "**"
# would otherwise render as an active link or reflow the document. Escaping the
# structural characters keeps the text readable and inert.
_MARKDOWN_ESCAPE = str.maketrans(
    {ch: "\\" + ch for ch in "\\`*_[]()#<>|"}
)


def _text(node, limit: int = _MAX_FIELD_CHARS) -> str:
    if node is None:
        return ""
    value = " ".join(node.get_text(" ", strip=True).split())
    return value[:limit].translate(_MARKDOWN_ESCAPE)


def _attr_text(node, attribute: str, limit: int) -> str:
    """Sanitise a raw attribute the same way element text is sanitised."""
    if node is None:
        return ""
    value = node.get(attribute) or ""
    if not isinstance(value, str):
        return ""
    cleaned = "".join(ch for ch in value if ch >= " " and ch != "\x7f")
    return " ".join(cleaned.split())[:limit].translate(_MARKDOWN_ESCAPE)


def strip_tracking(url: str) -> str:
    """Drop LinkedIn's per-response tracking query from a job or company URL.

    The returned hrefs carry position/pageNum/refId/trackingId, which change on
    every request. Keeping them would make otherwise-identical results compare
    unequal and leak our pagination state into the output.
    """
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https"):
        return ""
    # These are rendered as links, and every one we emit comes from a LinkedIn
    # card. Anything else is either markup we misread or content someone placed
    # to be followed; neither belongs in the output.
    host = (parts.hostname or "").rstrip(".").casefold()
    if not (host == "linkedin.com" or host.endswith(".linkedin.com")):
        return ""
    cleaned = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
        return ""
    return cleaned


def canonical_job_url(job_id: str) -> str:
    return f"https://www.linkedin.com/jobs/view/{job_id}" if job_id else ""


def _job_id_from(card, link_href: str) -> str:
    urn = card.get("data-entity-urn") if hasattr(card, "get") else None
    if isinstance(urn, str):
        match = _JOB_URN_RE.search(urn)
        if match:
            return match.group(1)
    match = _JOB_PATH_ID_RE.search(link_href or "")
    return match.group(1) if match else ""


def parse_search_fragment(html: str) -> list[JobCard]:
    """Job cards from a search fragment, in the order LinkedIn returned them."""
    if not html or not html.strip():
        return []
    soup = BeautifulSoup(html, "lxml")

    cards: list[JobCard] = []
    for container in soup.select("div.base-search-card, div.job-search-card"):
        link = container.select_one("a.base-card__full-link[href]")
        href = link.get("href", "") if link else ""
        job_id = _job_id_from(container, href)

        company_link = container.select_one("h4.base-search-card__subtitle a[href]")
        time_node = container.select_one(
            "time.job-search-card__listdate, time.job-search-card__listdate--new, time"
        )

        badges = []
        for badge in container.select(".job-posting-benefits__text"):
            label = _text(badge, _MAX_BADGE_CHARS)
            if label and label not in badges:
                badges.append(label)
            if len(badges) >= _MAX_BADGES:
                break

        card = JobCard(
            badges=tuple(badges),
            job_id=job_id,
            title=_text(container.select_one("h3.base-search-card__title")),
            company=_text(container.select_one("h4.base-search-card__subtitle")),
            company_url=strip_tracking(company_link.get("href", "")) if company_link else "",
            location=_text(container.select_one("span.job-search-card__location")),
            posted_label=_text(time_node, 60),
            # An attribute, so it never went through _text: sanitise it the
            # same way before it reaches markdown.
            posted_date=_attr_text(time_node, "datetime", 32),
            # Prefer the stable public URL; the returned href is tracking-laden
            # and host-varying (ca.linkedin.com, uk.linkedin.com, ...).
            url=canonical_job_url(job_id) or strip_tracking(href),
        )
        if card.title or card.job_id:
            cards.append(card)
    return cards


def parse_job_detail(html: str, job_id: str = "") -> JobDetail | None:
    """Public detail for one posting, or None if the fragment carries none."""
    if not html or not html.strip():
        return None
    soup = BeautifulSoup(html, "lxml")

    title = _text(soup.select_one("h2.top-card-layout__title, h1.top-card-layout__title"))
    if not title:
        return None

    topcard_link = soup.select_one('a[data-tracking-control-name="public_jobs_topcard-title"][href]')
    company_link = soup.select_one("a.topcard__org-name-link")
    company = _text(company_link)
    if not company:
        # Postings without a company page render a plain flavor span instead.
        for flavor in soup.select("span.topcard__flavor"):
            classes = flavor.get("class") or []
            if "topcard__flavor--bullet" not in classes:
                company = _text(flavor)
                break

    resolved_id = job_id or _job_id_from(None, topcard_link.get("href", "") if topcard_link else "")

    criteria: dict[str, str] = {}
    for item in soup.select("li.description__job-criteria-item")[:_MAX_CRITERIA]:
        key = _text(item.select_one("h3.description__job-criteria-subheader"), 80)
        value = _text(item.select_one("span.description__job-criteria-text"), 160)
        if key and value:
            criteria[key] = value

    return JobDetail(
        job_id=resolved_id,
        title=title,
        company=company,
        company_url=strip_tracking(company_link.get("href", "")) if company_link else "",
        location=_text(soup.select_one("span.topcard__flavor--bullet")),
        posted_label=_text(soup.select_one("span.posted-time-ago__text"), 60),
        applicants=_text(soup.select_one(".num-applicants__caption"), 60),
        description=_text(soup.select_one(".show-more-less-html__markup"), _MAX_DESCRIPTION_CHARS),
        criteria=criteria,
        url=canonical_job_url(resolved_id)
        or strip_tracking(topcard_link.get("href", "") if topcard_link else ""),
    )
