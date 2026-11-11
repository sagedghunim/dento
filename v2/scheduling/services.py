"""Booking, status changes and reschedule (design 5.1, 5.2). Inside the request's shard transaction.

Lock order (design 5.0): request key → appointment → invoice → counters (patient_file,
invoice) → doctor-days (one statement) → clinic-days (one statement, last).
"""

from django.db import IntegrityError
from django.utils import timezone
from django.utils.translation import gettext as _

from audit.services import record_change
from billing import services as billing
from clinics.services import take_numbers
from core import sql
from core.days import business_day
from core.errors import ApiError
from patients import services as patients
from summaries import selectors as summaries_read
from summaries.services import Deltas

from . import rules
from .models import Appointment

# action: (from, to, timestamp column)
TRANSITIONS = {
    "arrive": ("booked", "waiting", "arrived_at"),
    "call": ("waiting", "in_room", "called_at"),
    "finish": ("in_room", "done", "finished_at"),
    "cancel": ("booked", "cancelled", "cancelled_at"),
}
# Undo on the desk (design D4, requirements 7.8): only from the status right after the step, clearing its time.
UNDO = {
    "undo_arrive": ("waiting", "booked", "arrived_at"),
    "undo_call": ("in_room", "waiting", "called_at"),
}


def _slot_taken():
    return ApiError("slot_taken", _("This time is already booked."), 409, fields={"starts_at": "taken"})


def _constraint(error):
    cause = error.__cause__
    diag = getattr(cause, "diag", None)
    return getattr(diag, "constraint_name", None)


def book(ctx, request):
    """POST /appointments (design 5.1). `request` is the validated body (see views.book_view).

    Returns (appointment payload, version of the appointment's day).
    """
    clinic = ctx.clinic
    day = request["day"]
    today = rules.check_day(clinic, day)
    payment = request["payment"]
    if request["new_patient"] is not None:
        ctx.require("patients.edit")
    if payment is not None:
        ctx.require("billing.collect")
        if day != today:
            raise ApiError("payment_not_today", _("A payment with a booking is only for today. Book, then take the payment."),
                           fields={"payment": "not_today"})
    if request["arrive"]:
        ctx.require("appointments.queue")
        if day != today:
            raise _not_today()

    doctor, service = rules.doctor_day_and_service(ctx, request["doctor_id"], request["service_id"], day)
    # The section comes from the service; the doctor must have a session in it that day (design D1).
    starts_at, ends_at = rules.resolve_slot(clinic, day, doctor, request["starts_at"], service.section_id)
    is_extra = bool(request["extra"] and starts_at is not None)

    if request["patient_id"] is not None:
        found = sql.row(ctx, f'SELECT p.id, p.full_name, p.phone, p.file_no, {rules.history_sql("p.id")} '
                             'FROM "patients_patient" p WHERE p.clinic_id = %s AND p.id = %s',
                        [*rules.history_params(ctx, doctor.doctor_id, day, rules.follow_up_window(clinic)),
                         ctx.clinic_id, request["patient_id"]])
        if found is None:
            raise rules.not_found("patient")
        patient = {"id": found[0], "full_name": found[1], "phone": found[2], "file_no": found[3]}
        eligible, has_arrived = found[4], found[5]
        fields = None
    else:
        fields = patients.clean_fields(request["new_patient"], today)
        eligible, has_arrived = False, False
    fee, is_follow_up = rules.fee_for(service, eligible)
    if payment is not None and payment["amount"] > fee:
        raise ApiError("over_balance", _("The amount is more than what is owed."), 409, fields={"amount": "over_balance"})

    # Writes, in the global lock order.
    if fields is not None:
        patient = patients.create(ctx, fields)  # counter patient_file
    invoice_number = take_numbers(ctx, "invoice")[0] if payment is not None else None  # kept for ensure_invoice
    deltas = Deltas().take_turn(day, doctor.doctor_id, doctor.cap)
    deltas.clinic_day(day)
    if payment is not None:
        deltas.doctor_day(today, doctor.doctor_id, collected=payment["amount"])
        deltas.clinic_day(today, collected=payment["amount"])
    queue_no = deltas.write_doctors(ctx)

    now = timezone.now()
    try:
        appointment = Appointment.objects.create(
            clinic_id=ctx.clinic_id, patient_id=patient["id"], doctor_id=doctor.doctor_id, service_id=service.id,
            section_id=service.section_id, day=day, queue_no=queue_no, starts_at=starts_at, ends_at=ends_at,
            is_extra=is_extra,
            status="waiting" if request["arrive"] else "booked", source=request["source"], fee=fee,
            is_follow_up=is_follow_up, is_new=not has_arrived, note=request["note"],
            arrived_at=now if request["arrive"] else None, created_at=now, created_by_id=ctx.membership_id,
            updated_at=now, updated_by_id=ctx.membership_id)
    except IntegrityError as error:
        if _constraint(error) == "appointment_slot_uniq":
            raise _slot_taken()
        raise

    invoice = None
    if payment is not None:
        locked = {"id": appointment.id, "status": appointment.status, "day": day, "doctor_id": doctor.doctor_id,
                  "patient_id": patient["id"], "service_id": service.id, "fee": fee, "service_name": service.name}
        # The summary changes are already in `deltas` above, so collect into a throwaway.
        _payment, invoice = billing.collect(ctx, locked, payment["amount"], payment["method"], Deltas(),
                                            invoice_number=invoice_number, today=today)
    version = deltas.write_clinics(ctx)[day]

    from .selectors import payload

    body = payload(clinic, {
        "id": appointment.id, "day": day, "queue_no": queue_no, "status": appointment.status, "starts_at": starts_at,
        "ends_at": ends_at, "is_extra": is_extra, "source": request["source"], "fee": fee, "is_follow_up": is_follow_up,
        "is_new": not has_arrived, "auto_closed": False, "note": request["note"], "arrived_at": appointment.arrived_at,
        "called_at": None, "finished_at": None, "cancelled_at": None, "cancel_reason": "", "doctor_id": doctor.doctor_id,
        "patient_id": patient["id"], "patient_name": patient["full_name"], "patient_phone": patient["phone"],
        "patient_file_no": patient["file_no"], "service_id": service.id, "service_name": service.name,
        "section_id": service.section_id,
        "invoice_id": invoice["id"] if invoice else None, "invoice_number": invoice["number"] if invoice else None,
        "invoice_total": invoice["total"] if invoice else None, "invoice_paid": invoice["paid"] if invoice else None,
    })
    return body, version


