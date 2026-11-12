"""Clinic configuration endpoints (design 6.2): the configuration read, settings, sections and sessions."""

import datetime as dt
import zoneinfo

from django.utils.translation import gettext as _

from audit.services import record_change
from core import validate
from core.api import Result, api, by_method
from core.days import business_day, local_iso
from core.errors import ApiError
from core.numbers import parse_int

from . import selectors, services
from . import settings as clinic_settings
from .models import Clinic, Membership, Schedule, Service

DAY_SETTINGS = ("timezone", "day_ends_at")


def clinic_payload(ctx):
    from billing.models import METHODS

    clinic = ctx.clinic
    today = business_day(clinic)
    services = Service.objects.filter(clinic_id=clinic.id, is_active=True).order_by("sort", "id")
    doctors = Membership.objects.filter(clinic_id=clinic.id, is_active=True, role__in=["owner", "doctor"]).order_by("id")
    sessions = Schedule.objects.filter(clinic_id=clinic.id, weekday=today.weekday()).order_by("starts")
    hours = {}
    for session in sessions:
        hours.setdefault(session.doctor_id, []).append(
            {"starts": session.starts.strftime("%H:%M"), "ends": session.ends.strftime("%H:%M"), "mode": session.mode,
             "slot_minutes": session.slot_minutes, "section_id": session.section_id})
    return {
        "clinic": {"id": clinic.id, "name": clinic.name, "kind": clinic.kind, "timezone": clinic.timezone,
                   "day_ends_at": clinic.day_ends_at.strftime("%H:%M"), "business_day": today.isoformat()},
        "sections": selectors.sections(ctx),
        "settings": clinic_settings.effective(clinic.settings),
        "services": [{"id": s.id, "section_id": s.section_id, "name": s.name, "kind": s.kind, "price": s.price,
                      "duration_min": s.duration_min, "follow_up_service_id": s.follow_up_service_id} for s in services],
        "doctors": [{"id": d.id, "name": d.display_name, "today": hours.get(d.id, [])} for d in doctors],
        "payment_methods": list(METHODS),  # transfer added for the finish dialog (design E3)
        "me": {"membership_id": ctx.membership_id, "role": ctx.membership.role, "codes": sorted(ctx.codes)},
    }


@api("GET")
def clinic_view(request, ctx, data):
    """Readable by any member: the Book dialog needs services, prices and doctors (design 6.2)."""
    return clinic_payload(ctx)


def _settings_body(clinic):
    """The settings registry's values, plus the time zone and day end, which live in their own columns."""
    return {"settings": {**clinic_settings.effective(clinic.settings), "timezone": clinic.timezone,
                         "day_ends_at": clinic.day_ends_at.strftime("%H:%M")}}


def _settings_replay(ctx, stored):
    return _settings_body(Clinic.objects.get(id=ctx.clinic_id))


def _clean_day_settings(changes):
    """The time zone (an IANA name) and the day end ("HH:MM", from 00:00 to 06:00)."""
    cleaned = {}
    if "timezone" in changes:
        name = changes["timezone"]
        try:
            if not isinstance(name, str) or not name or len(name) > 60:
                raise ValueError
            zoneinfo.ZoneInfo(name)
        except (ValueError, zoneinfo.ZoneInfoNotFoundError):
            raise ApiError("invalid", _("Pick a time zone from the list."), fields={"timezone": "choice"})
        cleaned["timezone"] = name
    if "day_ends_at" in changes:
        try:
            value = dt.time.fromisoformat(changes["day_ends_at"]) if isinstance(changes["day_ends_at"], str) else None
        except ValueError:
            value = None
        if value is None or value.second or value.microsecond or value > dt.time(6, 0):
            raise ApiError("invalid", _("The day can end between 00:00 and 06:00."), fields={"day_ends_at": "range"})
        cleaned["day_ends_at"] = value.strftime("%H:%M")
    return cleaned


@api("PUT", perm="settings.manage", replay=_settings_replay)
def clinic_settings_view(request, ctx, data):
    """Absolute values with the values they replace (design 5.0). The time zone and day end also go to the directory."""
    changes = data.get("changes")
    expected = data.get("expected")
    if not isinstance(changes, dict) or not changes or not isinstance(expected, dict):
        raise ApiError("invalid", _("Send the changes and the values they replace."))
    day_changes = _clean_day_settings({key: changes[key] for key in DAY_SETTINGS if key in changes})
    changes = {key: value for key, value in changes.items() if key not in DAY_SETTINGS}
    clinic_settings.validated(changes)
    clinic = Clinic.objects.select_for_update(no_key=True).get(id=ctx.clinic_id)
    current = {**clinic_settings.effective(clinic.settings), "timezone": clinic.timezone,
               "day_ends_at": clinic.day_ends_at.strftime("%H:%M")}
    stale = [key for key in [*changes, *day_changes] if expected.get(key, current[key]) != current[key]]
    if stale:
        raise ApiError("stale", _("Someone changed these settings meanwhile. Reload and try again."), 409,
                       fields={key: "changed" for key in stale})
    before = {key: current[key] for key in [*changes, *day_changes]}
    if day_changes:
        services.change_day_settings(ctx, clinic, day_changes)
    if changes:
        clinic.settings = {**(clinic.settings or {}), **changes}
        clinic.save(update_fields=["settings"])
    after = {**changes, **day_changes}
    if after != before:
        record_change(ctx, "settings.change", "clinic", clinic.id, before=before, after=after)
    return _settings_body(clinic)


