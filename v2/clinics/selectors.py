"""Reading a place's configuration: its sections, their services and doctors, and sessions (design D1, 6.2)."""

from django.utils.translation import get_language
from django.utils.translation import gettext as _

from core import sql
from core.errors import ApiError
from core.i18n import pick

from .models import Schedule, Section, Service
from .specialties import SPECIALTIES

SCHEDULE_LIMIT = 1000  # a place's whole week: doctors x 7 days x a few sessions


def section_not_found():
    return ApiError("not_found", _("This section wasn't found."), 404)


def specialty_label(code):
    names = SPECIALTIES.get(code, {}).get("name", {})
    return names.get(get_language() or "ar", names.get("en", code))


def section_payload(section):
    return {"id": section.id, "name": pick(section.name, section.name_en), "name_ar": section.name,
            "name_en": section.name_en, "specialty": section.specialty_code,
            "specialty_name": specialty_label(section.specialty_code), "sort": section.sort}


def service_payload(ctx, service):
    """One service; its price only for holders of money.view (Saged, 2026-11-16)."""
    from billing import visibility

    return {"id": service.id, "section_id": service.section_id, "name": pick(service.name, service.name_en),
            "name_ar": service.name, "name_en": service.name_en, "kind": service.kind,
            "price": service.price if visibility.sees(ctx) else None, "duration_min": service.duration_min,
            "follow_up_service_id": service.follow_up_service_id}


def session_payload(session):
    return {"id": session.id, "doctor_id": session.doctor_id, "section_id": session.section_id,
            "weekday": session.weekday, "starts": session.starts.strftime("%H:%M"), "ends": session.ends.strftime("%H:%M"),
            "mode": session.mode, "slot_minutes": session.slot_minutes, "max_bookings": session.max_bookings}


def sections(ctx):
    """The place's active sections, in their order. A clinic has exactly one. A limited assistant sees theirs (G1)."""
    from scheduling.rules import section_scope

    scope = section_scope(ctx)
    return [section_payload(s) for s in Section.objects.filter(clinic_id=ctx.clinic_id, is_active=True).order_by("sort", "id")
            if scope is None or s.id in scope]


def active_section(ctx, section_id):
    from scheduling.rules import section_scope

    scope = section_scope(ctx)
    section = Section.objects.filter(clinic_id=ctx.clinic_id, id=section_id, is_active=True).first()
    if section is None or (scope is not None and section.id not in scope):
        raise section_not_found()
    return section


def section_services(ctx, section_id):
    """The section's active services and prices, for the Book dialog after the section is picked."""
    section = active_section(ctx, section_id)
    services = Service.objects.filter(clinic_id=ctx.clinic_id, section_id=section.id, is_active=True).order_by("sort", "id")
    return {"section": section_payload(section), "items": [service_payload(ctx, s) for s in services]}


def section_doctors(ctx, section_id):
    """The doctors with a session in this section, with those sessions (a doctor's sections are their sessions')."""
    section = active_section(ctx, section_id)
    doctors = {}
    for doctor_id, name, title, weekday, starts, ends, mode, minutes, cap, name_en in sql.rows(ctx, """
            SELECT m.id, m.display_name, m.title, s.weekday, s.starts, s.ends, s.mode, s.slot_minutes, s.max_bookings,
                   m.display_name_en
              FROM "clinics_schedule" s
              JOIN "clinics_membership" m ON m.clinic_id = s.clinic_id AND m.id = s.doctor_id
             WHERE s.clinic_id = %s AND s.section_id = %s AND m.is_active AND m.role IN ('owner', 'doctor')
             ORDER BY m.id, s.weekday, s.starts""", [ctx.clinic_id, section.id]):
        doctor = doctors.setdefault(doctor_id, {"id": doctor_id, "name": pick(name, name_en), "title": title,
                                                "sessions": []})
        doctor["sessions"].append({"weekday": weekday, "starts": starts.strftime("%H:%M"), "ends": ends.strftime("%H:%M"),
                                   "mode": mode, "slot_minutes": minutes, "max_bookings": cap})
    return {"section": section_payload(section), "items": list(doctors.values())}


def schedules(ctx, doctor_id=None, section_id=None):
    """Every session of the place, optionally of one doctor or one section."""
    rows = Schedule.objects.filter(clinic_id=ctx.clinic_id)
    if doctor_id is not None:
        rows = rows.filter(doctor_id=doctor_id)
    if section_id is not None:
        rows = rows.filter(section_id=section_id)
    return {"items": [session_payload(s) for s in rows.order_by("doctor_id", "weekday", "starts", "id")[:SCHEDULE_LIMIT]]}


def schedule(ctx, schedule_id):
    found = Schedule.objects.filter(clinic_id=ctx.clinic_id, id=schedule_id).first()
    return session_payload(found) if found else None
