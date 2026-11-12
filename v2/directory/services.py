"""The clinic directory: which shard holds a clinic, and which clinics a person can pick (design 3.3)."""

import time

from django.conf import settings
from django.db.models import Q

from core.i18n import pick

from .models import DirectoryClinic, UserClinic

_CACHE_SECONDS = 60
_shard_cache = {}


def shard_for(clinic_id, *, fresh=False):
    """The shard alias of a clinic, cached for a minute per process.

    A stale entry is safe: the shard refuses a clinic it doesn't own (design 3.3),
    and the caller calls forget() and asks again.
    """
    now = time.monotonic()
    cached = _shard_cache.get(clinic_id)
    if cached and not fresh and now - cached[1] < _CACHE_SECONDS:
        return cached[0]
    row = DirectoryClinic.objects.filter(pk=clinic_id).values_list("shard", flat=True).first()
    if row is None or row not in settings.SHARDS:
        return None
    _shard_cache[clinic_id] = (row, now)
    return row


def forget(clinic_id):
    _shard_cache.pop(clinic_id, None)


def clinics_for_user(user_id):
    """Active clinics for the picker. The shard re-checks the membership when one is picked."""
    links = (
        UserClinic.objects.filter(user_id=user_id, is_active=True, clinic__status=DirectoryClinic.Status.ACTIVE)
        .select_related("clinic")
        .order_by("clinic__name", "clinic_id")
    )
    return [
        {"clinic_id": link.clinic_id, "membership_id": link.membership_id,
         "name": pick(link.clinic.name, link.clinic.name_en), "role": link.role,
         "kind": link.clinic.kind}
        for link in links
    ]


def record_facts(clinic_id, *, kind, timezone, day_ends_at, name=None):
    """Bring the directory's copy of a place's kind, time zone and day end in line with its shard (I6).

    Called after the shard commits a change, and by the sweep whenever the copies differ. Returns
    True when the copy changed.
    """
    changes = {"kind": kind, "timezone": timezone, "day_ends_at": day_ends_at}
    if name is not None:
        changes["name"] = name
    stale = Q()
    for field, value in changes.items():
        stale |= ~Q(**{field: value})
    return DirectoryClinic.objects.filter(Q(pk=clinic_id) & stale).update(**changes) == 1


# The last business day that ended at least 30 minutes before :now, in each place's own time zone (design 5.10).
_CLOSED_DAY = ("(((%(now)s::timestamptz - interval '30 minutes') AT TIME ZONE timezone) - day_ends_at::interval)::date - 1")
_DUE = {
    "sweep": "swept_through IS NULL OR swept_through < closed",
    # After the sweep has closed that day, so the recompute sees its final rows.
    "recompute": "swept_through >= closed AND (recomputed_through IS NULL OR recomputed_through < closed)",
}


def due_clinics(job, now, *, shard=None, after=0, limit=500):
    """Active places whose business day has ended and that `job` ("sweep" or "recompute") hasn't done yet.

    Returns up to `limit` dicts {id, shard, closed, kind, timezone, day_ends_at, name}, by id after `after`.
    One scan of the directory, with the day worked out in SQL from the copied time zone and day end.
    """
    from django.db import connections

    sql = (f"SELECT id, shard, closed, kind, timezone, day_ends_at, name FROM ("
           f"SELECT id, shard, name, kind, timezone, day_ends_at, swept_through, recomputed_through, {_CLOSED_DAY} AS closed "
           "FROM directory_clinic WHERE status = 'active' AND id > %(after)s"
           + (" AND shard = %(shard)s" if shard else "") +
           f") d WHERE {_DUE[job]} ORDER BY id LIMIT %(limit)s")
    with connections["default"].cursor() as cursor:
        cursor.execute(sql, {"now": now, "after": after, "shard": shard, "limit": limit})
        names = [column.name for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]


def mark_done(job, clinic_id, day):
    """Record that `job` has finished `day` for this place; never moves the mark back."""
    field = {"sweep": "swept_through", "recompute": "recomputed_through"}[job]
    DirectoryClinic.objects.filter(Q(pk=clinic_id) & (Q(**{f"{field}__isnull": True}) | Q(**{f"{field}__lt": day}))).update(
        **{field: day})


def least_full_shard():
    """New clinics go to the shard holding the fewest (a stand-in for size until M5 measures it)."""
    counts = {alias: 0 for alias in settings.SHARDS}
    for alias, count in _counts():
        if alias in counts:
            counts[alias] = count
    return min(counts, key=lambda alias: (counts[alias], settings.SHARDS[alias]))


def _counts():
    from django.db.models import Count

    return DirectoryClinic.objects.values_list("shard").annotate(n=Count("id")).values_list("shard", "n")
