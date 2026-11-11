"""Clinic-level writes: counters other apps take inside their own transaction, sessions, the day settings (design 4.2)."""

import datetime as dt

from django.db import transaction
from django.utils.translation import gettext as _

from audit.services import record_change
from core import sql
from core.days import business_day, local_iso
from core.errors import ApiError

from .models import Schedule
from .selectors import session_payload

COUNTERS = ("patient_file", "invoice", "prescription")  # also the lock order between them (design 5.0)


def take_numbers(ctx, name, count=1):
    """Gap-free numbers from a clinic counter, taken inside the transaction that uses them.

    The counter row stays locked until commit, and a rollback gives the numbers back.
    Returns the numbers as a list.
    """
    assert name in COUNTERS and count >= 1
    value = sql.row(ctx, 'INSERT INTO "clinics_counter" AS k (clinic_id, name, value) VALUES (%s, %s, %s) '
                         "ON CONFLICT (clinic_id, name) DO UPDATE SET value = k.value + EXCLUDED.value RETURNING value",
                    [ctx.clinic_id, name, count])[0]
    return list(range(value - count + 1, value + 1))


# --- Sessions (design 5.1, D2: the M3 settings screens) -------------------------------------------------------
#
# Lock order: request key -> the doctors' membership rows (FOR NO KEY UPDATE, so changes to one doctor's week
# go one at a time; a booking takes only KEY SHARE on it) -> the session row -> clinic_day (today's version).


def not_found(what):
    messages = {"doctor": _("This doctor wasn't found."), "section": _("This section wasn't found."),
                "session": _("This session wasn't found.")}
    return ApiError("not_found", messages[what], 404)


def _lock_doctors(ctx, *doctor_ids):
    """Lock the doctors' membership rows in id order; each must be an active owner or doctor of this place."""
    ids = sorted(set(doctor_ids))
    found = {r[0] for r in sql.rows(ctx, 'SELECT id FROM "clinics_membership" WHERE clinic_id = %s AND id = ANY(%s) '
                                         "AND is_active AND role IN ('owner', 'doctor') ORDER BY id FOR NO KEY UPDATE",
                                    [ctx.clinic_id, ids])}
    if found != set(ids):
        raise not_found("doctor")


def _check_section(ctx, section_id):
    if not sql.row(ctx, 'SELECT 1 FROM "clinics_section" WHERE clinic_id = %s AND id = %s AND is_active',
                   [ctx.clinic_id, section_id]):
        raise not_found("section")


def _span(clinic, starts, ends):
    """A session as minutes after the place's day end: (start, end). Sessions may run past midnight (design 4.0)."""
    def minutes(t):
        return t.hour * 60 + t.minute

    start = (minutes(starts) - minutes(clinic.day_ends_at)) % 1440
    return start, start + (minutes(ends) - minutes(starts)) % 1440


def _check_fits(ctx, session, exclude_id=None):
    """Inside one business day, and not overlapping the doctor's other sessions that day, in any section."""
    start, end = _span(ctx.clinic, session["starts"], session["ends"])
    if end > 1440:
        raise ApiError("invalid", _("A session must end before the business day ends."), fields={"ends": "past_day_end"})
    others = Schedule.objects.filter(clinic_id=ctx.clinic_id, doctor_id=session["doctor_id"], weekday=session["weekday"])
    if exclude_id is not None:
        others = others.exclude(id=exclude_id)
    for other in others:
        other_start, other_end = _span(ctx.clinic, other.starts, other.ends)
        if start < other_end and other_start < end:
            raise ApiError("session_overlap", _("This session overlaps another session of the same doctor."), 409,
                           fields={"starts": "overlap"})


def _check_not_stranded(ctx, doctor_id, weekdays):
    """Refuse a change that leaves future bookings off the doctor's grid or outside their sessions (design 5.1)."""
    from scheduling import rules
    from scheduling.selectors import future_bookings

    bookings = future_bookings(ctx, doctor_id, weekdays, business_day(ctx.clinic))
    if not bookings:
        return
    sessions = [rules.Session(s.starts, s.ends, s.mode, s.slot_minutes, s.max_bookings, s.section_id, s.weekday)
                for s in Schedule.objects.filter(clinic_id=ctx.clinic_id, doctor_id=doctor_id, weekday__in=list(weekdays))]
    lost = rules.stranded(ctx.clinic, bookings, sessions)
    if lost:
        raise ApiError("schedule_conflict", _("This change would leave booked visits outside the doctor's sessions."), 409,
                       extra={"count": len(lost), "bookings": [
                           {"id": b["id"], "day": b["day"].isoformat(), "queue_no": b["queue_no"],
                            "starts_at": local_iso(ctx.clinic, b["starts_at"]) if b["starts_at"] else None}
                           for b in lost[:50]]})


