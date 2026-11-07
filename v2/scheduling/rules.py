"""Booking rules: who works when, the slot grid, the fee and the follow-up rule (design 5.1, R4.1)."""

import datetime as dt
from dataclasses import dataclass, field

from django.utils.translation import gettext as _

from clinics import settings as clinic_settings
from core import sql
from core.days import business_day, zone
from core.errors import ApiError

BOOKING_HORIZON_DAYS = 366


def not_found(what):
    messages = {
        "appointment": _("This appointment wasn't found."),
        "doctor": _("This doctor wasn't found."),
        "service": _("This service wasn't found."),
        "patient": _("This patient wasn't found."),
    }
    return ApiError("not_found", messages[what], 404)


@dataclass
class Session:
    starts: dt.time
    ends: dt.time
    mode: str
    slot_minutes: int | None
    max_bookings: int | None


@dataclass
class DoctorDay:
    """A doctor's sessions on one business day."""

    doctor_id: int
    name: str
    sessions: list = field(default_factory=list)

    @property
    def has_queue(self):
        return any(s.mode == "queue" for s in self.sessions)

    @property
    def cap(self):
        """max_bookings for the day: the sum of the sessions' limits, or none if any session has none."""
        if not self.sessions or any(s.max_bookings is None for s in self.sessions):
            return None
        return sum(s.max_bookings for s in self.sessions)


@dataclass
class ServiceInfo:
    id: int
    name: str
    kind: str
    price: int
    follow_up_price: int | None


def check_day(clinic, day):
    today = business_day(clinic)
    if not today <= day <= today + dt.timedelta(days=BOOKING_HORIZON_DAYS):
        raise ApiError("invalid", _("Pick a day from today up to a year ahead."), fields={"day": "range"})
    return today


def _session_rows_sql(with_service):
    service = ("sv.id, sv.name, sv.kind, sv.price, sv.is_active, fu.price, fu.is_active, " if with_service else "")
    joins = ('LEFT JOIN "clinics_service" sv ON sv.clinic_id = x.clinic_id AND sv.id = %s '
             'LEFT JOIN "clinics_service" fu ON fu.clinic_id = sv.clinic_id AND fu.id = sv.follow_up_service_id '
             if with_service else "")
    return (f"SELECT {service}m.id, m.role, m.is_active, m.display_name, s.starts, s.ends, s.mode, s.slot_minutes, "
            "s.max_bookings FROM (SELECT %s::bigint AS clinic_id) x "
            f"{joins}"
            'LEFT JOIN "clinics_membership" m ON m.clinic_id = x.clinic_id AND m.id = %s '
            'LEFT JOIN "clinics_schedule" s ON s.clinic_id = m.clinic_id AND s.doctor_id = m.id AND s.weekday = %s '
            "WHERE x.clinic_id = %s ORDER BY s.starts")


def _doctor_from(rows, offset):
    first = rows[0]
    doctor_id, role, active, name = first[offset:offset + 4]
    if doctor_id is None or not active or role not in ("owner", "doctor"):
        raise not_found("doctor")
    doctor = DoctorDay(doctor_id, name)
    for r in rows:
        if r[offset + 4] is not None:
            doctor.sessions.append(Session(*r[offset + 4:offset + 9]))
    return doctor


def doctor_day(ctx, doctor_id, day):
    """One statement: the doctor (active owner or doctor of this clinic) and their sessions that weekday."""
    rows = sql.rows(ctx, _session_rows_sql(False), [ctx.clinic_id, doctor_id, day.weekday(), ctx.clinic_id])
    return _doctor_from(rows, 0)


def doctor_day_and_service(ctx, doctor_id, service_id, day):
    """One statement for booking and quotes: the service with its follow-up price, the doctor and their sessions."""
    rows = sql.rows(ctx, _session_rows_sql(True),
                    [ctx.clinic_id, service_id, doctor_id, day.weekday(), ctx.clinic_id])
    sid, name, kind, price, active, follow_price, follow_active = rows[0][:7]
    if sid is None:
        raise not_found("service")
    if not active:
        raise ApiError("invalid", _("This service isn't offered any more."), fields={"service_id": "inactive"})
    service = ServiceInfo(sid, name, kind, price, follow_price if follow_active else None)
    return _doctor_from(rows, 7), service


