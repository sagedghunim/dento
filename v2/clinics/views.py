"""Clinic configuration endpoints (design 6.2)."""

from django.utils.translation import gettext as _

from audit.services import record_change
from core.api import api
from core.days import business_day, local_iso
from core.errors import ApiError

from . import settings as clinic_settings
from .models import Clinic, Membership, Schedule, Service


def clinic_payload(ctx):
    clinic = ctx.clinic
    today = business_day(clinic)
    services = Service.objects.filter(clinic_id=clinic.id, is_active=True).order_by("sort", "id")
    doctors = Membership.objects.filter(clinic_id=clinic.id, is_active=True, role__in=["owner", "doctor"]).order_by("id")
    sessions = Schedule.objects.filter(clinic_id=clinic.id, weekday=today.weekday()).order_by("starts")
    hours = {}
    for session in sessions:
        hours.setdefault(session.doctor_id, []).append(
            {"starts": session.starts.strftime("%H:%M"), "ends": session.ends.strftime("%H:%M"), "mode": session.mode,
             "slot_minutes": session.slot_minutes})
    return {
        "clinic": {"id": clinic.id, "name": clinic.name, "timezone": clinic.timezone,
                   "day_ends_at": clinic.day_ends_at.strftime("%H:%M"), "business_day": today.isoformat()},
        "settings": clinic_settings.effective(clinic.settings),
        "services": [{"id": s.id, "name": s.name, "kind": s.kind, "price": s.price, "duration_min": s.duration_min,
                      "follow_up_service_id": s.follow_up_service_id} for s in services],
        "doctors": [{"id": d.id, "name": d.display_name, "today": hours.get(d.id, [])} for d in doctors],
        "payment_methods": ["cash", "card", "wallet", "instapay", "other"],
        "me": {"membership_id": ctx.membership_id, "role": ctx.membership.role, "codes": sorted(ctx.codes)},
    }


@api("GET")
def clinic_view(request, ctx, data):
    """Readable by any member: the Book dialog needs services, prices and doctors (design 6.2)."""
    return clinic_payload(ctx)


def _settings_replay(ctx, stored):
    return {"settings": clinic_settings.effective(Clinic.objects.get(id=ctx.clinic_id).settings)}


@api("PUT", perm="settings.manage", replay=_settings_replay)
def clinic_settings_view(request, ctx, data):
    changes = data.get("changes")
    expected = data.get("expected")
    if not isinstance(changes, dict) or not changes or not isinstance(expected, dict):
        raise ApiError("invalid", _("Send the changes and the values they replace."))
    clinic_settings.validated(changes)
    clinic = Clinic.objects.select_for_update(no_key=True).get(id=ctx.clinic_id)
    current = clinic_settings.effective(clinic.settings)
    stale = [key for key in changes if expected.get(key, current[key]) != current[key]]
    if stale:
        raise ApiError("stale", _("Someone changed these settings meanwhile. Reload and try again."), 409,
                       fields={key: "changed" for key in stale})
    before = {key: current[key] for key in changes}
    clinic.settings = {**(clinic.settings or {}), **changes}
    clinic.save(update_fields=["settings"])
    record_change(ctx, "settings.change", "clinic", clinic.id, before=before, after=changes)
    return {"settings": clinic_settings.effective(clinic.settings)}
