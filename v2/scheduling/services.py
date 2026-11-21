"""Booking, status changes and reschedule (design 5.1, 5.2). Inside the request's shard transaction.

Lock order (design 5.0): request key → appointment → invoice → counters (patient_file,
invoice) → doctor-days (one statement) → clinic-days (one statement, last).
"""

from django.db import IntegrityError
from django.utils import timezone
from django.utils.translation import gettext as _

from audit.services import record_change
from billing import services as billing
from clinics import settings as clinic_settings
from clinics.services import take_numbers
from core import sql
from core.days import business_day
from core.errors import ApiError
from patients import services as patients
from summaries import selectors as summaries_read
from summaries.services import Deltas, record_visits

from . import rules
from .models import Appointment

# action: (the statuses it starts from, to, timestamp column). The stages (design E3):
# booked -> waiting -> with_assistant (optional) -> ready -> in_room -> done.
TRANSITIONS = {
    "arrive": (("booked",), "waiting", "arrived_at"),
    "assist": (("waiting",), "with_assistant", "with_assistant_at"),
    "ready": (("waiting", "with_assistant"), "ready", "ready_at"),
    "call": (("waiting", "ready"), "in_room", "called_at"),
    "finish": (("in_room",), "done", "finished_at"),
    "cancel": (("booked",), "cancelled", "cancelled_at"),
}
# Undo on the desk (design D4, E3): one step back from each stage, clearing its time. The step back is
# worked out from the times still set, so "ready" goes back to with_assistant only if the visit was there.
UNDO = {
    "undo_arrive": ("waiting", "arrived_at", "'booked'", ("booked",)),
    "undo_assist": ("with_assistant", "with_assistant_at", "'waiting'", ("waiting",)),
    "undo_ready": ("ready", "ready_at", "CASE WHEN with_assistant_at IS NULL THEN 'waiting' ELSE 'with_assistant' END",
                   ("waiting", "with_assistant")),
    "undo_call": ("in_room", "called_at", "CASE WHEN ready_at IS NULL THEN 'waiting' ELSE 'ready' END",
                  ("waiting", "ready")),
}


def _slot_taken():
    return ApiError("slot_taken", _("This time is already booked."), 409, fields={"starts_at": "taken"})


SLOT_CONSTRAINTS = ("appointment_slot_uniq", "appointment_no_overlap")  # an equal start, or an overlapping slot


def _constraint(error):
    cause = error.__cause__
    diag = getattr(cause, "diag", None)
    return getattr(diag, "constraint_name", None)


