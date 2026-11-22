"""Appointment endpoints and Today's JSON (design 6.2, 5.11). Views validate, check codes and call one service."""

from django.conf import settings
from django.utils.translation import gettext as _

from core import validate
from core.api import Result, api
from core.days import business_day
from core.errors import ApiError
from core.numbers import parse_money

from . import rules, selectors, services
from .models import STATUSES

QUEUE_CODES = ("appointments.book", "appointments.queue")


def _any_queue_code(ctx):
    if not any(ctx.can(code) for code in QUEUE_CODES):
        ctx.require(QUEUE_CODES[0])


def _payment(data, name="payment", deposit=False):
    """{amount, method}. With `deposit` (booking only) it may also say "deposit": true: the booking deposit."""
    value = data.get(name)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ApiError("invalid", f"{name} must be an object", fields={name: "object"})
    from billing.models import METHODS

    payment = {"amount": parse_money(value.get("amount"), "amount", low=1),
               "method": validate.choice(value, "method", METHODS)}
    if value.get("deposit") is not None:
        if not deposit:
            raise ApiError("invalid", _("A deposit is taken only with a booking."), fields={f"{name}.deposit": "booking"})
        payment["deposit"] = validate.boolean(value, "deposit")
    return payment


def _appointment_id(value):
    return validate.path_id(value, rules.not_found("appointment"))


# --- Today ---------------------------------------------------------------------------------------------------


def poll_headers():
    """Every Today answer carries Poll-After, refusals included, so operations can slow every screen (design 5.11)."""
    return {"Poll-After": str(settings.POLL_AFTER_SECONDS)}


def _qty(data):
    """How many of a procedure (Saged, 2026-11-19): 1 to 20, 1 when not sent."""
    from .models import MAX_QTY

    value = data.get("qty", 1)
    if value is None:
        return 1
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_QTY:
        raise ApiError("invalid", _("Enter a count from 1 to 20."), fields={"qty": "range"})
    return value


@api("GET", headers=poll_headers)
def today_view(request, ctx, data):
    """Today's queues with an ETag. The version is read before the data; every answer carries Poll-After.

    ?section= keeps one section of a center; it is part of the ETag.
    """
    _any_queue_code(ctx)
    day = selectors.today_day(ctx, request.GET.get("day"))
    section_id = selectors.today_section(request.GET.get("section"))
    state = selectors.today_state(ctx, day)
    etag = selectors.today_etag(ctx, day, state["version"], getattr(request, "LANGUAGE_CODE", ""), section_id)
    headers = {"ETag": etag, **poll_headers()}
    if selectors.etag_matches(request.headers.get("If-None-Match"), etag):
        return Result({}, status=304, headers=headers)
    return Result(selectors.today_data(ctx, day, state, section_id), headers=headers)


# --- Booking -------------------------------------------------------------------------------------------------


def _replay_appointment(ctx, stored):
    return selectors.answer(ctx, stored["target_id"])


@api("POST", perm="appointments.book", replay=_replay_appointment)
def book_view(request, ctx, data):
    patient_id = data.get("patient_id")
    new_patient = data.get("new_patient")
    if (patient_id is None) == (new_patient is None):
        raise ApiError("invalid", _("Pick a patient or enter a new one."), fields={"patient_id": "required"})
    if new_patient is not None and not isinstance(new_patient, dict):
        raise ApiError("invalid", "new_patient must be an object", fields={"new_patient": "object"})
    starts_at = data.get("starts_at")
    body = {
        "patient_id": validate.ident(patient_id, "patient_id") if patient_id is not None else None,
        "new_patient": new_patient,
        "doctor_id": validate.ident(data.get("doctor_id"), "doctor_id"),
        "service_id": validate.ident(data.get("service_id"), "service_id"),
        "day": validate.iso_day(data["day"]) if data.get("day") else business_day(ctx.clinic),
        "starts_at": rules.parse_instant(starts_at) if starts_at else None,
        "extra": validate.boolean(data, "extra"),
        "arrive": validate.boolean(data, "arrive"),
        "source": validate.choice({"source": data.get("source") or "clinic"}, "source",
                                  ["clinic", "phone", "online", "whatsapp"]),
        "note": validate.text(data, "note", 500, required=False),
        "payment": _payment(data, deposit=True),
        # The follow-up this booking is for (clinical.followups.claim), linked in the same transaction.
        "follow_up_id": (validate.ident(data.get("follow_up_id"), "follow_up_id")
                         if data.get("follow_up_id") is not None else None),
        # The desk saw "has a booking that day with another doctor" and books anyway (Saged, 2026-11-17).
        "confirm_other_doctor": validate.boolean(data, "confirm_other_doctor"),
        "qty": _qty(data),
    }
    appointment, version = services.book(ctx, body)
    from billing import visibility

    visibility.appointment(ctx, appointment)
    return Result({"appointment": appointment, "version": version}, status=201, target_type="appointment",
                  target_id=appointment["id"])


