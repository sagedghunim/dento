"""Patient endpoints (design 6.2, M1). Reading a file or its lists writes an access event."""

from django.utils.translation import gettext as _

from audit.models import AuditAccess
from audit.services import record_access
from core import paging
from core.api import Result, api
from core.days import business_day
from core.errors import ApiError
from core.validate import path_id

from . import selectors, services


def _not_found():
    return ApiError("not_found", _("This patient wasn't found."), 404)


@api("GET", perm="patients.view")
def search_view(request, ctx, data):
    query = request.GET.get("q", "")
    if len(query) > 60:
        raise ApiError("invalid", "q is too long", fields={"q": "too_long"})
    return {"items": selectors.search(ctx, query, business_day(ctx.clinic))}


@api("GET", perm="patients.view")
def by_phone_view(request, ctx, data):
    phone = request.GET.get("phone", "")
    if len(phone) > 20:
        raise ApiError("invalid", "phone is too long", fields={"phone": "too_long"})
    return {"items": selectors.by_phone(ctx, phone)}


def _replay_patient(ctx, stored):
    return {"patient": selectors.summary_payload(selectors.get(ctx, stored["target_id"]))}


@api("POST", perm="patients.edit", replay=_replay_patient)
def create_view(request, ctx, data):
    today = business_day(ctx.clinic)
    created = services.create(ctx, services.clean_fields(data, today))
    body = {"id": created["id"], "file_no": created["file_no"], "full_name": created["full_name"],
            "phone": created["phone"]}
    return Result({"patient": body}, status=201, target_type="patient", target_id=created["id"])


@api("GET", perm="patients.view", access="open")
def file_view(request, ctx, data, patient_id):
    patient = selectors.get(ctx, path_id(patient_id, _not_found()))
    record_access(ctx, patient.id, AuditAccess.Action.OPEN)
    return {"patient": selectors.file_payload(patient, business_day(ctx.clinic))}


@api("PATCH", perm="patients.edit", replay=_replay_patient)
def update_view(request, ctx, data, patient_id):
    patient_id = path_id(patient_id, _not_found())
    changes, expected = data.get("changes"), data.get("expected", {})
    if not isinstance(changes, dict) or not changes or not isinstance(expected, dict):
        raise ApiError("invalid", _("Send the changes and the values they replace."))
    unknown = set(changes) - {*services.FIELDS, "age"}
    if unknown:
        raise ApiError("invalid", "Unknown fields: " + ", ".join(sorted(unknown)),
                       fields={name: "unknown" for name in unknown})
    today = business_day(ctx.clinic)
    patient = services.update(ctx, patient_id, services.clean_fields(changes, today, partial=True), expected, today)
    # The summary and what was sent, never the rest of the file: only GET patients/{id} reads it (with an access event).
    return Result({"patient": selectors.summary_payload(patient)}, target_type="patient", target_id=patient.id)


@api("GET", perm="patients.view", access="history")
def visits_view(request, ctx, data, patient_id):
    from scheduling.selectors import patient_visits

    patient_id = path_id(patient_id, _not_found())
    selectors.exists(ctx, patient_id)
    record_access(ctx, patient_id, AuditAccess.Action.HISTORY)
    return patient_visits(ctx, patient_id, paging.limit(request), paging.day_cursor(request))


@api("GET", perm="patients.view", access="history")
def payments_view(request, ctx, data, patient_id):
    from billing.selectors import patient_payments

    patient_id = path_id(patient_id, _not_found())
    selectors.exists(ctx, patient_id)
    record_access(ctx, patient_id, AuditAccess.Action.HISTORY)
    return patient_payments(ctx, patient_id, paging.limit(request), paging.time_cursor(request))