def book(ctx, request, deltas=None):
    """POST /appointments (design 5.1). `request` is the validated body (see views.book_view).

    Returns (appointment payload, version of the appointment's day). With `deltas` (the finish dialog's "book
    the next visit", design E3) the summary changes join the caller's: this writes the doctor-days in the one
    statement that also takes the turn, and the caller writes the clinic-days; the version is then None.
    """
    own = deltas is None
    clinic = ctx.clinic
    day = request["day"]
    today = rules.check_day(clinic, day)
    payment = request["payment"]
    if request["new_patient"] is not None:
        ctx.require("patients.edit")
    is_deposit = bool(payment is not None and payment.get("deposit"))
    if payment is not None:
        ctx.require("billing.collect")
        if is_deposit:
            # A booking deposit (Saged, 2026-11-16): for any day, and the clinic's own amount, never the screen's.
            # Not for a patient who is here now (a walk-in, or "here now" in Book): they pay at the visit, so a
            # deposit is only ever money taken before the visit.
            percent = clinic_settings.deposit_percent(clinic)  # a percent is checked once the fee is known, below
            asked = clinic_settings.deposit(clinic)
            if request["arrive"]:
                raise ApiError("invalid", _("A patient who is here now pays at the visit, not a deposit."),
                               fields={"payment.deposit": "here_now"})
            if not percent and not asked:
                raise ApiError("deposit_off", _("This clinic doesn't take a deposit with bookings."), 409,
                               fields={"payment": "deposit_off"})
            if not percent and payment["amount"] != asked:
                raise ApiError("deposit_changed", _("The deposit amount changed. Reload and try again."), 409,
                               fields={"payment.amount": "changed"}, extra={"amount": asked})
        elif day != today:
            raise ApiError("payment_not_today", _("A payment with a booking is only for today. Book, then take the payment."),
                           fields={"payment": "not_today"})
    if request["arrive"]:
        ctx.require("appointments.queue")
        if day != today:
            raise _not_today()

    rules.require_doctor(ctx, request["doctor_id"])  # a doctor books only for themselves (Saged, 2026-11-13)
    doctor, service = rules.doctor_day_and_service(ctx, request["doctor_id"], request["service_id"], day, lock=True)
    scope = rules.section_scope(ctx)
    if scope is not None and service.section_id not in scope:
        raise rules.not_found("service")  # a limited assistant books only in their sections (design G1)
    # The section comes from the service; the doctor must have a session in it that day (design D1).
    if request.get("walk_in"):
        # A walk-in takes the next turn with no time, even with a slot doctor (design G5).
        rules.require_working(doctor, service.section_id)
        starts_at, ends_at = None, None
    else:
        starts_at, ends_at = rules.resolve_slot(clinic, day, doctor, request["starts_at"], service.section_id)
    is_extra = bool(request["extra"] and starts_at is not None)

    if request["patient_id"] is not None:
        mine, mine_params = rules.patient_sql(ctx, "p.clinic_id", "p.id")  # another doctor's patient is 404 to a doctor
        found = sql.row(ctx, f'SELECT p.id, p.full_name, p.phone, p.file_no, {rules.history_sql("p.id")} '
                             f'FROM "patients_patient" p WHERE p.clinic_id = %s AND p.id = %s AND p.archived_at IS NULL{mine}',
                        [*rules.history_params(ctx, doctor.doctor_id, day, rules.follow_up_window(clinic)),
                         ctx.clinic_id, request["patient_id"], *mine_params])
        if found is None:
            raise rules.not_found("patient")
        patient = {"id": found[0], "full_name": found[1], "phone": found[2], "file_no": found[3]}
        eligible, has_arrived = found[4], found[5]
        fields = None
        check_same_day(ctx, patient, doctor.doctor_id, day, request.get("confirm_other_doctor", False))
    else:
        fields = patients.clean_fields(request["new_patient"], today)
        eligible, has_arrived = False, False
    visit_price = ""
    if request.get("follow_up_id") is not None:
        # The plan session's own visit fee (set on the follow-up when the doctor added it; it never changes after),
        # read here without a lock; `claim` below locks the follow-up and checks whose it is.
        from clinical.followups import visit_price_of

        found_price = visit_price_of(ctx, request["follow_up_id"])
        visit_price = found_price if service.kind == "consultation" else ""
    fee, is_follow_up = rules.fee_for(service, eligible, visit_price)
    qty = (request.get("qty") or 1) if service.kind == "procedure" else 1  # 3 fillings: 3 times the price
    fee *= qty
    if is_deposit and clinic_settings.deposit_percent(clinic):
        asked = clinic_settings.deposit(clinic, fee)  # a percent of this visit's fee (Saged, 2026-11-19)
        if not asked:
            raise ApiError("deposit_off", _("This visit has no fee, so it takes no deposit."), 409,
                           fields={"payment": "deposit_off"})
        if payment["amount"] != asked:
            raise ApiError("deposit_changed", _("The deposit amount changed. Reload and try again."), 409,
                           fields={"payment.amount": "changed"}, extra={"amount": asked})
    if payment is not None and payment["amount"] > fee and not is_deposit:
        raise ApiError("over_balance", _("The amount is more than what is owed."), 409, fields={"amount": "over_balance"})

    # Writes, in the global lock order.
    if fields is not None:
        patient = patients.create(ctx, fields)  # counter patient_file
    invoice_number = take_numbers(ctx, "invoice")[0] if payment is not None else None  # kept for ensure_invoice
    shift_id = None
    if payment is not None and payment["method"] == "cash":
        from finance.services import shift_for

        shift_id = shift_for(ctx, "cash")  # the open shift (8a), opened if none is, before the doctor-day
    deltas = (Deltas() if own else deltas).take_turn(day, doctor.doctor_id, doctor.cap)
    deltas.clinic_day(day)
    if payment is not None:
        deltas.doctor_day(today, doctor.doctor_id, collected=payment["amount"])
        deltas.money_day(today, service.section_id, payment["method"], collected=payment["amount"])
        deltas.clinic_day(today, collected=payment["amount"])
    queue_no = deltas.write_doctors(ctx)

    now = timezone.now()
    try:
        appointment = Appointment.objects.create(
            clinic_id=ctx.clinic_id, patient_id=patient["id"], doctor_id=doctor.doctor_id, service_id=service.id,
            section_id=service.section_id, day=day, queue_no=queue_no, starts_at=starts_at, ends_at=ends_at,
            is_extra=is_extra,
            status="waiting" if request["arrive"] else "booked", source=request["source"], fee=fee,
            is_follow_up=is_follow_up, is_new=not has_arrived, note=request["note"], visit_price=visit_price, qty=qty,
            arrived_at=now if request["arrive"] else None, created_at=now, created_by_id=ctx.membership_id,
            updated_at=now, updated_by_id=ctx.membership_id)
    except IntegrityError as error:
        if _constraint(error) in SLOT_CONSTRAINTS:
            raise _slot_taken()
        raise

    invoice = None
    if payment is not None:
        locked = {"id": appointment.id, "status": appointment.status, "day": day, "doctor_id": doctor.doctor_id,
                  "patient_id": patient["id"], "service_id": service.id, "fee": fee, "service_name": service.name,
                  "section_id": service.section_id, "qty": qty}
        # The summary changes are already in `deltas` above, so collect into a throwaway.
        _payment, invoice = billing.collect(ctx, locked, payment["amount"], payment["method"], Deltas(),
                                            invoice_number=invoice_number, today=today, shift_id=shift_id,
                                            deposit=is_deposit)
    if request.get("follow_up_id") is not None:
        # The plan's session this booking is for: claimed under its row lock, or the whole booking is refused
        # (two people booking one session, a resend under a new key). Today's strip hears through the version.
        from clinical.followups import claim

        visit_day = claim(ctx, request["follow_up_id"], appointment.id, patient["id"])
        if visit_day == today:
            deltas.clinic_day(today)
    version = deltas.write_clinics(ctx)[day] if own else None

    from .selectors import payload

    body = payload(clinic, {
        "id": appointment.id, "day": day, "queue_no": queue_no, "status": appointment.status, "starts_at": starts_at,
        "ends_at": ends_at, "is_extra": is_extra, "source": request["source"], "fee": fee, "is_follow_up": is_follow_up,
        "is_new": not has_arrived, "auto_closed": False, "note": request["note"], "arrived_at": appointment.arrived_at,
        "visit_price": visit_price, "qty": qty,
        "called_at": None, "finished_at": None, "cancelled_at": None, "cancel_reason": "", "created_at": now,
        "doctor_id": doctor.doctor_id,
        "patient_id": patient["id"], "patient_name": patient["full_name"], "patient_phone": patient["phone"],
        "patient_file_no": patient["file_no"], "service_id": service.id, "service_name": service.name,
        "section_id": service.section_id,
        "invoice_id": invoice["id"] if invoice else None, "invoice_number": invoice["number"] if invoice else None,
        "invoice_total": invoice["total"] if invoice else None, "invoice_paid": invoice["paid"] if invoice else None,
    })
    return body, version