def require_working(doctor):
    if not doctor.sessions:
        raise ApiError("doctor_off", _("This doctor doesn't work on that day."), 409, fields={"day": "doctor_off"})


def grid(clinic, day, doctor):
    """Every slot of the doctor's slot sessions that business day, as (starts, ends) in UTC.

    A session that starts before the clinic's day end (after midnight) is on the next calendar
    day. Slots step in real minutes, so a clock change inside a session can't make a slot twice.
    """
    tz = zone(clinic)
    slots = []
    for session in doctor.sessions:
        if session.mode != "slots" or not session.slot_minutes:
            continue
        start_date = day if session.starts >= clinic.day_ends_at else day + dt.timedelta(days=1)
        end_date = start_date if session.ends > session.starts else start_date + dt.timedelta(days=1)
        start = dt.datetime.combine(start_date, session.starts, tzinfo=tz).astimezone(dt.UTC)
        end = dt.datetime.combine(end_date, session.ends, tzinfo=tz).astimezone(dt.UTC)
        step = dt.timedelta(minutes=session.slot_minutes)
        at = start
        while at + step <= end:
            slots.append((at, at + step))
            at += step
    return sorted(set(slots))


def parse_instant(value, name="starts_at"):
    if not isinstance(value, str):
        raise ApiError("invalid", f"{name} must be a time with its offset", fields={name: "datetime"})
    try:
        instant = dt.datetime.fromisoformat(value.strip())
    except ValueError:
        raise ApiError("invalid", f"{name} must be a time with its offset", fields={name: "datetime"})
    if instant.tzinfo is None:
        raise ApiError("invalid", f"{name} must be a time with its offset", fields={name: "datetime"})
    return instant.astimezone(dt.UTC)


def resolve_slot(clinic, day, doctor, starts_at):
    """(starts, ends) for a slot booking, or (None, None) for a queue booking."""
    require_working(doctor)
    if starts_at is None:
        if not doctor.has_queue:
            raise ApiError("time_required", _("Pick a time for this doctor."), fields={"starts_at": "required"})
        return None, None
    for start, end in grid(clinic, day, doctor):
        if start == starts_at:
            return start, end
    raise ApiError("off_grid", _("Pick one of the doctor's times."), fields={"starts_at": "off_grid"})


def follow_up_window(clinic):
    return clinic_settings.effective(clinic.settings)["follow_up_days"]


def history_sql(patient_column):
    """Two EXISTS on the patient's history: an eligible consultation in the window, and any arrived visit.

    A consultation counts when it arrived or was paid, with the same doctor, within the window
    counted back from the new appointment's day; a follow-up never starts a window (R4.1).
    """
    return (
        'EXISTS (SELECT 1 FROM "scheduling_appointment" h '
        'JOIN "clinics_service" hs ON hs.clinic_id = h.clinic_id AND hs.id = h.service_id '
        'LEFT JOIN "billing_invoice" hi ON hi.clinic_id = h.clinic_id AND hi.appointment_id = h.id AND hi.status = \'open\' '
        f"WHERE h.clinic_id = %s AND h.patient_id = {patient_column} AND h.doctor_id = %s "
        "AND h.day BETWEEN %s AND %s AND h.id <> %s AND hs.kind = 'consultation' AND NOT h.is_follow_up "
        "AND (h.arrived_at IS NOT NULL OR hi.paid > 0)), "
        'EXISTS (SELECT 1 FROM "scheduling_appointment" n '
        f"WHERE n.clinic_id = %s AND n.patient_id = {patient_column} AND n.arrived_at IS NOT NULL AND n.id <> %s)"
    )


def history_params(ctx, doctor_id, day, window, exclude_id=0):
    start = day - dt.timedelta(days=window) if window > 0 else day + dt.timedelta(days=1)  # 0 days: rule off
    return [ctx.clinic_id, doctor_id, start, day, exclude_id, ctx.clinic_id, exclude_id]


def fee_for(service, eligible):
    """(fee, is_follow_up). A consultation in the window takes its follow-up service's price."""
    if service.kind == "consultation" and eligible and service.follow_up_price is not None:
        return service.follow_up_price, True
    return service.price, service.kind == "follow_up"
