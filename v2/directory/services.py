"""The clinic directory: which shard holds a clinic, and which clinics a person can pick (design 3.3)."""

import time

from django.conf import settings

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
        {"clinic_id": link.clinic_id, "membership_id": link.membership_id, "name": link.clinic.name, "role": link.role}
        for link in links
    ]


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