ACTIVE = ("booked", "waiting", "with_assistant", "ready", "in_room")
HERE = ("waiting", "with_assistant", "ready", "in_room")  # arrived and not finished: in the clinic now


def _where(status):
    return {"waiting": _("waiting"), "with_assistant": _("with the assistant"), "ready": _("ready for the doctor"),
            "in_room": _("with the doctor")}[status]


def same_day_visits(ctx, patient_id, day, exclude_id=None):
    """The patient's open visits on `day` (booked, or here and not finished), with the doctor's names: one statement
    on index appointment_patient_hist."""
    return sql.dicts(ctx, """
        SELECT a.id, a.doctor_id, a.section_id, a.status, a.queue_no, a.starts_at, m.display_name, m.display_name_en
          FROM "scheduling_appointment" a
          JOIN "clinics_membership" m ON m.clinic_id = a.clinic_id AND m.id = a.doctor_id
         WHERE a.clinic_id = %s AND a.patient_id = %s AND a.day = %s AND a.status = ANY(%s) AND a.id <> %s
         ORDER BY a.id LIMIT 20""", [ctx.clinic_id, patient_id, day, list(ACTIVE), exclude_id or 0])


def _lock_patient_day(ctx, patient_id, day):
    sql.row(ctx, "SELECT pg_advisory_xact_lock(hashtextextended('visit:' || %s || ':' || %s || ':' || %s, 0))",
            [ctx.clinic_id, patient_id, day.isoformat()])


def visible_visit(ctx, visit):
    """A limited assistant sees only their sections' visits, a doctor only their own (design G1)."""
    scope = rules.section_scope(ctx)
    doctor = rules.own_doctor(ctx)
    return (scope is None or visit["section_id"] in scope) and (doctor is None or visit["doctor_id"] == doctor)


def visit_extra(ctx, visit):
    from core.days import local_iso
    from core.i18n import pick

    return {"id": visit["id"], "status": visit["status"], "queue_no": visit["queue_no"],
            "starts_at": local_iso(ctx.clinic, visit["starts_at"]) if visit["starts_at"] else None,
            "doctor_id": visit["doctor_id"], "doctor_name": pick(visit["display_name"], visit["display_name_en"])}


def check_same_day(ctx, patient, doctor_id, day, confirm=False, exclude_id=None):
    """No double booking of one patient (Saged, 2026-11-17), under a lock on (clinic, patient, day):

    - a patient who is in the clinic now (arrived, not finished) gets no second visit that day, with any doctor;
    - a patient with an open visit with this doctor that day gets no second one with them;
    - an open booking with another doctor that day is a warning the desk confirms (`confirm`): centers.
    A finished, cancelled or missed visit doesn't count. The transaction-level advisory lock makes two desks (or a
    double tap under two keys) go one at a time: the second's read, a new statement after the lock, sees the
    first's visit. Taken after the doctor's FOR SHARE (or the appointment's own lock) and before the counters
    (design 5.0); only booking, reschedule and undo-no-show take it, and they take no older lock after it.
    """
    _lock_patient_day(ctx, patient["id"], day)
    visits = same_day_visits(ctx, patient["id"], day, exclude_id)
    name = patient["full_name"]
    for visit in visits:
        if visit["status"] in HERE or visit["doctor_id"] == doctor_id:
            shown = visible_visit(ctx, visit)
            if not shown:
                message = _("This patient already has an open visit in the clinic that day.")
            elif visit["status"] in HERE:
                message = _("%(name)s is already here now: %(where)s (turn %(n)s).") % {
                    "name": name, "where": _where(visit["status"]), "n": visit["queue_no"]}
            elif visit["starts_at"] is not None:
                from web.format import clock_text

                message = _("%(name)s is already booked with this doctor at %(time)s.") % {
                    "name": name, "time": clock_text(visit["starts_at"], ctx.clinic)}
            else:
                message = _("%(name)s is already booked with this doctor that day (turn %(n)s).") % {
                    "name": name, "n": visit["queue_no"]}
            raise ApiError("already_booked", message, 409, fields={"patient_id": "already_booked"},
                           extra={"appointment": visit_extra(ctx, visit) if shown else None})
    others = [v for v in visits if visible_visit(ctx, v)]
    if others and not confirm:
        doctor_name = visit_extra(ctx, others[0])["doctor_name"]
        raise ApiError("other_doctor_today", _("%(name)s has a booking that day with %(doctor)s.") % {
            "name": name, "doctor": doctor_name}, 409, fields={"confirm_other_doctor": "required"},
            extra={"appointments": [visit_extra(ctx, v) for v in others]})


