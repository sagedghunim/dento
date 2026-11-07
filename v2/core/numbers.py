"""One number parser for amounts, quantities, phones, ages and days (design 6.1, 7.2).

The same rules run in `web/static/js/numbers.js`. Arabic keyboards type
Arabic-Indic digits, which `int()` and the browser's Number() don't all accept.
"""

import re

from core.errors import ApiError
from core.text import ascii_digits

_DECIMAL_MARKS = str.maketrans({"٫": ".", ",": "."})
# "1,500" is ambiguous (one and a half, or fifteen hundred), so it is refused.
_THOUSANDS_LOOKALIKE = re.compile(r"^\d+,\d{3}$")


def parse_int(value, field, low, high):
    if isinstance(value, bool):
        raise ApiError("invalid", f"{field} must be a whole number", fields={field: "whole_number"})
    if isinstance(value, str):
        text = ascii_digits(value).strip()
        if not re.fullmatch(r"-?\d+", text):
            raise ApiError("invalid", f"{field} must be a whole number", fields={field: "whole_number"})
        value = int(text)
    if not isinstance(value, int):
        raise ApiError("invalid", f"{field} must be a whole number", fields={field: "whole_number"})
    if not low <= value <= high:
        raise ApiError("invalid", f"{field} must be between {low} and {high}", fields={field: "range"})
    return value


def parse_money_text(text, field, high):
    """Typed money ("150", "150.5", "١٥٠٫٥") to integer piastres, by string arithmetic."""
    raw = ascii_digits(str(text)).strip()
    if _THOUSANDS_LOOKALIKE.match(raw):
        raise ApiError("invalid", f"{field}: write the amount without a thousands separator", fields={field: "ambiguous"})
    raw = raw.translate(_DECIMAL_MARKS)
    match = re.fullmatch(r"(\d+)(?:\.(\d{1,2}))?", raw)
    if not match:
        raise ApiError("invalid", f"{field} must be an amount", fields={field: "amount"})
    pounds, fraction = match.group(1), (match.group(2) or "").ljust(2, "0")
    piastres = int(pounds) * 100 + int(fraction or "0")
    if piastres > high:
        raise ApiError("invalid", f"{field} is too large", fields={field: "range"})
    return piastres


def parse_money(value, field, low=0, high=100_000_000_00):
    """Money in JSON is integer piastres; typed text is accepted too."""
    if isinstance(value, str):
        value = parse_money_text(value, field, high)
    return parse_int(value, field, low, high)
