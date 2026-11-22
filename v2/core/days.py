"""The business day (design 4.0): one server helper, used everywhere a `day` is computed.

Evening sessions run past midnight, so a time before the clinic's `day_ends_at`
(default 04:00) belongs to the previous calendar day. The browser never works
out what "today" is.

The drawer decides (Saged, 2026-11-22): while a register shift is open, the day is the one it opened on, however
late it gets, until someone closes it. A shift forgotten open holds the day back one day at most.
"""

import datetime as dt
from zoneinfo import ZoneInfo

from django.utils import timezone


def zone(clinic):
    return ZoneInfo(clinic.timezone)


def local_now(clinic, instant=None):
    return (instant or timezone.now()).astimezone(zone(clinic))


def _clock_day(clinic, instant=None):
    local = local_now(clinic, instant)
    if local.time() < clinic.day_ends_at:
        return local.date() - dt.timedelta(days=1)
    return local.date()


OPEN_SHIFT = "_open_shift_started"  # cached on the clinic object for the request


def _shift_started(clinic, instant):
    """When the register shift open at `instant` (now when None) was opened, or None. A clinic object that isn't a
    saved row (unit tests) has no shifts."""
    state = getattr(clinic, "_state", None)
    if getattr(clinic, "id", None) is None or state is None or state.db is None:
        return None
    if instant is None and OPEN_SHIFT in clinic.__dict__:
        return clinic.__dict__[OPEN_SHIFT]
    from django.db.models import Q

    from finance.models import Shift

    shifts = Shift.objects.using(state.db).filter(clinic_id=clinic.id)
    if instant is None:
        shifts = shifts.filter(status="open")
    else:
        shifts = shifts.filter(Q(closed_at__isnull=True) | Q(closed_at__gt=instant), opened_at__lte=instant)
    started = shifts.order_by("-opened_at").values_list("opened_at", flat=True).first()
    if instant is None:
        clinic.__dict__[OPEN_SHIFT] = started
    return started


def forget_shift(clinic):
    """After opening or closing a shift in this request: the day is worked out again."""
    clinic.__dict__.pop(OPEN_SHIFT, None)


def business_day(clinic, instant=None):
    day = _clock_day(clinic, instant)
    started = _shift_started(clinic, instant)
    if started is not None:
        opened_on = _clock_day(clinic, started)
        if day - dt.timedelta(days=1) <= opened_on < day:
            return opened_on
    return day


def next_rollover(clinic, instant=None):
    """The moment the current business day ends, in UTC."""
    day = business_day(clinic, instant)
    end_local = dt.datetime.combine(day + dt.timedelta(days=1), clinic.day_ends_at, tzinfo=zone(clinic))
    return end_local.astimezone(dt.UTC)


def local_iso(clinic, instant):
    """ISO 8601 with the clinic's offset, e.g. 2026-07-15T20:00:00+03:00 (design 6.1)."""
    return instant.astimezone(zone(clinic)).isoformat(timespec="seconds")