def _not_today():
    return ApiError("not_today", _("A visit can be marked arrived only on its own day."), 409, fields={"day": "not_today"})


def change_status(ctx, appointment_id, action, seen, *, reason="", payment=None, earlier=None, next_visit=None,
                  outcome=None):
    """arrive, call, finish (optionally with a payment) and cancel (design 5.2).

    The client sends the status it saw. One conditional UPDATE decides; a repeat of a change
    that already happened is a harmless 200. Returns the version of the appointment's day.
    """
    sources, target, column = TRANSITIONS[action]
    rules.guard(ctx, appointment_id)  # another section's visit, or another doctor's for a doctor, is 404
    if seen not in sources:
        raise ApiError("invalid_transition", _("This action doesn't apply to a visit in that status."), 400,
                       fields={"status": "invalid"})
    source = seen
    clinic = ctx.clinic
    today = business_day(clinic)
    now = timezone.now()
    extra_set, extra_params, extra_where = "", [], ""
    if action == "cancel":
        extra_set, extra_params = ", cancel_reason = %s", [reason]
    if action == "arrive":
        extra_where = " AND day = %s"  # arrive is allowed only on the visit's business day
    if action == "call":
        _one_in_room(ctx, appointment_id)
    params =[target, now, now, ctx.membership_id, *extra_params, ctx.clinic_id, appointment_id, source]
    if action == "arrive":
        params.append(today)
    changed = sql.row(ctx, f'UPDATE "scheduling_appointment" SET status = %s, {column} = %s, updated_at = %s, '
                           f"updated_by_id = %s{extra_set} WHERE clinic_id = %s AND id = %s AND status = %s{extra_where} "
                           "RETURNING id, status, day, doctor_id, patient_id, service_id, fee, is_follow_up, "
                           '(SELECT name FROM "clinics_service" s WHERE s.clinic_id = "scheduling_appointment".clinic_id '
                           'AND s.id = "scheduling_appointment".service_id), section_id, arrived_at, called_at, '
                           "finished_at, visit_price, qty", params)
    if changed is None:
        return _unchanged(ctx, appointment_id, action, target, today,
                          with_payment=payment is not None or earlier is not None or next_visit is not None)
    appointment = dict(zip(("id", "status", "day", "doctor_id", "patient_id", "service_id", "fee", "is_follow_up",
                            "service_name", "section_id", "arrived_at", "called_at", "finished_at", "visit_price", "qty"),
                           changed))
    day, doctor_id = appointment["day"], appointment["doctor_id"]
    deltas = Deltas().clinic_day(day)
    if action == "arrive":
        _already_here(ctx, appointment)
        _recheck_follow_up(ctx, appointment)
    elif action == "finish":
        deltas.doctor_day(day, doctor_id, done=1).clinic_day(day, done=1)
        deltas.timing(day, doctor_id, appointment["arrived_at"], appointment["called_at"], appointment["finished_at"])
        if earlier is not None:
            # The visit's invoice and the patient's older unpaid ones, in one statement in id order (design 5.0).
            ctx.require("billing.collect")
            invoices = billing.lock_patient_invoices(ctx, appointment["patient_id"], appointment_id)
        if payment is not None:
            ctx.require("billing.collect")
            billing.collect(ctx, appointment, payment["amount"], payment["method"], deltas, today=today)
        else:
            billing.ensure_invoice(ctx, appointment)  # design 5.3: finish ensures the invoice, paid or owed
        if earlier is not None:
            billing.collect_earlier(ctx, invoices, appointment_id, earlier["amount"], earlier["method"], deltas, today)
        record_visits(ctx, [(appointment["patient_id"], day)])  # the patient's stats row (F6, lock order 11a)
        if next_visit is not None:
            # "Book the next visit" (design E3), in this transaction; its turn goes into the one doctor-day write.
            ctx.require("appointments.book")
            body, _version = book(ctx, {**next_visit, "patient_id": appointment["patient_id"], "new_patient": None,
                                        "arrive": False, "payment": None, "source": "clinic",
                                        # the doctor chose it: another doctor's booking that day doesn't stop it
                                        "confirm_other_doctor": True}, deltas=deltas)
            if outcome is not None:
                outcome["next_appointment"] = body
    elif action == "cancel":
        billing.cancel_invoice(ctx, appointment_id)
        deltas.doctor_day(day, doctor_id, booked=-1)
    if not (action == "finish" and next_visit is not None):
        deltas.write_doctors(ctx)  # a next visit's booking already wrote them all, with its turn, in one statement
    return deltas.write_clinics(ctx)[day]


