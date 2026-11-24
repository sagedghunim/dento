"""The business day (design 4.0): one server helper, used everywhere a `day` is computed.

Evening sessions run past midnight, so a time before the clinic's `day_ends_at`
(default 04:00) belongs to the previous calendar day. The browser never works
out what "today" is.

A fixed time, whatever the drawer (Saged, 2026-11-24; replaces "the drawer decides" of 2026-11-22): a register left
open held one place on yesterday. Whoever is still waiting or in the room when the day ends moves to the new day
(scheduling.services.carry_over).
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


def business_day(clinic, instant=None):
    return _clock_day(clinic, instant)


def next_rollover(clinic, instant=None):
    """The moment the current business day ends, in UTC."""
    day = business_day(clinic, instant)
    end_local = dt.datetime.combine(day + dt.timedelta(days=1), clinic.day_ends_at, tzinfo=zone(clinic))
    return end_local.astimezone(dt.UTC)


def local_iso(clinic, instant):
    """ISO 8601 with the clinic's offset, e.g. 2026-07-15T20:00:00+03:00 (design 6.1)."""
    return instant.astimezone(zone(clinic)).isoformat(timespec="seconds")
