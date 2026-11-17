"""Patient endpoints (design 6.2, M1). Reading a file or its lists writes an access event."""

from django.utils.translation import gettext as _

from audit.models import AuditAccess
from audit.services import record_access
from core import paging
from core.api import Result, api
from core.days import business_day
from core.errors import ApiError
from core.validate import boolean, ident, path_id, text

from scheduling.rules import require_patient

from . import selectors, services, tags


def _not_found():
    return ApiError("not_found", _("This patient wasn't found."), 404)


@api("GET", perm="patients.view")
def search_view(request, ctx, data):
    """?q= (name, phone or file number) and/or ?tag=<id> (F6). With a tag alone, ?after=<patient id> pages."""
    query = request.GET.get("q", "")
    if len(query) > 60:
        raise ApiError("invalid", "q is too long", fields={"q": "too_long"})
    today = business_day(ctx.clinic)
    tag = request.GET.get("tag")
    if not tag:
        return {"items": selectors.search(ctx, query, today)}
    tag_id = ident(tag, "tag")
    if query.strip():
        items = selectors.search(ctx, query, today)
        keep = tags.keep_tagged(ctx, [i["id"] for i in items], tag_id)
        return {"items": [i for i in items if i["id"] in keep]}
    after = request.GET.get("after")
    ids = tags.patients_with_tag(ctx, tag_id, ident(after, "after") if after else None)
    items = selectors.by_ids(ctx, ids, today)
    return {"items": items, "next": str(ids[-1]) if len(ids) == tags.BY_TAG_LIMIT else None}


@api("GET", perm="patients.view")
def by_phone_view(request, ctx, data):
    phone = request.GET.get("phone", "")
    if len(phone) > 20:
        raise ApiError("invalid", "phone is too long", fields={"phone": "too_long"})
    return selectors.by_phone(ctx, phone, business_day(ctx.clinic))


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
    require_patient(ctx, patient_id)  # another doctor's patient is 404 to a doctor
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
    """The patient's payments: money only, refused without money.view (Saged, 2026-11-16)."""
    from billing import visibility
    from billing.selectors import patient_payments

    visibility.require(ctx)
    patient_id = path_id(patient_id, _not_found())
    selectors.exists(ctx, patient_id)
    record_access(ctx, patient_id, AuditAccess.Action.HISTORY)
    return patient_payments(ctx, patient_id, paging.limit(request), paging.time_cursor(request))



# --- Tags (F6) -------------------------------------------------------------------------------------------------


def _tag_id(value):
    return path_id(value, tags.not_found())


def _replay_tag(ctx, stored):
    return {"tag": tags.get_tag(ctx, stored["target_id"])}


@api("GET", perm="patients.view")
def tags_view(request, ctx, data):
    return {"items": tags.tags(ctx, include_inactive=request.GET.get("all") == "1")}


@api("POST", perm="patients.tags", replay=_replay_tag)
def create_tag_view(request, ctx, data):
    """POST {name, color?}."""
    tag = tags.create_tag(ctx, text(data, "name", 40), tags.check_color(data.get("color", "") or ""),
                          text(data, "name_en", 40, required=False))
    return Result({"tag": tag}, status=201, target_type="tag", target_id=tag["id"])


@api("PATCH", perm="patients.tags", replay=_replay_tag)
def update_tag_view(request, ctx, data, tag_id):
    """PATCH {name?, color?, is_active?, sort?}."""
    from core.numbers import parse_int

    tag_id = _tag_id(tag_id)
    values = {}
    if "name" in data:
        values["name"] = text(data, "name", 40)
    if "name_en" in data:
        values["name_en"] = text(data, "name_en", 40, required=False)
    if "color" in data:
        values["color"] = tags.check_color(data.get("color") or "")
    if "is_active" in data:
        values["is_active"] = boolean(data, "is_active")
    if "sort" in data:
        values["sort"] = parse_int(data.get("sort"), "sort", 0, 32767)
    tag = tags.update_tag(ctx, tag_id, values)
    return Result({"tag": tag}, target_type="tag", target_id=tag_id)


def _replay_patient_tags(ctx, stored):
    return {"tags": tags.patient_tags(ctx, stored["target_id"])}


@api("GET", perm="patients.view")
def patient_tags_view(request, ctx, data, patient_id):
    patient_id = path_id(patient_id, _not_found())
    selectors.exists(ctx, patient_id)
    return {"tags": tags.patient_tags(ctx, patient_id)}


@api("PUT", perm="patients.edit", replay=_replay_patient_tags)
def set_patient_tags_view(request, ctx, data, patient_id):
    """PUT {tag_ids: [...]}: the patient's tags, absolute."""
    patient_id = path_id(patient_id, _not_found())
    require_patient(ctx, patient_id)
    raw = data.get("tag_ids")
    if not isinstance(raw, list):
        raise ApiError("invalid", "tag_ids must be a list", fields={"tag_ids": "list"})
    return Result({"tags": tags.set_patient_tags(ctx, patient_id, [ident(t, "tag_ids") for t in raw])},
                  target_type="patient", target_id=patient_id)