def _already_here(ctx, appointment):
    """A patient is in the clinic once at a time (Saged, 2026-11-17): marking a second visit of theirs arrived (one
    booked with another doctor) is refused while the first is still here. Under the same lock as booking."""
    _lock_patient_day(ctx, appointment["patient_id"], appointment["day"])
    here = [v for v in same_day_visits(ctx, appointment["patient_id"], appointment["day"], appointment["id"])
            if v["status"] in HERE]
    if here:
        visit = here[0]
        shown = visible_visit(ctx, visit)
        message = (_("This patient is already here now: %(where)s with %(doctor)s (turn %(n)s).") % {
            "where": _where(visit["status"]), "doctor": visit_extra(ctx, visit)["doctor_name"], "n": visit["queue_no"]}
            if shown else _("This patient already has an open visit in the clinic that day."))
        raise ApiError("already_here", message, 409, fields={"status": "already_here"},
                       extra={"appointment": visit_extra(ctx, visit) if shown else None})


def _one_in_room(ctx, appointment_id):
    """A doctor has one patient in the room at a time (Saged, 2026-11-13): calling a second is 409 doctor_busy.

    A transaction-level advisory lock on the doctor serialises two call-ins for the same doctor, so the second
    reads the first's committed row and is refused. Only call-ins take it, so it joins no other lock order.
    The check is on the visit's own day (index appointment_turn_uniq on clinic, day, doctor).
    """
    # One statement reads the visit and, only if it can be called in, waits for the doctor's lock.
    found = sql.row(ctx, "SELECT a.doctor_id, a.day, a.status, CASE WHEN a.status IN ('waiting', 'ready') THEN "
                         "pg_advisory_xact_lock(hashtextextended('in_room:' || a.clinic_id || ':' || a.doctor_id, 0)) END "
                         'FROM "scheduling_appointment" a WHERE a.clinic_id = %s AND a.id = %s', [ctx.clinic_id, appointment_id])
    if found is None or found[2] not in TRANSITIONS["call"][0]:
        return  # the conditional UPDATE answers: 404, a harmless repeat, or status_changed
    doctor_id, day = found[0], found[1]
    # A new statement, so a new snapshot: it sees a call-in that committed while this one waited for the lock.
    busy = sql.row(ctx, 'SELECT id FROM "scheduling_appointment" WHERE clinic_id = %s AND day = %s AND doctor_id = %s '
                        "AND status = 'in_room' AND id <> %s LIMIT 1", [ctx.clinic_id, day, doctor_id, appointment_id])
    if busy is not None:
        raise ApiError("doctor_busy", _("The doctor already has a patient in the room. Finish that visit first."), 409,
                       fields={"status": "doctor_busy"}, extra={"appointment_id": busy[0]})


def undo(ctx, appointment_id, action, seen):
    """One step back from a stage (design D4, E3): undo_arrive, undo_assist, undo_ready and undo_call.

    One conditional UPDATE decides, so an undo racing the next step takes effect only if it gets there
    first; the loser sees 409 status_changed. The turn number stays. Only Today's version changes
    (design 5.3: the stages and their undo change no counter). Each undo goes to the change log.
    Returns the version of the appointment's day, or None for a harmless repeat.
    """
    source, column, target_sql, targets = UNDO[action]
    rules.guard(ctx, appointment_id)
    if seen != source:
        raise ApiError("invalid_transition", _("This action doesn't apply to a visit in that status."), 400,
                       fields={"status": "invalid"})
    changed = sql.row(ctx, f'UPDATE "scheduling_appointment" SET status = {target_sql}, {column} = NULL, updated_at = %s, '
                           "updated_by_id = %s WHERE clinic_id = %s AND id = %s AND status = %s "
                           f"RETURNING day, OLD.{column}, status",
                      [timezone.now(), ctx.membership_id, ctx.clinic_id, appointment_id, source])
    if changed is None:
        return _unchanged(ctx, appointment_id, action, targets, None)
    day, undone_at, target = changed
    record_change(ctx, f"appointment.{action}", "appointment", appointment_id,
                  before={"status": source, column: undone_at.isoformat()}, after={"status": target, column: None})
    return Deltas().clinic_day(day).write_clinics(ctx)[day]


def _only_today():
    return ApiError("not_today", _("This can be done only on the visit's own day."), 409, fields={"day": "not_today"})


def mark_no_show(ctx, appointment_id, seen):
    """The desk marks a booked visit "didn't come" (desk review 7): today only, once its time has passed.

    A queue booking has no time, so it may be marked any time on its day. One conditional UPDATE decides; the
    slot is freed (holds_slot); the doctor-day's no-show count rises, as the nightly sweep's does. Goes to the
    change log. Returns the version of the day, or None for a harmless repeat.
    """
    rules.guard(ctx, appointment_id)
    if seen != "booked":
        raise ApiError("invalid_transition", _("This action doesn't apply to a visit in that status."), 400,
                       fields={"status": "invalid"})
    now = timezone.now()
    today = business_day(ctx.clinic)
    changed = sql.row(ctx, 'UPDATE "scheduling_appointment" SET status = %s, updated_at = %s, updated_by_id = %s '
                           "WHERE clinic_id = %s AND id = %s AND status = 'booked' AND day = %s "
                           "AND (starts_at IS NULL OR starts_at <= %s) RETURNING day, doctor_id, service_id",
                      ["no_show", now, ctx.membership_id, ctx.clinic_id, appointment_id, today, now])
    if changed is None:
        current = Appointment.objects.filter(clinic_id=ctx.clinic_id, id=appointment_id).values_list(
            "status", "day", "starts_at").first()
        if current is None:
            raise rules.not_found("appointment")
        status, day, starts_at = current
        if status == "no_show":
            return None
        if status == "booked" and day != today:
            raise _only_today()
        if status == "booked":
            raise ApiError("not_yet", _("This booking's time hasn't come yet."), 409, fields={"status": "not_yet"})
        raise ApiError("status_changed", _("This visit's status changed meanwhile."), 409, fields={"status": status})
    day, doctor_id, service_id = changed
    record_change(ctx, "appointment.no_show", "appointment", appointment_id,
                  before={"status": "booked"}, after={"status": "no_show"})
    # A visit that didn't happen charges no visit fee: a deposit taken with the booking stays, and nothing is owed.
    billing.set_visit_line(ctx, appointment_id, service_id, 0, "no_show")
    deltas = Deltas().doctor_day(day, doctor_id, no_shows=1).clinic_day(day)
    deltas.write_doctors(ctx)
    return deltas.write_clinics(ctx)[day]