@api("GET", perm="appointments.book")
def quote_view(request, ctx, data):
    params = request.GET
    day = validate.iso_day(params["day"]) if params.get("day") else business_day(ctx.clinic)
    rules.check_day(ctx.clinic, day)  # the same days booking accepts (chief judge M1, R6)
    patient = params.get("patient")
    follow_up = params.get("follow_up")  # the plan session being booked: its own visit fee rule
    return selectors.quote(ctx, validate.ident(params.get("doctor"), "doctor"),
                           validate.ident(params.get("service"), "service"), day,
                           validate.ident(patient, "patient") if patient else None,
                           validate.ident(follow_up, "follow_up") if follow_up else None)


@api("GET")
def appointment_view(request, ctx, data, appointment_id):
    _any_queue_code(ctx)
    appointment_id = _appointment_id(appointment_id)
    rules.guard(ctx, appointment_id)  # another section's visit is 404 for a limited assistant (design G1)
    return selectors.answer(ctx, appointment_id)


@api("GET", perm="appointments.book")
def slots_view(request, ctx, data, doctor_id):
    doctor_id = validate.path_id(doctor_id, rules.not_found("doctor"))
    day = validate.iso_day(request.GET["day"]) if request.GET.get("day") else business_day(ctx.clinic)
    section = request.GET.get("section")
    return selectors.slots(ctx, doctor_id, day, validate.ident(section, "section") if section else None)


@api("GET", perm="appointments.book")
def month_view(request, ctx, data, doctor_id):
    """GET /doctors/{id}/month?month=YYYY-MM[&section=]: the Book calendar's working days and free times."""
    import datetime as dt

    doctor_id = validate.path_id(doctor_id, rules.not_found("doctor"))
    raw = request.GET.get("month") or ""
    try:
        first = dt.date.fromisoformat(raw + "-01") if len(raw) == 7 else None
    except ValueError:
        first = None
    if first is None or not validate.DAY_MIN <= first <= validate.DAY_MAX:
        raise ApiError("invalid", "month must be YYYY-MM", fields={"month": "invalid"})
    section = request.GET.get("section")
    return selectors.month(ctx, doctor_id, first, validate.ident(section, "section") if section else None)


# --- Status changes ------------------------------------------------------------------------------------------


def _status_view(action, perm):
    def view(request, ctx, data, appointment_id):
        appointment_id = _appointment_id(appointment_id)
        seen = validate.choice(data, "status", STATUSES)
        payment = _payment(data) if action == "finish" else None
        if payment is not None:
            ctx.require("billing.collect")
        reason = validate.text(data, "reason", 200, required=False) if action == "cancel" else ""
        version = services.change_status(ctx, appointment_id, action, seen, reason=reason, payment=payment)
        return Result(selectors.answer(ctx, appointment_id, version), target_type="appointment",
                      target_id=appointment_id)

    view.__name__ = view.__qualname__ = f"{action}_view"  # before @api, which names the request-key endpoint
    return api("POST", perm=perm, replay=_replay_appointment)(view)


arrive_view = _status_view("arrive", "appointments.queue")
assist_view = _status_view("assist", "appointments.queue")
ready_view = _status_view("ready", "appointments.queue")
call_view = _status_view("call", "appointments.queue")
def _next_visit(data):
    value = data.get("next_visit")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ApiError("invalid", "next_visit must be an object", fields={"next_visit": "object"})
    starts_at = value.get("starts_at")
    return {"doctor_id": validate.ident(value.get("doctor_id"), "next_visit.doctor_id"),
            "service_id": validate.ident(value.get("service_id"), "next_visit.service_id"),
            "day": validate.iso_day(value.get("day"), "next_visit.day"),
            "starts_at": rules.parse_instant(starts_at, "next_visit.starts_at") if starts_at else None,
            "extra": validate.boolean(value, "extra"), "note": validate.text(value, "note", 500, required=False)}


def _finish_answer(ctx, appointment_id, version, next_appointment=None):
    """The finish dialog's answer (design E3): the visit, its invoice lines, total, paid and remaining, the
    patient's balance across visits, and the next visit if one was booked."""
    from billing import selectors as billing_read

    from billing import visibility

    body = selectors.answer(ctx, appointment_id, version)
    seen = visibility.sees(ctx)  # without money.view: no invoice and no balance (Saged, 2026-11-16)
    body["invoice"] = billing_read.visit_invoice(ctx, appointment_id) if seen else None
    body["balance"] = billing_read.patient_balance(ctx, body["appointment"]["patient"]["id"]) if seen else None
    body["next_appointment"] = next_appointment
    return body


def _replay_finish(ctx, stored):
    return _finish_answer(ctx, stored["target_id"], None)


