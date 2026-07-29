"""Strict helpers for validating price fields from untrusted SSR payloads."""

from __future__ import annotations

import math
import re

_MAX_PRICE_CHARS = 256
_CURRENCY_SYMBOLS = "$€£¥₩₹₽₺₫฿₱"
_CURRENCY_CODES = frozenset(
    {
        "aed",
        "aud",
        "brl",
        "cad",
        "chf",
        "cny",
        "dkk",
        "eur",
        "gbp",
        "hkd",
        "idr",
        "inr",
        "jpy",
        "krw",
        "mxn",
        "myr",
        "nok",
        "nzd",
        "php",
        "pln",
        "rmb",
        "rub",
        "sar",
        "sek",
        "sgd",
        "thb",
        "try",
        "twd",
        "usd",
        "vnd",
        "zar",
    }
)
_ALLOWED_PRICE_WORDS = _CURRENCY_CODES | {
    "from",
    "lot",
    "lots",
    "pack",
    "packs",
    "per",
    "piece",
    "pieces",
    "set",
    "sets",
    "unit",
    "units",
    "us",
}
_AMOUNT_RE = re.compile(r"(?<![\d.,])[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
_BARE_AMOUNT_RE = re.compile(r"[+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
_EXPONENT_RE = re.compile(r"\d\s*[eE][+-]?\d")
_REGIONAL_DOLLAR_PREFIX_RE = re.compile(
    r"(?<!\S)(?:AU|CA|HK|MX|NZ|SG|US|A|C|NT|R|S)\s*(?=\$)",
    re.IGNORECASE,
)


def _positive_amounts(value: str) -> bool:
    """Whether a bounded string contains at least one finite positive amount."""

    matches = list(_AMOUNT_RE.finditer(value))
    if not matches:
        return False
    positive = False
    for index, match in enumerate(matches):
        amount = match.group(0)
        # A currency symbol may sit between a leading minus sign and the
        # amount (``-$1``), so the sign is not always part of the match.
        if amount.startswith("-") or (index == 0 and "-" in value[: match.start()]):
            return False
        parsed = float(amount.replace(",", ""))
        if not math.isfinite(parsed):
            return False
        positive = positive or parsed > 0
    return positive


def has_positive_price(
    value: object,
    *,
    require_currency: bool,
) -> bool:
    """Validate a numeric raw price or a currency-marked formatted price.

    A digit embedded in prose such as ``Minimum order 100 pieces`` is not a
    price. Formatted fields therefore need a recognized currency symbol/code
    and may contain only currency words in addition to their numeric amount.
    Raw price fields may be a finite positive number or a bare decimal string.
    """

    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return not require_currency and value > 0 and len(str(value)) <= _MAX_PRICE_CHARS
    if isinstance(value, float):
        return not require_currency and math.isfinite(value) and value > 0
    if not isinstance(value, str) or len(value) > _MAX_PRICE_CHARS:
        return False

    text = " ".join(value.split())
    if not text or _EXPONENT_RE.search(text):
        return False
    if not require_currency and _BARE_AMOUNT_RE.fullmatch(text):
        return _positive_amounts(text)

    # Live marketplaces commonly render a dollar with a regional shorthand
    # (``C$ 6.91``, ``AU $12``) instead of an ISO code. Strip only a bounded
    # allowlist immediately adjacent to ``$`` before enforcing the prose
    # allowlist; arbitrary words and lookalike prefixes still fail closed.
    currency_normalized = _REGIONAL_DOLLAR_PREFIX_RE.sub("", text)
    words = {word.lower() for word in re.findall(r"[A-Za-z]+", currency_normalized)}
    has_symbol = any(symbol in text for symbol in _CURRENCY_SYMBOLS)
    has_code = bool(words & _CURRENCY_CODES)
    if not (has_symbol or has_code):
        return False
    if not words <= _ALLOWED_PRICE_WORDS:
        return False
    return _positive_amounts(text)