def undo_no_show(ctx, appointment_id, seen):
    """Undo a hand-marked "didn't come", today only: back to booked. A visit the nightly sweep closed stays.

    If someone took the slot meanwhile, the slot constraint refuses it (409 slot_taken).
    """
    rules.guard(ctx, appointment_id)
    if seen != "no_show":
        raise ApiError("invalid_transition", _("This action doesn't apply to a visit in that status."), 400,
                       fields={"status": "invalid"})
    today = business_day(ctx.clinic)
    try:
        changed = sql.row(ctx, 'UPDATE "scheduling_appointment" SET status = %s, updated_at = %s, updated_by_id = %s '
                               "WHERE clinic_id = %s AND id = %s AND status = 'no_show' AND day = %s "
                               "AND NOT auto_closed RETURNING day, doctor_id, service_id, fee",
                          ["booked", timezone.now(), ctx.membership_id, ctx.clinic_id, appointment_id, today])
    except IntegrityError as error:
        if _constraint(error) in SLOT_CONSTRAINTS:
            raise _slot_taken()
        raise
    if changed is None:
        current = Appointment.objects.filter(clinic_id=ctx.clinic_id, id=appointment_id).values_list(
            "status", "day").first()
        if current is None:
            raise rules.not_found("appointment")
        status, day = current
        if status == "booked":
            return None
        if status == "no_show" and day != today:
            raise _only_today()
        raise ApiError("status_changed", _("This visit's status changed meanwhile."), 409, fields={"status": status})
    day, doctor_id, service_id, fee = changed
    # Booked again only if no other open visit took its place meanwhile (Saged, 2026-11-17).
    patient = sql.dicts(ctx, 'SELECT p.id, p.full_name FROM "patients_patient" p JOIN "scheduling_appointment" a '
                             "ON a.clinic_id = p.clinic_id AND a.patient_id = p.id WHERE a.clinic_id = %s AND a.id = %s",
                        [ctx.clinic_id, appointment_id])[0]
    check_same_day(ctx, patient, doctor_id, day, confirm=True, exclude_id=appointment_id)
    record_change(ctx, "appointment.undo_no_show", "appointment", appointment_id,
                  before={"status": "no_show"}, after={"status": "booked"})
    if fee:
        billing.set_visit_line(ctx, appointment_id, service_id, fee, "undo_no_show", only_from=0)  # the fee is back
    deltas = Deltas().doctor_day(day, doctor_id, no_shows=-1).clinic_day(day)
    deltas.write_doctors(ctx)
    return deltas.write_clinics(ctx)[day]


def _unchanged(ctx, appointment_id, action, target, today, with_payment=False):
    current = Appointment.objects.filter(clinic_id=ctx.clinic_id, id=appointment_id).values_list("status", "day").first()
    if current is None:
        raise rules.not_found("appointment")
    status, day = current
    targets = target if isinstance(target, tuple) else (target,)
    if status in targets and action == "finish" and with_payment:
        # Someone else finished it first. This request's cash was never recorded, so it must not look like
        # a success: the desk takes the payment on the done visit instead (chief judge M1, R2; design 5.2).
        from .selectors import appointment as appointment_row

        raise ApiError("already_finished", _("The visit was already finished. Nothing was collected."), 409,
                       fields={"status": status}, extra={"appointment": appointment_row(ctx, appointment_id)})
    if status in targets:
        return None  # already done: a harmless repeat, nothing changes
    if action == "arrive" and status == "booked" and day != today:
        raise _not_today()
    raise ApiError("status_changed", _("This visit's status changed meanwhile."), 409, fields={"status": status})