@api("POST", perm="appointments.queue", replay=_replay_finish)
def finish_view(request, ctx, data, appointment_id):
    """finish {status, payment?, earlier?: {amount, method}, next_visit?: {doctor_id, service_id, day, starts_at?}}.

    One request: the visit's payment, the patient's earlier balance (oldest invoices first) and the next
    booking all commit together or not at all. A finish after someone else's is 409 already_finished.
    """
    appointment_id = _appointment_id(appointment_id)
    seen = validate.choice(data, "status", STATUSES)
    payment, earlier = _payment(data), _payment(data, "earlier")
    if payment is not None or earlier is not None:
        ctx.require("billing.collect")
    outcome = {}
    version = services.change_status(ctx, appointment_id, "finish", seen, payment=payment, earlier=earlier,
                                     next_visit=_next_visit(data), outcome=outcome)
    return Result(_finish_answer(ctx, appointment_id, version, outcome.get("next_appointment")),
                  target_type="appointment", target_id=appointment_id)
cancel_view = _status_view("cancel", "appointments.book")


def _undo_view(action):
    """undo-arrive takes {"status": "waiting"}, undo-call {"status": "in_room"} (design D4)."""

    def view(request, ctx, data, appointment_id):
        appointment_id = _appointment_id(appointment_id)
        seen = validate.choice(data, "status", STATUSES)
        version = services.undo(ctx, appointment_id, action, seen)
        return Result(selectors.answer(ctx, appointment_id, version), target_type="appointment",
                      target_id=appointment_id)

    view.__name__ = view.__qualname__ = f"{action}_view"
    return api("POST", perm="appointments.queue", replay=_replay_appointment)(view)


undo_arrive_view = _undo_view("undo_arrive")
wait_view = _undo_view("wait")  # the doctor's "wait" to the desk's "ready" (Saged, 2026-11-22)
undo_assist_view = _undo_view("undo_assist")
undo_ready_view = _undo_view("undo_ready")
undo_call_view = _undo_view("undo_call")


def _no_show_view(undo):
    """no-show takes {"status": "booked"}, undo-no-show {"status": "no_show"} (desk review 7)."""

    def view(request, ctx, data, appointment_id):
        appointment_id = _appointment_id(appointment_id)
        seen = validate.choice(data, "status", STATUSES)
        action = services.undo_no_show if undo else services.mark_no_show
        version = action(ctx, appointment_id, seen)
        return Result(selectors.answer(ctx, appointment_id, version), target_type="appointment",
                      target_id=appointment_id)

    view.__name__ = view.__qualname__ = "undo_no_show_view" if undo else "no_show_view"
    return api("POST", perm="appointments.queue", replay=_replay_appointment)(view)


no_show_view = _no_show_view(False)
undo_no_show_view = _no_show_view(True)


@api("POST", perm="appointments.book", replay=_replay_appointment)
def reschedule_view(request, ctx, data, appointment_id):
    appointment_id = _appointment_id(appointment_id)
    seen = validate.choice(data, "status", STATUSES)
    starts_at = data.get("starts_at")
    version = services.reschedule(
        ctx, appointment_id, seen, validate.ident(data.get("doctor_id"), "doctor_id"), validate.iso_day(data.get("day")),
        rules.parse_instant(starts_at) if starts_at else None, validate.boolean(data, "extra"))
    return Result(selectors.answer(ctx, appointment_id, version), target_type="appointment", target_id=appointment_id)


# --- Walk-in (design G5) ---------------------------------------------------------------------------------------


def _walk_in_answer(appointment, version, ahead):
    return {"appointment": appointment, "version": version, "turn": appointment["queue_no"], "ahead": ahead}


def _replay_walk_in(ctx, stored):
    body = selectors.answer(ctx, stored["target_id"])
    return _walk_in_answer(body["appointment"], body["version"], services.waiting_ahead(ctx, body["appointment"]))


def _optional_id(data, name):
    return validate.ident(data[name], name) if data.get(name) is not None else None


@api("POST", perm=("appointments.book", "appointments.queue"), replay=_replay_walk_in)
def walk_in_view(request, ctx, data):
    """{patient_id | new_patient, doctor_id?, service_id?, section_id?, payment?, note?}: a patient who walks in now.

    No time is needed; the doctor and the service default on the server (design G5).
    """
    patient_id, new_patient = data.get("patient_id"), data.get("new_patient")
    if (patient_id is None) == (new_patient is None):
        raise ApiError("invalid", _("Pick a patient or enter a new one."), fields={"patient_id": "required"})
    if new_patient is not None and not isinstance(new_patient, dict):
        raise ApiError("invalid", "new_patient must be an object", fields={"new_patient": "object"})
    body = {
        "patient_id": validate.ident(patient_id, "patient_id") if patient_id is not None else None,
        "new_patient": new_patient, "doctor_id": _optional_id(data, "doctor_id"),
        "service_id": _optional_id(data, "service_id"), "section_id": _optional_id(data, "section_id"),
        "note": validate.text(data, "note", 500, required=False), "payment": _payment(data),
        "confirm_other_doctor": validate.boolean(data, "confirm_other_doctor"), "qty": _qty(data),
    }
    appointment, version, ahead = services.walk_in(ctx, body)
    from billing import visibility

    visibility.appointment(ctx, appointment)
    return Result(_walk_in_answer(appointment, version, ahead), status=201, target_type="appointment",
                  target_id=appointment["id"])
