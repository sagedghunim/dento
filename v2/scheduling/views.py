"""Appointment endpoints and Today's JSON (design 6.2, 5.11). Views validate, check codes and call one service."""

from django.conf import settings
from django.utils.translation import gettext as _

from core import validate
from core.api import Result, api
from core.days import business_day
from core.errors import ApiError
from core.numbers import parse_money

from . import rules, selectors, services

QUEUE_CODES = ("appointments.book", "appointments.queue")


def _any_queue_code(ctx):
    if not any(ctx.can(code) for code in QUEUE_CODES):
        ctx.require(QUEUE_CODES[0])


def _payment(data, name="payment"):
    value = data.get(name)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ApiError("invalid", f"{name} must be an object", fields={name: "object"})
    from billing.models import METHODS

    return {"amount": parse_money(value.get("amount"), "amount", low=1),
            "method": validate.choice(value, "method", METHODS)}


def _appointment_id(value):
    return validate.path_id(value, rules.not_found("appointment"))


# --- Today ---------------------------------------------------------------------------------------------------


@api("GET")
def today_view(request, ctx, data):
    """Today's queues with an ETag. The version is read before the data; every answer carries Poll-After."""
    _any_queue_code(ctx)
    day = selectors.today_day(ctx, request.GET.get("day"))
    state = selectors.today_state(ctx, day)
    etag = selectors.today_etag(ctx, day, state["version"], getattr(request, "LANGUAGE_CODE", ""))
    headers = {"ETag": etag, "Poll-After": str(settings.POLL_AFTER_SECONDS)}
    if selectors.etag_matches(request.headers.get("If-None-Match"), etag):
        return Result({}, status=304, headers=headers)
    return Result(selectors.today_data(ctx, day, state), headers=headers)


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
        "payment": _payment(data),
    }
    appointment, version = services.book(ctx, body)
    return Result({"appointment": appointment, "version": version}, status=201, target_type="appointment",
                  target_id=appointment["id"])


@api("GET", perm="appointments.book")
def quote_view(request, ctx, data):
    params = request.GET
    day = validate.iso_day(params["day"]) if params.get("day") else business_day(ctx.clinic)
    patient = params.get("patient")
    return selectors.quote(ctx, validate.ident(params.get("doctor"), "doctor"),
                           validate.ident(params.get("service"), "service"), day,
                           validate.ident(patient, "patient") if patient else None)


@api("GET")
def appointment_view(request, ctx, data, appointment_id):
    _any_queue_code(ctx)
    return selectors.answer(ctx, _appointment_id(appointment_id))


@api("GET", perm="appointments.book")
def slots_view(request, ctx, data, doctor_id):
    doctor_id = validate.path_id(doctor_id, rules.not_found("doctor"))
    day = validate.iso_day(request.GET["day"]) if request.GET.get("day") else business_day(ctx.clinic)
    return selectors.slots(ctx, doctor_id, day)


# --- Status changes ------------------------------------------------------------------------------------------


def _status_view(action, perm):
    def view(request, ctx, data, appointment_id):
        appointment_id = _appointment_id(appointment_id)
        seen = validate.choice(data, "status", ["booked", "waiting", "in_room", "done", "cancelled", "no_show"])
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
call_view = _status_view("call", "appointments.queue")
finish_view = _status_view("finish", "appointments.queue")
cancel_view = _status_view("cancel", "appointments.book")


@api("POST", perm="appointments.book", replay=_replay_appointment)
def reschedule_view(request, ctx, data, appointment_id):
    appointment_id = _appointment_id(appointment_id)
    seen = validate.choice(data, "status", ["booked", "waiting", "in_room", "done", "cancelled", "no_show"])
    starts_at = data.get("starts_at")
    version = services.reschedule(
        ctx, appointment_id, seen, validate.ident(data.get("doctor_id"), "doctor_id"), validate.iso_day(data.get("day")),
        rules.parse_instant(starts_at) if starts_at else None, validate.boolean(data, "extra"))
    return Result(selectors.answer(ctx, appointment_id, version), target_type="appointment", target_id=appointment_id)