def _recheck_follow_up(ctx, appointment):
    """The follow-up rule is checked again at arrival. A plan session keeps its own visit fee rule (visit_price).

    The fee changes while the visit has no invoice, or while its invoice holds nothing but a booking deposit:
    then the visit's line changes with it (billing.set_visit_line), or neither does."""
    found = sql.row(ctx, f"""
        SELECT s.kind, s.price, fu.price, fu.is_active, {rules.history_sql("%s")},
               EXISTS (SELECT 1 FROM "billing_invoice" i WHERE i.clinic_id = %s AND i.appointment_id = %s AND i.status = 'open')
          FROM "clinics_service" s
          LEFT JOIN "clinics_service" fu ON fu.clinic_id = s.clinic_id AND fu.id = s.follow_up_service_id
         WHERE s.clinic_id = %s AND s.id = %s""", [
        *_with_patient(rules.history_params(ctx, appointment["doctor_id"], appointment["day"],
                                            rules.follow_up_window(ctx.clinic), appointment["id"]), appointment["patient_id"]),
        ctx.clinic_id, appointment["id"], ctx.clinic_id, appointment["service_id"]])
    kind, price, follow_price, follow_active, eligible, _arrived, has_invoice = found
    service = rules.ServiceInfo(appointment["service_id"], "", kind, price, follow_price if follow_active else None)
    fee, is_follow_up = rules.fee_for(service, eligible, appointment.get("visit_price") or "")
    fee *= appointment.get("qty") or 1
    if (fee, is_follow_up) != (appointment["fee"], appointment["is_follow_up"]):
        if has_invoice:
            invoice = billing.open_invoice(ctx, appointment["id"])
            only_deposit = invoice is not None and 0 < invoice["deposit"] and invoice["paid"] <= invoice["deposit"]
            if fee != appointment["fee"] and not (only_deposit and billing.set_visit_line(
                    ctx, appointment["id"], appointment["service_id"], fee, "arrival", only_from=appointment["fee"])):
                return
            if fee == appointment["fee"] and not only_deposit:
                return
        sql.run(ctx, 'UPDATE "scheduling_appointment" SET fee = %s, is_follow_up = %s WHERE clinic_id = %s AND id = %s',
                [fee, is_follow_up, ctx.clinic_id, appointment["id"]])


def _with_patient(params, patient_id):
    """history_sql("%s") takes the patient id as a parameter after each clinic id."""
    clinic_id, doctor_id, start, day, exclude, clinic_id2, exclude2 = params
    return [clinic_id, patient_id, doctor_id, start, day, exclude, clinic_id2, patient_id, exclude2]


def reschedule(ctx, appointment_id, seen, doctor_id, day, starts_at, extra):
    """Move a booked visit (design 5.2). Returns the version of the new day, or None when nothing changed."""
    rules.guard(ctx, appointment_id)
    if seen != "booked":
        raise ApiError("invalid_transition", _("This action doesn't apply to a visit in that status."), 400,
                       fields={"status": "invalid"})
    clinic = ctx.clinic
    current = sql.row(ctx, 'SELECT status, day, doctor_id, starts_at, is_extra, queue_no, section_id '
                           'FROM "scheduling_appointment" WHERE clinic_id = %s AND id = %s FOR NO KEY UPDATE',
                      [ctx.clinic_id, appointment_id])
    if current is None:
        raise rules.not_found("appointment")
    status, old_day, old_doctor, old_starts, old_extra, queue_no, section_id = current
    if status != "booked":
        raise ApiError("status_changed", _("This visit's status changed meanwhile."), 409, fields={"status": status})
    rules.check_day(clinic, day)
    rules.require_doctor(ctx, doctor_id)
    doctor = rules.doctor_day(ctx, doctor_id, day, lock=True)
    new_starts, new_ends = rules.resolve_slot(clinic, day, doctor, starts_at, section_id)  # the visit keeps its section
    is_extra = bool(extra and new_starts is not None)
    if (doctor_id, day, new_starts, is_extra) == (old_doctor, old_day, old_starts, old_extra):
        return None
    if (doctor_id, day) != (old_doctor, old_day):
        # Moved to another doctor or day: not onto a second open visit of this patient (Saged, 2026-11-17).
        patient = sql.dicts(ctx, 'SELECT p.id, p.full_name FROM "patients_patient" p JOIN "scheduling_appointment" a '
                                 "ON a.clinic_id = p.clinic_id AND a.patient_id = p.id WHERE a.clinic_id = %s AND a.id = %s",
                            [ctx.clinic_id, appointment_id])[0]
        check_same_day(ctx, patient, doctor_id, day, confirm=True, exclude_id=appointment_id)
    deltas = Deltas()
    if (doctor_id, day) == (old_doctor, old_day):
        deltas.doctor_day(day, doctor_id)  # lock only: slot moves on one doctor-day go one at a time; the turn stays
        deltas.write_doctors(ctx)
    else:
        deltas.doctor_day(old_day, old_doctor, booked=-1)
        deltas.take_turn(day, doctor_id, doctor.cap)  # the old number stays a gap; last_queue_no never goes down
        queue_no = deltas.write_doctors(ctx)
    try:
        sql.run(ctx, 'UPDATE "scheduling_appointment" SET day = %s, doctor_id = %s, queue_no = %s, starts_at = %s, '
                     "ends_at = %s, is_extra = %s, updated_at = %s, updated_by_id = %s WHERE clinic_id = %s AND id = %s",
                [day, doctor_id, queue_no, new_starts, new_ends, is_extra, timezone.now(), ctx.membership_id,
                 ctx.clinic_id, appointment_id])
    except IntegrityError as error:
        if _constraint(error) in SLOT_CONSTRAINTS:
            raise _slot_taken()
        raise
    deltas.clinic_day(old_day).clinic_day(day)
    return deltas.write_clinics(ctx)[day]