def _not_today():
    return ApiError("not_today", _("A visit can be marked arrived only on its own day."), 409, fields={"day": "not_today"})


def change_status(ctx, appointment_id, action, seen, *, reason="", payment=None):
    """arrive, call, finish (optionally with a payment) and cancel (design 5.2).

    The client sends the status it saw. One conditional UPDATE decides; a repeat of a change
    that already happened is a harmless 200. Returns the version of the appointment's day.
    """
    source, target, column = TRANSITIONS[action]
    if seen != source:
        raise ApiError("invalid_transition", _("This action doesn't apply to a visit in that status."), 400,
                       fields={"status": "invalid"})
    clinic = ctx.clinic
    today = business_day(clinic)
    now = timezone.now()
    extra_set, extra_params, extra_where = "", [], ""
    if action == "cancel":
        extra_set, extra_params = ", cancel_reason = %s", [reason]
    if action == "arrive":
        extra_where = " AND day = %s"  # arrive is allowed only on the visit's business day
    params = [target, now, now, ctx.membership_id, *extra_params, ctx.clinic_id, appointment_id, source]
    if action == "arrive":
        params.append(today)
    changed = sql.row(ctx, f'UPDATE "scheduling_appointment" SET status = %s, {column} = %s, updated_at = %s, '
                           f"updated_by_id = %s{extra_set} WHERE clinic_id = %s AND id = %s AND status = %s{extra_where} "
                           "RETURNING id, status, day, doctor_id, patient_id, service_id, fee, is_follow_up, "
                           '(SELECT name FROM "clinics_service" s WHERE s.clinic_id = "scheduling_appointment".clinic_id '
                           'AND s.id = "scheduling_appointment".service_id)', params)
    if changed is None:
        return _unchanged(ctx, appointment_id, action, target, today)
    appointment = dict(zip(("id", "status", "day", "doctor_id", "patient_id", "service_id", "fee", "is_follow_up",
                            "service_name"), changed))
    day, doctor_id = appointment["day"], appointment["doctor_id"]
    deltas = Deltas().clinic_day(day)
    if action == "arrive":
        _recheck_follow_up(ctx, appointment)
    elif action == "finish":
        deltas.doctor_day(day, doctor_id, done=1).clinic_day(day, done=1)
        if payment is not None:
            ctx.require("billing.collect")
            billing.collect(ctx, appointment, payment["amount"], payment["method"], deltas, today=today)
        else:
            billing.ensure_invoice(ctx, appointment)  # design 5.3: finish ensures the invoice, paid or owed
    elif action == "cancel":
        billing.cancel_invoice(ctx, appointment_id)
        deltas.doctor_day(day, doctor_id, booked=-1)
    deltas.write_doctors(ctx)
    return deltas.write_clinics(ctx)[day]