def _today_version(ctx):
    from summaries.services import bump

    today = business_day(ctx.clinic)
    return bump(ctx, today)[today]


def create_session(ctx, session):
    """Add a weekly session for a doctor in one section. `session` is validated (clinics.views.clean_session)."""
    _lock_doctors(ctx, session["doctor_id"])
    _check_section(ctx, session["section_id"])
    _check_fits(ctx, session)
    created = Schedule.objects.create(clinic_id=ctx.clinic_id, **session)
    payload = session_payload(created)
    record_change(ctx, "schedule.create", "schedule", created.id, after=payload)
    return payload, _today_version(ctx)


def update_session(ctx, schedule_id, session, expected):
    """Replace a session with absolute values, if it still holds `expected` (design 5.0: PUT sends the old values)."""
    current = Schedule.objects.filter(clinic_id=ctx.clinic_id, id=schedule_id).values_list("doctor_id", flat=True).first()
    if current is None:
        raise not_found("session")
    _lock_doctors(ctx, current, session["doctor_id"])
    row = Schedule.objects.select_for_update(no_key=True).filter(clinic_id=ctx.clinic_id, id=schedule_id).first()
    if row is None:
        raise not_found("session")
    before = session_payload(row)
    stale = sorted(name for name, value in expected.items() if before.get(name) != value)
    if stale:
        raise ApiError("stale", _("Someone changed this session meanwhile. Reload and try again."), 409,
                       fields={name: "changed" for name in stale}, extra={"current": before})
    _check_section(ctx, session["section_id"])
    _check_fits(ctx, session, exclude_id=row.id)
    Schedule.objects.filter(clinic_id=ctx.clinic_id, id=row.id).update(**session)
    for name, value in session.items():
        setattr(row, name, value)
    _check_not_stranded(ctx, before["doctor_id"], {before["weekday"]})
    after = session_payload(row)
    if after != before:
        record_change(ctx, "schedule.update", "schedule", row.id, before=before, after=after)
    return after, _today_version(ctx)


def delete_session(ctx, schedule_id):
    """Remove a session, unless future bookings depend on it."""
    current = Schedule.objects.filter(clinic_id=ctx.clinic_id, id=schedule_id).values_list("doctor_id", flat=True).first()
    if current is None:
        raise not_found("session")
    _lock_doctors(ctx, current)
    row = Schedule.objects.select_for_update(no_key=True).filter(clinic_id=ctx.clinic_id, id=schedule_id).first()
    if row is None:
        raise not_found("session")
    before = session_payload(row)
    Schedule.objects.filter(clinic_id=ctx.clinic_id, id=schedule_id).delete()
    _check_not_stranded(ctx, before["doctor_id"], {before["weekday"]})
    record_change(ctx, "schedule.delete", "schedule", schedule_id, before=before)
    return _today_version(ctx)


# --- The time zone and day end (design 4.2; the directory copy, I6) -------------------------------------------


def change_day_settings(ctx, clinic, changes):
    """Set the place's time zone and/or day end. Refused once it has bookings, since every `day` would shift.

    `clinic` is the row the caller locked; `changes` is validated. The directory keeps a copy for the
    nightly jobs: it is written after the shard commits, and the sweep repairs it if that step is lost.
    Returns the old values.
    """
    from directory.services import record_facts
    from scheduling.selectors import has_bookings

    before = {"timezone": clinic.timezone, "day_ends_at": clinic.day_ends_at.strftime("%H:%M")}
    if all(before[name] == value for name, value in changes.items()):
        return before
    if has_bookings(ctx):
        raise ApiError("has_bookings", _("The time zone and day end can't change once the place has bookings."), 409,
                       fields={name: "locked" for name in changes})
    clinic.timezone = changes.get("timezone", clinic.timezone)
    if "day_ends_at" in changes:
        clinic.day_ends_at = dt.time.fromisoformat(changes["day_ends_at"])
    clinic.save(update_fields=["timezone", "day_ends_at"])
    facts = {"kind": clinic.kind, "timezone": clinic.timezone, "day_ends_at": clinic.day_ends_at}
    transaction.on_commit(lambda: record_facts(clinic.id, **facts), using=ctx.alias)
    return before
