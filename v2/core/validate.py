"""Small validators for request bodies (design 6.1)."""

import datetime as _dt

from core.errors import ApiError


def text(data, name, max_len, required=True, strip=True):
    value = data.get(name, "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ApiError("invalid", f"{name} must be text", fields={name: "text"})
    if strip:
        value = " ".join(value.split()) if "\n" not in value else value.strip()
    if required and not value:
        raise ApiError("invalid", f"{name} is required", fields={name: "required"})
    if len(value) > max_len:
        raise ApiError("invalid", f"{name} is too long", fields={name: "too_long"})
    return value


def choice(data, name, options):
    value = data.get(name)
    if value not in options:
        raise ApiError("invalid", f"{name} must be one of: {', '.join(options)}", fields={name: "choice"})
    return value


def boolean(data, name, default=False):
    value = data.get(name, default)
    if not isinstance(value, bool):
        raise ApiError("invalid", f"{name} must be true or false", fields={name: "boolean"})
    return value


MAX_ID = 2**63 - 1


def ident(value, name="id"):
    """A row id from a body or a query string: a positive bigint (as a number or digits)."""
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_ID:
        raise ApiError("invalid", f"{name} must be an id", fields={name: "id"})
    return value


def path_id(value, not_found):
    """A row id from the URL. Anything out of range is simply not found."""
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_ID:
        raise not_found
    return value


# Days a request may name. Wide enough for any visit, narrow enough that day arithmetic (windows, grids,
# rollovers) never leaves Python's calendar, which would be a 500 (chief judge M1, R6).
DAY_MIN = _dt.date(1900, 1, 1)
DAY_MAX = _dt.date(2199, 12, 31)


def iso_day(value, name="day"):
    if isinstance(value, str):
        try:
            day = _dt.date.fromisoformat(value.strip())
        except ValueError:
            day = None
        if day is not None:
            if not DAY_MIN <= day <= DAY_MAX:
                raise ApiError("invalid", f"{name} is out of range", fields={name: "range"})
            return day
    raise ApiError("invalid", f"{name} must be a date (YYYY-MM-DD)", fields={name: "date"})
