"""Content fetching and processing utilities.

Module layout:
- html.py       — Generic HTML→markdown pipeline (universal junk selectors,
                   markdownify, whitespace cleanup). Dispatches to site modules.
- amazon.py    — Amazon: CSS selectors for sponsored products, buy box noise,
                   quick-view overlay, footer; soup cleanup for form inputs,
                   translate/report links; regex post-processing for tracking
                   URLs, feedback blocks, footer sections. All Amazon TLDs.
- github.py     — GitHub: CSS selectors, soup cleanup, regex post-processing,
                   URL transforms, file tree extraction.
- reddit.py     — Reddit: CSS selectors, URL transforms, post formatting.
- hackernews.py — Hacker News: CSS selectors, soup cleanup, story reformatter.
- medium.py     — Medium: CSS selectors, source param stripping, post-article
                   block removal, footer cleanup. HTML-based detection for
                   unknown custom domains.
- huggingface.py — Hugging Face: data-target CSS selectors, filter tag/button
                   cleanup, DatasetViewer removal, gated model license stripping.
- stackoverflow.py — Stack Overflow / Stack Exchange: CSS selectors for
                   sidebars, vote buttons, post menus, user signatures; soup
                   cleanup for avatars, banners, Collectives promo; regex
                   post-processing for badges, dates, comments headers.
- wikipedia.py  — Wikipedia: CSS selectors for edit buttons, navboxes, etc.
- fetcher.py    — HTTP fetching via curl_cffi.
- pdf.py        — PDF to markdown extraction via pymupdf4llm.
- url.py        — URL validation and SSRF protection.

Each site module follows the same interface:
- is_<site>(url) → bool           — URL detection
- SELECTORS_LIST: list[str]       — CSS selectors to remove before conversion
- strip_<site>_junk(soup)         — BeautifulSoup-level cleanup (optional)
- postprocess_<site>(markdown)    — Regex post-processing on output (optional)
"""

from .fetcher import ContentFetcher, FetchResult, RetryConfig
from .html import clean_html, html_to_markdown
from .pdf import PdfResult, extract_pdf
from .reddit import RedditTransformResult, format_reddit_post, format_relative_time, transform_reddit_url
from .url import normalize_url

__all__ = [
    "ContentFetcher",
    "FetchResult",
    "PdfResult",
    "RedditTransformResult",
    "RetryConfig",
    "clean_html",
    "extract_pdf",
    "format_reddit_post",
    "format_relative_time",
    "html_to_markdown",
    "normalize_url",
    "transform_reddit_url",
]
