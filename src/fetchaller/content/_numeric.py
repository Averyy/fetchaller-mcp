"""Strict formatting guards for numeric metadata embedded in scraped pages."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation


def bounded_number_text(
    value: object,
    *,
    minimum: int | float,
    maximum: int | float,
    integral: bool = False,
    allow_grouping: bool = False,
    minimum_exclusive: bool = False,
    max_chars: int = 32,
) -> str:
    """Return bounded finite decimal text inside a semantic domain."""

    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    try:
        text = str(value).strip()
    except (OverflowError, ValueError):
        return ""
    if not text or len(text) > max_chars:
        return ""
    if allow_grouping:
        if re.fullmatch(r"(?:\d+|\d{1,3}(?:,\d{3})+)", text) is None:
            return ""
        numeric_text = text.replace(",", "")
    else:
        if re.fullmatch(r"\d+(?:\.\d+)?", text) is None:
            return ""
        numeric_text = text
    try:
        number = Decimal(numeric_text)
    except InvalidOperation:
        return ""
    if (
        not number.is_finite()
        or (
            number <= Decimal(str(minimum))
            if minimum_exclusive
            else number < Decimal(str(minimum))
        )
        or number > Decimal(str(maximum))
        or (integral and number != number.to_integral_value())
    ):
        return ""
    return text