def current_version(ctx, appointment_id):
    day = Appointment.objects.filter(clinic_id=ctx.clinic_id, id=appointment_id).values_list("day", flat=True).first()
    if day is None:
        raise rules.not_found("appointment")
    return summaries_read.clinic_day(ctx, day)["version"]



# --- A walk-in (design G5) -------------------------------------------------------------------------------------

WAITING = ("waiting", "with_assistant", "ready")


def _walk_in_doctor(ctx, today, section_id=None):
    """The doctor a walk-in goes to when the desk doesn't pick one: a doctor takes it themselves (they book only for
    themselves); else the only working doctor, else the one with the fewest people waiting (then the lowest id).
    Only doctors working today in the member's sections (and the given section) count. One statement."""
    scope = rules.section_scope(ctx)
    rows = sql.rows(ctx, """
        SELECT m.id, array_agg(DISTINCT s.section_id),
               (SELECT count(*) FROM "scheduling_appointment" a WHERE a.clinic_id = m.clinic_id AND a.day = %s
                   AND a.doctor_id = m.id AND a.status = ANY(%s))
          FROM "clinics_membership" m
          JOIN "clinics_schedule" s ON s.clinic_id = m.clinic_id AND s.doctor_id = m.id AND s.weekday = %s
         WHERE m.clinic_id = %s AND m.is_active AND m.role IN ('owner', 'doctor')
         GROUP BY m.id, m.clinic_id ORDER BY m.id""", [today, list(WAITING), today.weekday(), ctx.clinic_id])
    working = []
    for doctor_id, sections, waiting in rows:
        sections = [s for s in sections if (scope is None or s in scope) and (section_id is None or s == section_id)]
        if sections:
            working.append((waiting, doctor_id, sections))
    if rules.own_doctor(ctx) is not None:
        working = [item for item in working if item[1] == ctx.membership_id]
    if not working:
        raise ApiError("no_doctor", _("No doctor works today."), 409, fields={"doctor_id": "none_working"})
    _waiting, doctor_id, sections = min(working)
    return doctor_id, sections


def _walk_in_service(ctx, doctor_id, today, sections=None):
    """The first consultation of the doctor's first section today (by session start), among `sections`."""
    found = sql.row(ctx, """
        SELECT sv.id FROM "clinics_schedule" s
          JOIN "clinics_service" sv ON sv.clinic_id = s.clinic_id AND sv.section_id = s.section_id
         WHERE s.clinic_id = %s AND s.doctor_id = %s AND s.weekday = %s AND sv.kind = 'consultation' AND sv.is_active
           AND (%s::bigint[] IS NULL OR s.section_id = ANY(%s::bigint[]))
         ORDER BY s.starts, sv.sort, sv.id LIMIT 1""", [ctx.clinic_id, doctor_id, today.weekday(), sections, sections])
    if found is None:
        if not sql.row(ctx, 'SELECT 1 FROM "clinics_membership" WHERE clinic_id = %s AND id = %s AND is_active '
                            "AND role IN ('owner', 'doctor')", [ctx.clinic_id, doctor_id]):
            raise rules.not_found("doctor")
        raise ApiError("no_service", _("This doctor has no consultation service. Pick the service."), 409,
                       fields={"service_id": "required"})
    return found[0]


def walk_in(ctx, request):
    """POST /appointments/walk-in: today, the next turn, no time, already arrived, with an optional payment.

    `request` has patient_id or new_patient, and optional doctor_id, service_id, section_id, payment and note.
    The defaults are resolved here, then booking runs as for any visit (one transaction, the same lock order).
    Returns (appointment, version, ahead): ahead is how many wait for that doctor before this turn.
    """
    today = business_day(ctx.clinic)
    doctor_id, service_id = request["doctor_id"], request["service_id"]
    scope = rules.section_scope(ctx)
    sections = [request["section_id"]] if request.get("section_id") else (sorted(scope) if scope is not None else None)
    if doctor_id is None:
        section_id = request.get("section_id")
        if service_id is not None:
            found = sql.row(ctx, 'SELECT section_id FROM "clinics_service" WHERE clinic_id = %s AND id = %s',
                            [ctx.clinic_id, service_id])
            if found is None:
                raise rules.not_found("service")
            section_id = found[0]
        doctor_id, sections = _walk_in_doctor(ctx, today, section_id)
    if service_id is None:
        service_id = _walk_in_service(ctx, doctor_id, today, sections)
    appointment, version = book(ctx, {
        "patient_id": request["patient_id"], "new_patient": request["new_patient"], "doctor_id": doctor_id,
        "service_id": service_id, "day": today, "starts_at": None, "extra": False, "arrive": True, "walk_in": True,
        "source": "clinic", "note": request["note"], "payment": request["payment"], "qty": request.get("qty") or 1,
        "confirm_other_doctor": request.get("confirm_other_doctor", False)})
    return appointment, version, waiting_ahead(ctx, appointment)


def waiting_ahead(ctx, appointment):
    return sql.row(ctx, 'SELECT count(*) FROM "scheduling_appointment" WHERE clinic_id = %s AND day = %s '
                        "AND doctor_id = %s AND status = ANY(%s) AND queue_no < %s",
                   [ctx.clinic_id, appointment["day"], appointment["doctor_id"], list(WAITING),
                    appointment["queue_no"]])[0]
