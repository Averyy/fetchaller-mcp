"""Storefront display-locale cookies: which currency and country a shop prices in.

Amazon, AliExpress and Alibaba choose the currency, and the latter two the
ship-to country and its VAT, from the visitor's IP. Behind a VPN that exits in
Frankfurt, a UK user was quoted euros with German VAT by all three
(2026-10-31). The cookies below are the sites' own display preferences, the
ones their currency pickers set, and each is sent only to the site it belongs
to. They change what a page is priced in, never whether it is served.

- Amazon: every marketplace is read in its own currency (``i18n-prefs``), the
  currency it charges in, whatever the IP. amazon.co.uk answered a Frankfurt
  exit EUR91.30 without it and £75.71, the UK price, with it (2026-10-31);
  amazon.com answered a Canadian IP CAD115.32 without it and $82.49 with it
  (2026-11-02). The converted figure is Amazon's currency-converter estimate,
  not the listing's price.
- AliExpress (``aep_usuc_f``) and Alibaba (``sc_g_cfg_f``): pinned only when
  ``SHOPPING_COUNTRY`` (ISO 3166-1 alpha-2) and ``SHOPPING_CURRENCY``
  (ISO 4217) are both set. Unset, each site keeps choosing for itself.
"""

from __future__ import annotations

import os
import re
import sys
from urllib.parse import urlparse

from .content.amazon import MARKETPLACE_CURRENCIES, amazon_marketplace

_warned_invalid = False


def shopping_locale() -> tuple[str, str] | None:
    """Return the configured ``(country, currency)``, or None when unset or invalid."""

    global _warned_invalid
    country = os.environ.get("SHOPPING_COUNTRY", "").strip().upper()
    currency = os.environ.get("SHOPPING_CURRENCY", "").strip().upper()
    if not country and not currency:
        return None
    if re.fullmatch(r"[A-Z]{2}", country) and re.fullmatch(r"[A-Z]{3}", currency):
        return country, currency
    if not _warned_invalid:
        _warned_invalid = True
        print(
            "shopping locale: SHOPPING_COUNTRY must be two letters and "
            "SHOPPING_CURRENCY three, both set; ignoring them",
            file=sys.stderr,
        )
    return None


def amazon_currency(url: str) -> str | None:
    """The currency an Amazon marketplace prices in, from its domain."""

    marketplace = amazon_marketplace(url)
    return MARKETPLACE_CURRENCIES[marketplace] if marketplace is not None else None


def locale_cookies(url: str) -> list[str]:
    """``Set-Cookie`` values pinning the display locale of the shop at ``url``."""

    marketplace = amazon_marketplace(url)
    if marketplace is not None:
        currency = MARKETPLACE_CURRENCIES[marketplace]
        return [f"i18n-prefs={currency}; Domain=amazon.{marketplace}; Path=/"]

    locale = shopping_locale()
    if locale is None:
        return []
    country, currency = locale
    host = (urlparse(url).hostname or "").lower()
    if host == "aliexpress.com" or host.endswith(".aliexpress.com"):
        return [
            f"aep_usuc_f=site=glo&c_tp={currency}&region={country}&b_locale=en_US; "
            "Domain=.aliexpress.com; Path=/"
        ]
    if host == "alibaba.com" or host.endswith(".alibaba.com"):
        return [
            f"sc_g_cfg_f=sc_b_site={country}&sc_b_locale=en_US&sc_b_currency={currency}; "
            "Domain=alibaba.com; Path=/; Secure"
        ]
    return []