# --- Sections (design D1): readable by anyone who can book ------------------------------------------------------


def _section_id(value):
    return validate.path_id(value, selectors.section_not_found())


@api("GET", perm="appointments.book")
def sections_view(request, ctx, data):
    return {"kind": ctx.clinic.kind, "items": selectors.sections(ctx)}


@api("GET", perm="appointments.book")
def section_services_view(request, ctx, data, section_id):
    return selectors.section_services(ctx, _section_id(section_id))


@api("GET", perm="appointments.book")
def section_doctors_view(request, ctx, data, section_id):
    return selectors.section_doctors(ctx, _section_id(section_id))


# --- Sessions (design 5.1, D2), guarded by settings.manage --------------------------------------------------------

SESSION_FIELDS = ("doctor_id", "section_id", "weekday", "starts", "ends", "mode", "slot_minutes", "max_bookings")


def _hhmm(data, name):
    value = data.get(name)
    try:
        parsed = dt.time.fromisoformat(value) if isinstance(value, str) and len(value.strip()) == 5 else None
    except ValueError:
        parsed = None
    if parsed is None:
        raise ApiError("invalid", f"{name} must be a time (HH:MM)", fields={name: "time"})
    return parsed


def clean_session(data):
    """A weekly session from a request body: doctor, section, weekday (Monday is 0), HH:MM times, the booking mode."""
    session = {
        "doctor_id": validate.ident(data.get("doctor_id"), "doctor_id"),
        "section_id": validate.ident(data.get("section_id"), "section_id"),
        "weekday": parse_int(data.get("weekday"), "weekday", 0, 6),
        "starts": _hhmm(data, "starts"),
        "ends": _hhmm(data, "ends"),
        "mode": validate.choice(data, "mode", ["queue", "slots"]),
        "slot_minutes": None,
        "max_bookings": None,
    }
    if session["starts"] == session["ends"]:
        raise ApiError("invalid", _("A session must end after it starts."), fields={"ends": "range"})
    if session["mode"] == "slots":
        session["slot_minutes"] = parse_int(data.get("slot_minutes"), "slot_minutes", 5, 120)
    elif data.get("slot_minutes") not in (None, ""):
        raise ApiError("invalid", "slot_minutes is only for slot sessions", fields={"slot_minutes": "queue"})
    if data.get("max_bookings") not in (None, ""):
        session["max_bookings"] = parse_int(data.get("max_bookings"), "max_bookings", 1, 500)
    return session


def _clean_expected(data):
    """The session as the screen saw it, in the shape GET answers (only the keys sent are compared)."""
    expected = data.get("expected")
    if not isinstance(expected, dict) or not expected:
        raise ApiError("invalid", _("Send the changes and the values they replace."), fields={"expected": "required"})
    unknown = set(expected) - {"id", *SESSION_FIELDS}
    if unknown:
        raise ApiError("invalid", "Unknown fields: " + ", ".join(sorted(unknown)), fields={"expected": "unknown"})
    return expected


def _schedule_id(value):
    return validate.path_id(value, services.not_found("session"))


def _today_version(ctx):
    """Today's version as it stands (a replay reads it; it never bumps it)."""
    from summaries.selectors import clinic_day

    return clinic_day(ctx, business_day(ctx.clinic))["version"]


def _replay_schedule(ctx, stored):
    return {"schedule": selectors.schedule(ctx, stored["target_id"]), "version": _today_version(ctx)}


@api("GET", perm="settings.manage")
def schedules_view(request, ctx, data):
    doctor, section = request.GET.get("doctor"), request.GET.get("section")
    return selectors.schedules(ctx, validate.ident(doctor, "doctor") if doctor else None,
                               validate.ident(section, "section") if section else None)


@api("POST", perm="settings.manage", replay=_replay_schedule)
def schedule_create_view(request, ctx, data):
    schedule, version = services.create_session(ctx, clean_session(data))
    return Result({"schedule": schedule, "version": version}, status=201, target_type="schedule",
                  target_id=schedule["id"])


@api("PUT", perm="settings.manage", replay=_replay_schedule)
def schedule_update_view(request, ctx, data, schedule_id):
    schedule_id = _schedule_id(schedule_id)
    schedule, version = services.update_session(ctx, schedule_id, clean_session(data), _clean_expected(data))
    return Result({"schedule": schedule, "version": version}, target_type="schedule", target_id=schedule_id)


def _replay_delete(ctx, stored):
    return {"deleted": stored["target_id"], "version": _today_version(ctx)}


@api("DELETE", perm="settings.manage", replay=_replay_delete)
def schedule_delete_view(request, ctx, data, schedule_id):
    schedule_id = _schedule_id(schedule_id)
    version = services.delete_session(ctx, schedule_id)
    return Result({"deleted": schedule_id, "version": version}, target_type="schedule", target_id=schedule_id)


schedules_endpoint = by_method(GET=schedules_view, POST=schedule_create_view)
schedule_endpoint = by_method(PUT=schedule_update_view, DELETE=schedule_delete_view)
