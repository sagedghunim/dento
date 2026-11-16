"""Booking rules: who works when, the slot grid, the fee and the follow-up rule (design 5.1, R4.1)."""

import datetime as dt
from dataclasses import dataclass, field

from django.utils.translation import gettext as _

from clinics import settings as clinic_settings
from core import sql, validate
from core.days import business_day, zone
from core.errors import ApiError

BOOKING_HORIZON_DAYS = 366


def not_found(what):
    messages = {
        "appointment": _("This appointment wasn't found."),
        "doctor": _("This doctor wasn't found."),
        "service": _("This service wasn't found."),
        "patient": _("This patient wasn't found."),
        "section": _("This section wasn't found."),
    }
    return ApiError("not_found", messages[what], 404)


@dataclass
class Session:
    starts: dt.time
    ends: dt.time
    mode: str
    slot_minutes: int | None
    max_bookings: int | None
    section_id: int | None = None
    weekday: int | None = None


@dataclass
class DoctorDay:
    """A doctor's sessions on one business day, in every section."""

    doctor_id: int
    name: str
    sessions: list = field(default_factory=list)

    def sessions_in(self, section_id=None):
        """The sessions of one section (a doctor may work two sections on one day); all with None."""
        return [s for s in self.sessions if section_id is None or s.section_id == section_id]

    def has_queue(self, section_id=None):
        return any(s.mode == "queue" for s in self.sessions_in(section_id))

    @property
    def cap(self):
        """max_bookings for the doctor-day (every section: the summary row is per doctor and day).

        The sum of the sessions' limits, or none if any session has none.
        """
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
    section_id: int | None = None


def check_day(clinic, day):
    today = business_day(clinic)
    if not today <= day <= today + dt.timedelta(days=BOOKING_HORIZON_DAYS):
        raise ApiError("invalid", _("Pick a day from today up to a year ahead."), fields={"day": "range"})
    return today