def undo(ctx, appointment_id, action, seen):
    """undo_arrive (waiting -> booked) and undo_call (in_room -> waiting), design D4.

    One conditional UPDATE decides, so an undo racing a call or a finish takes effect only if it
    gets there first; the loser sees 409 status_changed. The turn number stays. Only Today's version
    changes (design 5.3: arrive, undo and call change no counter). Each undo goes to the change log.
    Returns the version of the appointment's day, or None for a harmless repeat.
    """
    source, target, column = UNDO[action]
    if seen != source:
        raise ApiError("invalid_transition", _("This action doesn't apply to a visit in that status."), 400,
                       fields={"status": "invalid"})
    changed = sql.row(ctx, f'UPDATE "scheduling_appointment" SET status = %s, {column} = NULL, updated_at = %s, '
                           "updated_by_id = %s WHERE clinic_id = %s AND id = %s AND status = %s "
                           f"RETURNING day, OLD.{column}",
                      [target, timezone.now(), ctx.membership_id, ctx.clinic_id, appointment_id, source])
    if changed is None:
        return _unchanged(ctx, appointment_id, action, target, None)
    day, undone_at = changed
    record_change(ctx, f"appointment.{action}", "appointment", appointment_id,
                  before={"status": source, column: undone_at.isoformat()}, after={"status": target, column: None})
    return Deltas().clinic_day(day).write_clinics(ctx)[day]


def _unchanged(ctx, appointment_id, action, target, today):
    current = Appointment.objects.filter(clinic_id=ctx.clinic_id, id=appointment_id).values_list("status", "day").first()
    if current is None:
        raise rules.not_found("appointment")
    status, day = current
    if status == target:
        return None  # already done: a harmless repeat, nothing changes
    if action == "arrive" and status == "booked" and day != today:
        raise _not_today()
    raise ApiError("status_changed", _("This visit's status changed meanwhile."), 409, fields={"status": status})


def _recheck_follow_up(ctx, appointment):
    """The follow-up rule is checked again at arrival; the fee changes only while the visit has no invoice."""
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
    if has_invoice:
        return
    service = rules.ServiceInfo(appointment["service_id"], "", kind, price, follow_price if follow_active else None)
    fee, is_follow_up = rules.fee_for(service, eligible)
    if (fee, is_follow_up) != (appointment["fee"], appointment["is_follow_up"]):
        sql.run(ctx, 'UPDATE "scheduling_appointment" SET fee = %s, is_follow_up = %s WHERE clinic_id = %s AND id = %s',
                [fee, is_follow_up, ctx.clinic_id, appointment["id"]])


def _with_patient(params, patient_id):
    """history_sql("%s") takes the patient id as a parameter after each clinic id."""
    clinic_id, doctor_id, start, day, exclude, clinic_id2, exclude2 = params
    return [clinic_id, patient_id, doctor_id, start, day, exclude, clinic_id2, patient_id, exclude2]


def reschedule(ctx, appointment_id, seen, doctor_id, day, starts_at, extra):
    """Move a booked visit (design 5.2). Returns the version of the new day, or None when nothing changed."""
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
    doctor = rules.doctor_day(ctx, doctor_id, day)
    new_starts, new_ends = rules.resolve_slot(clinic, day, doctor, starts_at, section_id)  # the visit keeps its section
    is_extra = bool(extra and new_starts is not None)
    if (doctor_id, day, new_starts, is_extra) == (old_doctor, old_day, old_starts, old_extra):
        return None
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
        if _constraint(error) == "appointment_slot_uniq":
            raise _slot_taken()
        raise
    deltas.clinic_day(old_day).clinic_day(day)
    return deltas.write_clinics(ctx)[day]


def current_version(ctx, appointment_id):
    day = Appointment.objects.filter(clinic_id=ctx.clinic_id, id=appointment_id).values_list("day", flat=True).first()
    if day is None:
        raise rules.not_found("appointment")
    return summaries_read.clinic_day(ctx, day)["version"]

