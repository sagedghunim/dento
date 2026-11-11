"""Keyset lists (design 6.1): ?limit= (at most 100) and ?after=<cursor>; {"items": [...], "next": ...}."""

import datetime as dt

from core.errors import ApiError
from core.numbers import parse_int


def limit(request, default=20):
    raw = request.GET.get("limit")
    return default if raw in (None, "") else parse_int(raw, "limit", 1, 100)


def _bad_cursor():
    return ApiError("invalid", "after must be a cursor from a previous page", fields={"after": "cursor"})


def _cursor_id(raw):
    """An id inside a cursor: a bigint, so the database never sees a value it can't hold."""
    value = int(raw)
    if not 0 <= value <= 2**63 - 1:
        raise ValueError(raw)
    return value


def day_cursor(request):
    """A cursor over (day DESC, id DESC): 'YYYY-MM-DD.id'."""
    raw = request.GET.get("after")
    if not raw:
        return None
    try:
        day, ident = raw.split(".", 1)
        return dt.date.fromisoformat(day), _cursor_id(ident)
    except ValueError:
        raise _bad_cursor()


def time_cursor(request):
    """A cursor over (timestamp DESC, id DESC): '<microseconds since the epoch>.id'."""
    raw = request.GET.get("after")
    if not raw:
        return None
    try:
        micros, ident = raw.split(".", 1)
        return dt.datetime(1970, 1, 1, tzinfo=dt.UTC) + dt.timedelta(microseconds=int(micros)), _cursor_id(ident)
    except (ValueError, OverflowError):
        raise _bad_cursor()


def encode_day(day, ident):
    return f"{day.isoformat()}.{ident}"


def encode_time(instant, ident):
    micros = (instant - dt.datetime(1970, 1, 1, tzinfo=dt.UTC)) // dt.timedelta(microseconds=1)
    return f"{micros}.{ident}"


def page(items, size, cursor_of):
    """items holds up to size + 1 rows; the extra one only says there is a next page."""
    more = len(items) > size
    items = items[:size]
    return {"items": items, "next": cursor_of(items[-1]) if more and items else None}