def _session_rows_sql(with_service):
    service = ("sv.id, sv.name, sv.kind, sv.price, sv.is_active, fu.price, fu.is_active, sv.section_id, "
               if with_service else "")
    joins = ('LEFT JOIN "clinics_service" sv ON sv.clinic_id = x.clinic_id AND sv.id = %s '
             'LEFT JOIN "clinics_service" fu ON fu.clinic_id = sv.clinic_id AND fu.id = sv.follow_up_service_id '
             if with_service else "")
    return (f"SELECT {service}m.id, m.role, m.is_active, m.display_name, s.starts, s.ends, s.mode, s.slot_minutes, "
            "s.max_bookings, s.section_id FROM (SELECT %s::bigint AS clinic_id) x "
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
            doctor.sessions.append(Session(*r[offset + 4:offset + 10]))
    return doctor


def lock_doctor(ctx, doctor_id):
    """FOR SHARE on the doctor's membership row before a booking or a move reads the sessions.

    A schedule change takes FOR NO KEY UPDATE on the same row before it reads the bookings, so the two wait
    for each other: a booking can't be stranded by a change it raced, nor use a stale max_bookings (chief judge
    M1, R4; design 5.0). Two bookings take FOR SHARE together without waiting.
    """
    sql.run(ctx, 'SELECT 1 FROM "clinics_membership" WHERE clinic_id = %s AND id = %s FOR SHARE', [ctx.clinic_id, doctor_id])


def doctor_day(ctx, doctor_id, day, lock=False):
    """One statement: the doctor (active owner or doctor of this clinic) and their sessions that weekday.

    `lock` (reschedule) first locks the target doctor's row; reads such as the slot grid don't.
    """
    if lock:
        lock_doctor(ctx, doctor_id)
    rows = sql.rows(ctx, _session_rows_sql(False), [ctx.clinic_id, doctor_id, day.weekday(), ctx.clinic_id])
    return _doctor_from(rows, 0)


def doctor_day_and_service(ctx, doctor_id, service_id, day, lock=False):
    """One statement for booking and quotes: the service with its follow-up price, the doctor and their sessions.

    `lock` (booking) first locks the doctor's row; a quote doesn't.
    """
    if lock:
        lock_doctor(ctx, doctor_id)
    rows = sql.rows(ctx, _session_rows_sql(True),
                    [ctx.clinic_id, service_id, doctor_id, day.weekday(), ctx.clinic_id])
    sid, name, kind, price, active, follow_price, follow_active, section_id = rows[0][:8]
    if sid is None:
        raise not_found("service")
    if not active:
        raise ApiError("invalid", _("This service isn't offered any more."), fields={"service_id": "inactive"})
    service = ServiceInfo(sid, name, kind, price, follow_price if follow_active else None, section_id)
    return _doctor_from(rows, 8), service


def require_working(doctor, section_id=None):
    """The doctor works that day, and in the service's section (design D1: the section comes from the service)."""
    if not doctor.sessions:
        raise ApiError("doctor_off", _("This doctor doesn't work on that day."), 409, fields={"day": "doctor_off"})
    if not doctor.sessions_in(section_id):
        raise ApiError("wrong_section", _("This doctor doesn't work in this section on that day."), 409,
                       fields={"doctor_id": "wrong_section"})


def step_minutes(clinic, session):
    """A session's booking step: its own slot length in slots mode, else the clinic's booking step (default 60)."""
    if session.mode == "slots" and session.slot_minutes:
        return session.slot_minutes
    return clinic_settings.effective(clinic.settings)["booking_step_minutes"]


def grid(clinic, day, doctor, section_id=None):
    """Every bookable time of the doctor's sessions that business day (in one section, if given), as (starts, ends) in UTC.

    Slot sessions step by their slot length; queue sessions offer times too, at the clinic's booking step
    (Saged, 2026-11-13), and still take bookings with no time ("next turn") and walk-ins. A timed booking
    holds its time in either mode (holds_slot), so the same constraints refuse a double booking.
    A session that starts before the clinic's day end (after midnight) is on the next calendar
    day. Slots step in real minutes, so a clock change inside a session can't make a slot twice.
    """
    tz = zone(clinic)
    slots = []
    for session in doctor.sessions_in(section_id):
        start_date = day if session.starts >= clinic.day_ends_at else day + dt.timedelta(days=1)
        end_date = start_date if session.ends > session.starts else start_date + dt.timedelta(days=1)
        start = dt.datetime.combine(start_date, session.starts, tzinfo=tz).astimezone(dt.UTC)
        end = dt.datetime.combine(end_date, session.ends, tzinfo=tz).astimezone(dt.UTC)
        step = dt.timedelta(minutes=step_minutes(clinic, session))
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
    try:
        utc = instant.astimezone(dt.UTC)
    except OverflowError:  # an offset that pushes year 1 or 9999 out of range
        raise ApiError("invalid", f"{name} is out of range", fields={name: "range"})
    if not validate.DAY_MIN <= utc.date() <= validate.DAY_MAX:
        raise ApiError("invalid", f"{name} is out of range", fields={name: "range"})
    return utc


def resolve_slot(clinic, day, doctor, starts_at, section_id=None):
    """(starts, ends) for a slot booking, or (None, None) for a queue booking, in the doctor's sessions of that section."""
    require_working(doctor, section_id)
    if starts_at is None:
        if not doctor.has_queue(section_id):
            raise ApiError("time_required", _("Pick a time for this doctor."), fields={"starts_at": "required"})
        return None, None
    for start, end in grid(clinic, day, doctor, section_id):
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


def fee_for(service, eligible, visit_price=""):
    """(fee, is_follow_up). A consultation in the window takes its follow-up service's price.

    `visit_price` is a plan session's own rule (Saged, 2026-11-16), which then decides instead of the window:
    "none" charges no visit fee (the procedure done in the session is charged on its own), "follow_up" the
    clinic's follow-up price (the full price when it has none), "consultation" the full price. It applies to a
    consultation only: when the desk books the session on another service, that service's price stands.
    """
    if service.kind == "consultation" and visit_price:
        if visit_price == "none":
            return 0, True
        if visit_price == "follow_up" and service.follow_up_price is not None:
            return service.follow_up_price, True
        return service.price, False
    if service.kind == "consultation" and eligible and service.follow_up_price is not None:
        return service.follow_up_price, True
    return service.price, service.kind == "follow_up"


def stranded(clinic, bookings, sessions):
    """The bookings that `sessions` (one doctor's, after a schedule change) no longer cover (design 5.1).

    A booking is covered by the doctor's sessions on its weekday in its own section: a slot booking
    needs its whole slot, start and end, to be a slot of their grid (a 20-minute visit is not covered
    by a 15-minute grid that starts at the same time); a turn without a time needs a queue session.
    `bookings` are dicts with day, section_id, starts_at and ends_at; `sessions` are Session objects
    with a `weekday` attribute.
    """
    by_weekday = {}
    for session in sessions:
        by_weekday.setdefault(session.weekday, []).append(session)
    grids, out = {}, []
    for booking in bookings:
        doctor = DoctorDay(0, "", by_weekday.get(booking["day"].weekday(), []))
        section_id = booking["section_id"]
        if booking["starts_at"] is None:
            covered = doctor.has_queue(section_id)
        else:
            key = (booking["day"], section_id)
            if key not in grids:
                grids[key] = set(grid(clinic, booking["day"], doctor, section_id))
            covered = (booking["starts_at"], booking["ends_at"]) in grids[key]
        if not covered:
            out.append(booking)
    return out


# --- Who sees and acts on which visit and patient (design G1; Saged, 2026-11-13) ---------------------------------


def section_scope(ctx):
    """The sections an assistant is limited to (design G1), or None for every section."""
    member = ctx.membership
    if member is None or member.role != "assistant" or not member.sections:
        return None
    return frozenset(member.sections)


def own_doctor(ctx):
    """The membership id of a doctor bound to their own patients and visits, or None.

    A doctor never sees or acts on another doctor's patients or visits, in every kind of place (Saged,
    2026-11-13): those are 404, as another place's are. The owner and assistants are unaffected.
    """
    member = ctx.membership
    return member.id if member is not None and member.role == "doctor" else None


def is_solo(clinic):
    """The owner runs a single clinic alone (no assistant): never a center."""
    return clinic.kind != "center" and bool(clinic_settings.effective(clinic.settings)["solo"])


def _owner_has_sessions(ctx):
    """Whether the owner has any doctor session in this place. One indexed read, kept on the ctx for the request."""
    cached = getattr(ctx, "_owner_has_sessions", None)
    if cached is None:
        cached = bool(sql.row(ctx, 'SELECT 1 FROM "clinics_schedule" WHERE clinic_id = %s AND doctor_id = %s LIMIT 1',
                              [ctx.clinic_id, ctx.membership_id]))
        object.__setattr__(ctx, "_owner_has_sessions", cached)  # Ctx is frozen; this is a per-request cache
    return cached


def owner_mode(ctx):
    """What the owner's screens are (Saged, 2026-11-13), or None for anyone else:

    "solo": a single clinic run alone: everything, Today with booking and walk-in is home.
    "doctor": also treats patients (has sessions): their own queue and Prescription, plus the manager's sections;
              no booking or walk-in (the desk books).
    "manager": treats nobody: Overview, Finances, Team and Patients only.
    The side bar, the pages and the screens' permissions all read this one rule.
    """
    member = getattr(ctx, "membership", None)
    if member is None or member.role != "owner":
        return None
    if is_solo(ctx.clinic):
        return "solo"
    return "doctor" if _owner_has_sessions(ctx) else "manager"


def queue_doctor(ctx):
    """The doctor whose visits alone Today shows this member: a doctor, or an owner who treats in a shared place."""
    doctor = own_doctor(ctx)
    if doctor is None and owner_mode(ctx) == "doctor":
        return ctx.membership_id
    return doctor


def may_book(ctx):
    """The screens offer booking and walk-in: never to an owner unless they run the clinic alone."""
    return ctx.can("appointments.book") and owner_mode(ctx) in (None, "solo")


def patient_sql(ctx, clinic_column, patient_column):
    """(sql, params) to AND into a WHERE: the patient is one the member may see. Empty for all but a doctor.

    A doctor sees a patient who has a visit with them (index appointment_patient_hist on (clinic, patient, ...))
    or whose file they created (patients_patient's primary key).
    """
    doctor = own_doctor(ctx)
    if doctor is None:
        return "", []
    return (f' AND (EXISTS (SELECT 1 FROM "scheduling_appointment" ov WHERE ov.clinic_id = {clinic_column} '
            f"AND ov.patient_id = {patient_column} AND ov.doctor_id = %s) "
            f'OR EXISTS (SELECT 1 FROM "patients_patient" op WHERE op.clinic_id = {clinic_column} '
            f"AND op.id = {patient_column} AND op.created_by_id = %s))", [doctor, ctx.user_id])


def visit_sql(ctx, doctor_column):
    """(sql, params) to AND into a WHERE on appointments: a doctor's own only. Empty for everyone else."""
    doctor = own_doctor(ctx)
    if doctor is None:
        return "", []
    return f" AND {doctor_column} = %s", [doctor]


def require_patient(ctx, patient_id):
    """404 unless the member may see this patient of this clinic. No query for anyone but a doctor."""
    if own_doctor(ctx) is None:
        return
    extra, params = patient_sql(ctx, "p.clinic_id", "p.id")
    if not sql.row(ctx, f'SELECT 1 FROM "patients_patient" p WHERE p.clinic_id = %s AND p.id = %s{extra}',
                   [ctx.clinic_id, patient_id, *params]):
        raise not_found("patient")


def require_doctor(ctx, doctor_id):
    """A doctor books and moves visits only for themselves: another doctor is 404 to them."""
    doctor = own_doctor(ctx)
    if doctor is not None and doctor_id != doctor:
        raise not_found("doctor")


def check_scope(ctx, section_id, doctor_id):
    """A visit outside the member's sections, or another doctor's visit for a doctor, is not found."""
    scope = section_scope(ctx)
    if scope is not None and section_id not in scope:
        raise not_found("appointment")
    doctor = own_doctor(ctx)
    if doctor is not None and doctor_id != doctor:
        raise not_found("appointment")


def guard(ctx, appointment_id):
    """check_scope for an action by id. Reads the visit only for a limited assistant or a doctor."""
    if section_scope(ctx) is None and own_doctor(ctx) is None:
        return
    found = sql.row(ctx, 'SELECT section_id, doctor_id FROM "scheduling_appointment" WHERE clinic_id = %s AND id = %s',
                    [ctx.clinic_id, appointment_id])
    if found is None:
        raise not_found("appointment")
    check_scope(ctx, found[0], found[1])
