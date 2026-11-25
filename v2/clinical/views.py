"""The doctor's visit endpoints (design 6.2 Clinical, E3). Views validate, check codes and call one service.

Every view that returns clinical data declares `access` and writes one access event.
"""

from django.http import FileResponse
from django.utils.translation import gettext as _

from audit.models import AuditAccess
from audit.services import record_access
from core import paging, validate
from core.api import Result, api
from core.errors import ApiError
from core.numbers import parse_int, parse_money

from . import selectors, services
from .files import MAX_BYTES


def _id(value, what):
    return validate.path_id(value, services.not_found(what))


def _any(ctx, *codes):
    if not any(ctx.can(code) for code in codes):
        ctx.require(codes[0])


def _if_match(request):
    """The version a PUT replaces (design 5.6): `If-Match: 3` (quotes and W/ are accepted); 0 for none yet."""
    raw = request.headers.get("If-Match", "").strip()
    if raw.startswith("W/"):
        raw = raw[2:]
    raw = raw.strip('"')
    if not raw.isdigit() or len(raw) > 9:
        raise ApiError("precondition_required", _("Reload and try again."), 428, fields={"If-Match": "required"})
    return int(raw)


def _section_param(request):
    raw = request.GET.get("section")
    return validate.ident(raw, "section") if raw else None


def _flag(request, name):
    return request.GET.get(name) in ("1", "true")


def _patient_exists(ctx, patient_id):
    from patients.selectors import exists

    exists(ctx, patient_id)


# --- The visit record ------------------------------------------------------------------------------------------


@api("GET", perm="clinical.view", access="note")
def record_view(request, ctx, data, appointment_id):
    """The visit screen: visit, note, vitals, procedures, prescriptions, attachments, open plan items, balance."""
    body = selectors.visit_record(ctx, _id(appointment_id, "appointment"))
    record_access(ctx, body["patient"]["id"], AuditAccess.Action.NOTE)
    return body


def _visit_patient(ctx, appointment_id):
    return services._appointment(ctx, appointment_id)["patient_id"]


@api("GET", perm="clinical.view", access="note")
def note_view(request, ctx, data, appointment_id):
    appointment_id = _id(appointment_id, "appointment")
    patient_id = _visit_patient(ctx, appointment_id)
    record_access(ctx, patient_id, AuditAccess.Action.NOTE)
    return {"note": selectors.note(ctx, appointment_id)}


def _replay_note(ctx, stored):
    return {"note": selectors.note(ctx, stored["target_id"]), "changed": True}


@api("PUT", perm="visits.notes", replay=_replay_note)
def note_save_view(request, ctx, data, appointment_id):
    """PUT {complaint, diagnosis, notes} with If-Match: <version>. A mismatch is 409 with the current text."""
    appointment_id = _id(appointment_id, "appointment")
    version = _if_match(request)
    values = {name: validate.text(data, name, size, required=False) for name, size in services.NOTE_FIELDS.items()}
    note, changed = services.save_note(ctx, appointment_id, version, values)
    return Result({"note": selectors.note_payload(ctx, note), "changed": changed}, target_type="appointment",
                  target_id=appointment_id)


@api("GET", access="note")
def vitals_view(request, ctx, data, appointment_id):
    _any(ctx, "visits.vitals", "clinical.view")
    appointment_id = _id(appointment_id, "appointment")
    record_access(ctx, _visit_patient(ctx, appointment_id), AuditAccess.Action.NOTE)
    return {"vitals": selectors.vitals(ctx, appointment_id)}


def _replay_vitals(ctx, stored):
    return {"vitals": selectors.vitals(ctx, stored["target_id"]), "changed": True}


@api("PUT", perm="visits.vitals", replay=_replay_vitals)
def vitals_save_view(request, ctx, data, appointment_id):
    """PUT {bp_systolic, bp_diastolic, pulse, temperature, weight, height, spo2, blood_sugar} with If-Match."""
    appointment_id = _id(appointment_id, "appointment")
    version = _if_match(request)
    unknown = set(data) - set(services.VITALS)
    if unknown:
        raise ApiError("invalid", "Unknown fields: " + ", ".join(sorted(unknown)),
                       fields={name: "unknown" for name in unknown})
    row, changed = services.save_vitals(ctx, appointment_id, version, services.clean_vitals(data))
    return Result({"vitals": selectors.vitals_payload(ctx, row), "changed": changed}, target_type="appointment",
                  target_id=appointment_id)


# --- Procedures recorded in a visit ----------------------------------------------------------------------------


def _record_answer(ctx, record_id):
    """The recorded procedure, the visit's invoice, the tooth's state when the procedure set one, and the version."""
    from billing.selectors import visit_invoice
    from scheduling.services import current_version

    from billing import visibility

    record = selectors.visit_procedure(ctx, record_id)
    appointment_id = record["appointment_id"]
    return {"procedure": record, "invoice": visit_invoice(ctx, appointment_id) if visibility.sees(ctx) else None,
            "tooth": selectors.patient_tooth(ctx, record["patient_id"], record["tooth"]) if record["sets_tooth"] else None,
            "version": current_version(ctx, appointment_id)}


def _replay_consult_only(ctx, stored):
    from core import sql

    found = sql.row(ctx, 'SELECT consult_only FROM "scheduling_appointment" WHERE clinic_id = %s AND id = %s',
                    [ctx.clinic_id, stored["target_id"]])
    return {"consult_only": bool(found and found[0])}


@api("POST", perm="procedures.record", replay=_replay_consult_only)
def consult_only_view(request, ctx, data, appointment_id):
    """{on}: the visit was a consultation and nothing more (Saged, 2026-11-24)."""
    appointment_id = validate.path_id(appointment_id, ApiError("not_found", _("This appointment wasn't found."), 404))
    return Result(services.set_consult_only(ctx, appointment_id, validate.boolean(data, "on")),
                  target_type="appointment", target_id=appointment_id)


def _replay_record(ctx, stored):
    return _record_answer(ctx, stored["target_id"])


@api("POST", perm="procedures.record", replay=_replay_record)
def record_procedure_view(request, ctx, data, appointment_id):
    """POST {procedure_id, tooth?, surfaces?, area?, qty?, unit_price?, plan_item_id?, note?, use_package?}."""
    appointment_id = _id(appointment_id, "appointment")
    plan_item = data.get("plan_item_id")
    body = {
        "procedure_id": validate.ident(data.get("procedure_id"), "procedure_id"),
        "tooth": services.clean_tooth(data["tooth"]) if data.get("tooth") not in (None, "") else None,
        "surfaces": services.clean_surfaces(data.get("surfaces")),
        "area": validate.text(data, "area", 20, required=False),
        "qty": parse_int(data.get("qty", 1), "qty", 1, 100),
        "unit_price": parse_money(data["unit_price"], "unit_price") if data.get("unit_price") not in (None, "") else None,
        "plan_item_id": validate.ident(plan_item, "plan_item_id") if plan_item is not None else None,
        "note": validate.text(data, "note", 500, required=False),
        # F7: a prepaid session of this procedure is used unless the doctor says not to (or names a price).
        "use_package": validate.boolean(data, "use_package", True),
    }
    record, _invoice, _tooth, _version = services.record_procedure(ctx, appointment_id, body)
    return Result(_record_answer(ctx, record["id"]), status=201, target_type="visit_procedure", target_id=record["id"])


def _removed_answer(ctx, appointment_id, restored=None):
    """After a removal: the visit's invoice (none without money.view), the tooth put back when the record had set its
    state (`tooth_number`, and `tooth`: its state now, None when it has no row), and the day's version."""
    from billing import visibility
    from billing.selectors import visit_invoice
    from scheduling.services import current_version

    patient_id, number = restored or (None, None)
    return {"invoice": visit_invoice(ctx, appointment_id) if visibility.sees(ctx) else None,
            "tooth_number": number,
            "tooth": selectors.patient_tooth(ctx, patient_id, number) if number is not None else None,
            "version": current_version(ctx, appointment_id)}


def _replay_removed(ctx, stored):
    """The removal's audit row (index audit_change_target) names its visit and the tooth it put back."""
    from audit.models import AuditChange

    details = (AuditChange.objects.filter(clinic_id=ctx.clinic_id, target_type="visit_procedure",
                                          target_id=stored["target_id"], action="visit_procedure.remove")
               .order_by("-at", "-id").values_list("details", flat=True).first())
    if details is None:
        raise services.not_found("procedure")
    return _removed_answer(ctx, details["appointment"], tuple(details["restored"]) if details.get("restored") else None)


@api("DELETE", perm="procedures.record", replay=_replay_removed)
def remove_procedure_view(request, ctx, data, appointment_id, record_id):
    """DELETE: undo a procedure recorded by mistake while the visit is on (Saged, 2026-11-17). Answers the visit's
    invoice and the tooth as they now are; the record's own id is the URL's."""
    appointment_id = _id(appointment_id, "appointment")
    record_id = _id(record_id, "procedure")
    _invoice, restored, _version = services.remove_procedure(ctx, appointment_id, record_id)
    return Result(_removed_answer(ctx, appointment_id, restored), target_type="visit_procedure", target_id=record_id)


# --- Attachments -----------------------------------------------------------------------------------------------


def _replay_attachment(ctx, stored):
    return {"attachment": selectors.attachment_payload(ctx, selectors.attachment(ctx, stored["target_id"]))}


@api("POST", perm="patients.edit", upload=MAX_BYTES, replay=_replay_attachment)
def attachment_add_view(request, ctx, data, patient_id):
    """Multipart: file (an image or a PDF, at most 10 MB), title?, appointment_id?. Answers the row, not the bytes."""
    patient_id = _id(patient_id, "patient")
    appointment = data.get("appointment_id")
    attachment_id = services.add_attachment(
        ctx, patient_id, validate.ident(appointment, "appointment_id") if appointment else None,
        validate.text(data, "title", 200, required=False), request.FILES.get("file"))
    return Result(_replay_attachment(ctx, {"target_id": attachment_id}), status=201, target_type="attachment",
                  target_id=attachment_id)


@api("GET", perm="clinical.view", access="history")
def attachments_view(request, ctx, data, patient_id):
    patient_id = _id(patient_id, "patient")
    _patient_exists(ctx, patient_id)
    record_access(ctx, patient_id, AuditAccess.Action.HISTORY)
    return selectors.attachments(ctx, patient_id, paging.limit(request), paging.time_cursor(request))


@api("GET", perm="clinical.view", access="attachment")
def attachment_file_view(request, ctx, data, attachment_id):
    """The file's bytes, after its row is found under the clinic's row-level security. Writes an access event."""
    from .files import storage

    row = selectors.attachment(ctx, _id(attachment_id, "attachment"))
    record_access(ctx, row["patient_id"], AuditAccess.Action.ATTACHMENT)
    try:
        handle = storage().open(row["storage_key"])
    except FileNotFoundError:
        raise services.not_found("attachment")
    response = FileResponse(handle, content_type=row["content_type"],
                            as_attachment=row["content_type"] == "application/pdf" and _flag(request, "download"),
                            filename=row["file_name"])
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "sandbox; default-src 'none'"
    response["Cache-Control"] = "no-store"
    return response


# --- Timeline, balance, chart and plans ------------------------------------------------------------------------


@api("GET", perm="clinical.view", access="history")
def timeline_view(request, ctx, data, patient_id):
    patient_id = _id(patient_id, "patient")
    _patient_exists(ctx, patient_id)
    record_access(ctx, patient_id, AuditAccess.Action.HISTORY)
    return selectors.timeline(ctx, patient_id, paging.limit(request), selectors.timeline_cursor(request.GET.get("after")))


@api("GET", perm="patients.view", access="history")
def balance_view(request, ctx, data, patient_id):
    """What the patient owes across visits (design E3): open invoices with something left, oldest first.
    Money only: refused without money.view (Saged, 2026-11-16)."""
    from billing import visibility
    from billing.selectors import patient_balance

    visibility.require(ctx)
    patient_id = _id(patient_id, "patient")
    _patient_exists(ctx, patient_id)
    record_access(ctx, patient_id, AuditAccess.Action.HISTORY)
    return {"balance": patient_balance(ctx, patient_id)}


@api("GET", perm="clinical.view", access="history")
def chart_view(request, ctx, data, patient_id):
    patient_id = _id(patient_id, "patient")
    _patient_exists(ctx, patient_id)
    record_access(ctx, patient_id, AuditAccess.Action.HISTORY)
    return selectors.chart(ctx, patient_id)


def _tooth_path(value):
    if not isinstance(value, int) or value not in services.TEETH:
        raise ApiError("not_found", _("Use an FDI tooth number: 11-48 or 51-85."), 404)
    return value


@api("GET", perm="clinical.view", access="history")
def tooth_history_view(request, ctx, data, patient_id, tooth):
    patient_id, tooth = _id(patient_id, "patient"), _tooth_path(tooth)
    _patient_exists(ctx, patient_id)
    record_access(ctx, patient_id, AuditAccess.Action.HISTORY)
    return selectors.tooth_history(ctx, patient_id, tooth, paging.limit(request), paging.time_cursor(request))


def _replay_tooth(ctx, stored):
    return {"tooth": selectors.tooth(ctx, stored["target_id"]), "changed": True}


@api("PUT", perm="procedures.record", replay=_replay_tooth)
def tooth_set_view(request, ctx, data, patient_id, tooth):
    """PUT {condition, surfaces?, note?, appointment_id?, expected_condition?}: an examination finding."""
    patient_id, tooth = _id(patient_id, "patient"), _tooth_path(tooth)
    condition = validate.choice(data, "condition", services.CONDITIONS)
    surfaces = services.clean_surfaces(data.get("surfaces"), condition)
    appointment = data.get("appointment_id")
    expected = data["expected_condition"] if "expected_condition" in data else False
    if expected is not False and expected is not None and expected not in services.CONDITIONS:
        raise ApiError("invalid", "expected_condition is not a condition", fields={"expected_condition": "choice"})
    state, changed = services.set_tooth(ctx, patient_id, tooth, condition, surfaces,
                                        validate.text(data, "note", 200, required=False),
                                        validate.ident(appointment, "appointment_id") if appointment else None, expected)
    return Result({"tooth": selectors.tooth_payload(ctx, state), "changed": changed}, target_type="tooth",
                  target_id=state["id"])


def _replay_teeth(ctx, stored):
    return {"teeth": selectors.chart(ctx, stored["target_id"])["teeth"]}


@api("PUT", perm="procedures.record", replay=_replay_teeth)
def teeth_set_view(request, ctx, data, patient_id):
    """PUT {teeth: [{tooth, condition, surfaces?, note?, expected_condition?}], appointment_id?}: every tooth the
    doctor changed, saved with one Save (Saged, 2026-11-22). All or nothing: one stale tooth is 409 for the lot."""
    patient_id = _id(patient_id, "patient")
    items = data.get("teeth")
    if not isinstance(items, list) or not 1 <= len(items) <= 52:
        raise ApiError("invalid", _("Change at least one tooth."), fields={"teeth": "count"})
    appointment = data.get("appointment_id")
    appointment = validate.ident(appointment, "appointment_id") if appointment else None
    cleaned = []
    for item in items:
        if not isinstance(item, dict):
            raise ApiError("invalid", "Each tooth is an object.", fields={"teeth": "shape"})
        tooth = item.get("tooth")
        if not isinstance(tooth, int) or tooth not in services.TEETH:
            raise ApiError("invalid", _("Use an FDI tooth number: 11-48 or 51-85."), fields={"teeth": "tooth"})
        condition = validate.choice(item, "condition", services.CONDITIONS)
        expected = item["expected_condition"] if "expected_condition" in item else False
        if expected is not False and expected is not None and expected not in services.CONDITIONS:
            raise ApiError("invalid", "expected_condition is not a condition", fields={"expected_condition": "choice"})
        cleaned.append((tooth, condition, services.clean_surfaces(item.get("surfaces"), condition),
                        validate.text(item, "note", 200, required=False), expected))
    if len({c[0] for c in cleaned}) != len(cleaned):
        raise ApiError("invalid", "A tooth comes once.", fields={"teeth": "duplicate"})
    for tooth, condition, surfaces, note, expected in sorted(cleaned):  # one lock order: by tooth number
        services.set_tooth(ctx, patient_id, tooth, condition, surfaces, note, appointment, expected)
    # The whole chart as it now is, so a repeat answers the same.
    return Result({"teeth": selectors.chart(ctx, patient_id)["teeth"]}, target_type="patient", target_id=patient_id)


@api("GET", perm="clinical.view", access="history")
def plans_view(request, ctx, data, patient_id):
    patient_id = _id(patient_id, "patient")
    _patient_exists(ctx, patient_id)
    record_access(ctx, patient_id, AuditAccess.Action.HISTORY)
    return selectors.plans(ctx, patient_id)


def _plan_answer(ctx, plan_id):
    patient_id = services.sql.row(ctx, 'SELECT patient_id FROM "clinical_plan" WHERE clinic_id = %s AND id = %s',
                                  [ctx.clinic_id, plan_id])[0]
    return {"plan": selectors.plans(ctx, patient_id, plan_id)}


def _replay_plan(ctx, stored):
    return _plan_answer(ctx, stored["target_id"])


@api("POST", perm="procedures.record", replay=_replay_plan)
def plan_create_view(request, ctx, data, patient_id):
    """POST {section_id?, title?, items: [{procedure_id, tooth?, area?, price?}]}."""
    patient_id = _id(patient_id, "patient")
    section = services.default_section(ctx, data)
    plan_id = services.create_plan(ctx, patient_id, section, validate.text(data, "title", 200, required=False),
                                   data.get("items"))
    return Result(_plan_answer(ctx, plan_id), status=201, target_type="plan", target_id=plan_id)


@api("POST", perm="procedures.record", replay=_replay_plan)
def plan_items_view(request, ctx, data, plan_id):
    """POST {items: [{procedure_id, tooth?, area?, price?}]}: more items on a plan."""
    plan_id = _id(plan_id, "plan")
    services.add_plan_items(ctx, plan_id, data.get("items"))
    return Result(_plan_answer(ctx, plan_id), target_type="plan", target_id=plan_id)


def _replay_plan_move(ctx, stored):
    return {**_plan_answer(ctx, stored["target_id"]), "changed": True}


@api("POST", perm="procedures.record", replay=_replay_plan_move)
def plan_item_status_view(request, ctx, data, item_id):
    """POST {status, expected}: planned, in_progress, done or dropped, from the status the screen showed."""
    item_id = _id(item_id, "plan_item")
    status = validate.choice(data, "status", services.PLAN_ITEM_STATUSES)
    expected = validate.choice(data, "expected", services.PLAN_ITEM_STATUSES)
    plan_id, changed = services.move_plan_item(ctx, item_id, status, expected)
    return Result({**_plan_answer(ctx, plan_id), "changed": changed}, target_type="plan", target_id=plan_id)


# --- Prescriptions ---------------------------------------------------------------------------------------------


def _replay_prescription(ctx, stored):
    return {"prescription": selectors.prescription(ctx, stored["target_id"])}


@api("POST", perm="prescriptions.write", replay=_replay_prescription)
def prescription_create_view(request, ctx, data):
    """POST {patient_id, appointment_id?, section_id?, notes?, items: [{medicine_id?, name?, strength?, form?, dose?,
    note?, add_to_library?}]}. Answers the prescription; its `warnings` list the lines matching recorded allergies."""
    appointment = data.get("appointment_id")
    section = data.get("section_id")
    prescription_id, _warnings = services.create_prescription(
        ctx, validate.ident(data.get("patient_id"), "patient_id"),
        validate.ident(appointment, "appointment_id") if appointment is not None else None,
        validate.ident(section, "section_id") if section is not None else None,
        validate.text(data, "notes", 1000, required=False), services.clean_rx_lines(data))
    return Result(_replay_prescription(ctx, {"target_id": prescription_id}), status=201, target_type="prescription",
                  target_id=prescription_id)


@api("GET", perm="clinical.view", access="prescription")
def prescription_view(request, ctx, data, prescription_id):
    body = selectors.prescription(ctx, _id(prescription_id, "prescription"))
    record_access(ctx, body["patient"]["id"], AuditAccess.Action.PRESCRIPTION)
    return {"prescription": body}


@api("GET", perm="clinical.view", access="history")
def patient_prescriptions_view(request, ctx, data, patient_id):
    patient_id = _id(patient_id, "patient")
    _patient_exists(ctx, patient_id)
    record_access(ctx, patient_id, AuditAccess.Action.HISTORY)
    return selectors.patient_prescriptions(ctx, patient_id, paging.limit(request), paging.time_cursor(request))


@api("GET", perm="prescriptions.write", access="prescription")
def allergy_check_view(request, ctx, data, patient_id):
    """?medicine=1,2&name=Augmentin: the lines that match the patient's recorded allergies, while writing."""
    patient_id = _id(patient_id, "patient")
    patient = services._patient(ctx, patient_id)
    ids = [validate.ident(part, "medicine") for part in request.GET.get("medicine", "").split(",") if part.strip()][:30]
    names = [part.strip()[:120] for part in request.GET.get("name", "").split(",") if part.strip()][:30]
    lines = [{"medicine_id": i, "name": "", "generic": "", "strength": "", "form": "", "dose": "", "note": ""} for i in ids]
    lines = services._fill_from_library(ctx, lines, None) + [{"name": n, "generic": ""} for n in names]
    record_access(ctx, patient_id, AuditAccess.Action.PRESCRIPTION)
    return {"warnings": services.allergy_warnings(patient["allergies"], lines)}


# --- Medicines library -----------------------------------------------------------------------------------------


@api("GET")
def medicines_view(request, ctx, data):
    _any(ctx, "prescriptions.write", "settings.medicines", "clinical.view")
    query = request.GET.get("q", "")
    if len(query) > 60:
        raise ApiError("invalid", "q is too long", fields={"q": "too_long"})
    return selectors.medicines(ctx, _section_param(request), query.strip(), _flag(request, "all"))


def _replay_medicine(ctx, stored):
    row = services.sql.row(ctx, f'SELECT {services.MEDICINE_COLUMNS} FROM "clinical_medicine" WHERE clinic_id = %s '
                                "AND id = %s", [ctx.clinic_id, stored["target_id"]])
    return {"medicine": services.medicine_row(row)}


@api("POST", perm="settings.medicines", replay=_replay_medicine)
def medicine_create_view(request, ctx, data):
    """POST {section_id?, name, generic?, strength?, form?, default_dose?, default_note?}."""
    section = services.default_section(ctx, data)
    medicine = services.create_medicine(ctx, section, services.clean_medicine(data))
    return Result({"medicine": medicine}, status=201, target_type="medicine", target_id=medicine["id"])


def _changes(data, allowed):
    changes = data.get("changes")
    if not isinstance(changes, dict) or not changes:
        raise ApiError("invalid", _("Send the changes."), fields={"changes": "required"})
    unknown = set(changes) - set(allowed)
    if unknown:
        raise ApiError("invalid", "Unknown fields: " + ", ".join(sorted(unknown)),
                       fields={name: "unknown" for name in unknown})
    return changes


@api("PATCH", perm="settings.medicines", replay=_replay_medicine)
def medicine_update_view(request, ctx, data, medicine_id):
    """PATCH {changes: {name?, generic?, strength?, form?, default_dose?, default_note?, is_active?}}."""
    medicine_id = _id(medicine_id, "medicine")
    changes = _changes(data, [*services.MEDICINE_FIELDS, "is_active"])
    medicine = services.update_medicine(ctx, medicine_id, services.clean_medicine(changes, partial=True))
    return Result({"medicine": medicine}, target_type="medicine", target_id=medicine_id)


@api("DELETE", perm="settings.medicines", replay=_replay_medicine)
def medicine_remove_view(request, ctx, data, medicine_id):
    """Remove from the list: the row is deactivated, so old prescriptions and templates keep pointing at it."""
    medicine_id = _id(medicine_id, "medicine")
    medicine = services.update_medicine(ctx, medicine_id, {"is_active": False})
    return Result({"medicine": medicine}, target_type="medicine", target_id=medicine_id)


# --- Prescription templates ------------------------------------------------------------------------------------


def _template_english(data):
    """The optional English name and notes (design G3)."""
    return {"name_en": validate.text(data, "name_en", 120, required=False),
            "notes_en": validate.text(data, "notes_en", 1000, required=False)}


@api("GET")
def templates_view(request, ctx, data):
    _any(ctx, "prescriptions.write", "settings.medicines")
    return selectors.templates(ctx, _section_param(request), _flag(request, "all"))


def _replay_template(ctx, stored):
    return {"template": selectors.templates(ctx, template_id=stored["target_id"])}


@api("POST", perm="settings.medicines", replay=_replay_template)
def template_create_view(request, ctx, data):
    """POST {section_id?, name, notes?, items: [{medicine_id?, name?, strength?, form?, dose?, note?}]}."""
    section = services.default_section(ctx, data)
    template_id = services.create_template(ctx, section, validate.text(data, "name", 120),
                                           validate.text(data, "notes", 1000, required=False),
                                           services.clean_rx_lines(data), **_template_english(data))
    return Result(_replay_template(ctx, {"target_id": template_id}), status=201, target_type="template",
                  target_id=template_id)


@api("PUT", perm="settings.medicines", replay=_replay_template)
def template_update_view(request, ctx, data, template_id):
    """PUT {name, notes?, items, is_active?} with If-Match: <version>. Replaces the lines."""
    template_id = _id(template_id, "template")
    version = _if_match(request)
    services.update_template(ctx, template_id, version, validate.text(data, "name", 120),
                             validate.text(data, "notes", 1000, required=False), services.clean_rx_lines(data),
                             validate.boolean(data, "is_active", default=True), **_template_english(data))
    return Result(_replay_template(ctx, {"target_id": template_id}), target_type="template", target_id=template_id)


@api("DELETE", perm="settings.medicines", replay=_replay_template)
def template_remove_view(request, ctx, data, template_id):
    template_id = _id(template_id, "template")
    services.deactivate_template(ctx, template_id)
    return Result(_replay_template(ctx, {"target_id": template_id}), target_type="template", target_id=template_id)


# --- Procedure catalogue ---------------------------------------------------------------------------------------


@api("GET")
def procedures_view(request, ctx, data):
    _any(ctx, "procedures.record", "settings.manage", "clinical.view", "procedures.manage", "prices.edit")
    return selectors.procedures(ctx, _section_param(request), _flag(request, "all"))


def _replay_procedure(ctx, stored):
    if stored["target_type"] == "cash_request":  # it waited for the owner (Saged, 2026-11-25)
        from finance.views import _replay_request

        return _replay_request(ctx, stored)
    return {"procedure": services._procedure(ctx, stored["target_id"], active=False)}


PROCEDURE_CHANGES = [*services.PROCEDURE_TEXT, "unit", "condition", "price", "internal_cost", "typical_min",
                     "typical_max", "sort", "is_active", "follow_up"]


# Who changes what on the price list (Saged, 2026-11-22): settings.manage anything; the doctor (procedures.manage)
# the procedures themselves; whoever has prices.edit (the desk, a doctor who sees money) the prices; a doctor
# (procedures.record) the follow-ups after a procedure (2026-11-19). The cost of materials stays the owner's.
PROCEDURE_FIELDS = {"name_ar", "name_en", "category", "family", "unit", "condition", "description", "description_en",
                    "sort", "is_active", "follow_up"}
PRICE_FIELDS = {"price", "typical_min", "typical_max"}


def _own_section(ctx, section_id):
    """A doctor keeps the price list of their own sections only, those they work in (Saged, 2026-11-22)."""
    from web.price_pages import own_sections

    sections = own_sections(ctx)
    if sections is not None and section_id not in sections:
        raise services.not_found("procedure")


def _may_change(ctx, names):
    if ctx.can("settings.manage"):
        return
    allowed = set()
    if ctx.can("procedures.manage"):
        allowed |= PROCEDURE_FIELDS
    if ctx.can("prices.edit"):
        allowed |= PRICE_FIELDS
    if ctx.can("procedures.record"):
        allowed |= {"follow_up"}
    if not names or set(names) - allowed:
        ctx.require("settings.manage")


@api("POST", perm="clinical.view", replay=_replay_procedure)
def procedure_create_view(request, ctx, data):
    """POST {section_id?, name_ar, name_en?, category?, family?, unit?, price, typical_min?, typical_max?,
    description?, internal_cost?, condition?, sort?}. A doctor who doesn't see money adds it without a price; the desk
    prices it."""
    if not ctx.can("settings.manage"):
        ctx.require("procedures.manage")
        _may_change(ctx, [n for n in (*PRICE_FIELDS, "internal_cost") if data.get(n) not in (None, "", 0)] or ["name_ar"])
    section = services.default_section(ctx, data)
    if not ctx.can("settings.manage"):
        _own_section(ctx, section["id"])
    procedure = services.create_procedure(ctx, section, services.clean_procedure(data))
    return Result({"procedure": procedure}, status=201, target_type="procedure", target_id=procedure["id"])


@api("PATCH", perm="clinical.view", replay=_replay_procedure)
def procedure_update_view(request, ctx, data, procedure_id):
    """settings.manage changes anything; a doctor (procedures.record) changes the follow-ups only (Saged,
    2026-11-19: the doctor keeps the follow-ups after a procedure)."""
    procedure_id = _id(procedure_id, "procedure")
    changes = services.clean_procedure(_changes(data, PROCEDURE_CHANGES), partial=True)
    _may_change(ctx, list(changes))
    if not ctx.can("settings.manage"):  # a doctor's follow-ups too: their own sections only (Saged, 2026-11-22)
        _own_section(ctx, services._procedure(ctx, procedure_id, active=False)["section_id"])
    from finance import services as finance

    if "price" in changes and finance.needs_approval(ctx, "price"):
        # The owner wants to approve a price change (Saged, 2026-11-25): the old price stays until they do (202).
        current = services._procedure(ctx, procedure_id, active=False)
        if changes["price"] != current["price"]:
            from finance.views import request_payload

            asked = finance.ask_cash(ctx, "price", changes["price"] or 0, note=current["name_ar"],
                                     data={"procedure_id": procedure_id, "changes": changes,
                                           "before": current["price"]})
            return Result({"request": request_payload(ctx, asked), "procedure": current}, status=202,
                          target_type="cash_request", target_id=asked["id"])
    procedure = services.update_procedure(ctx, procedure_id, changes)
    return Result({"procedure": procedure}, target_type="procedure", target_id=procedure_id)


@api("DELETE", perm="clinical.view", replay=_replay_procedure)
def procedure_remove_view(request, ctx, data, procedure_id):
    _any(ctx, "settings.manage", "procedures.manage")
    procedure_id = _id(procedure_id, "procedure")
    if not ctx.can("settings.manage"):
        _own_section(ctx, services._procedure(ctx, procedure_id, active=False)["section_id"])
    procedure = services.update_procedure(ctx, procedure_id, {"is_active": False})
    return Result({"procedure": procedure}, target_type="procedure", target_id=procedure_id)


# --- Follow-ups (Saged, 2026-11-13) ----------------------------------------------------------------------------


def _upcoming(ctx, patient_id):
    from . import followups

    return {"upcoming": followups.upcoming(ctx, patient_id)}


def _replay_follow_ups(ctx, stored):
    return _upcoming(ctx, services._appointment(ctx, stored["target_id"])["patient_id"])


@api("POST", perm="procedures.record", replay=_replay_follow_ups)
def follow_ups_add_view(request, ctx, data, appointment_id):
    """POST {items: [{procedure_id | label, days}]} or {recipe_of: procedure id, tooth?}: what comes next, from this
    visit. Answers the patient's upcoming follow-ups."""
    from . import followups

    appointment_id = _id(appointment_id, "appointment")
    recipe_of = data.get("recipe_of")
    patient_id = followups.add(
        ctx, appointment_id, items=data.get("items"),
        recipe_of=validate.ident(recipe_of, "recipe_of") if recipe_of is not None else None,
        tooth=services.clean_tooth(data["tooth"]) if data.get("tooth") not in (None, "") else None)
    return Result(_upcoming(ctx, patient_id), status=201, target_type="appointment", target_id=appointment_id)


def _replay_follow_up(ctx, stored):
    found = services.sql.row(ctx, 'SELECT p.patient_id FROM "clinical_plan_item" i JOIN "clinical_plan" p '
                                  "ON p.clinic_id = i.clinic_id AND p.id = i.plan_id WHERE i.clinic_id = %s AND i.id = %s",
                             [ctx.clinic_id, stored["target_id"]])
    return _upcoming(ctx, found[0])


@api("POST", perm="procedures.record", replay=_replay_follow_up)
def follow_up_due_view(request, ctx, data, item_id):
    """POST {days}: the suggested day, in days after the visit that suggested it."""
    from . import followups

    item_id = _id(item_id, "plan_item")
    patient_id = followups.set_due(ctx, item_id, parse_int(data.get("days"), "days", 1, followups.MAX_DAYS))
    return Result(_upcoming(ctx, patient_id), target_type="plan_item", target_id=item_id)


@api("POST", perm="procedures.record", replay=_replay_follow_up)
def follow_up_drop_view(request, ctx, data, item_id):
    from . import followups

    item_id = _id(item_id, "plan_item")
    return Result(_upcoming(ctx, followups.drop(ctx, item_id)), target_type="plan_item", target_id=item_id)


@api("POST", perm="appointments.book", replay=lambda ctx, stored: {"ok": True})
def follow_ups_snooze_view(request, ctx, data):
    """POST {item_ids, keep_listed?}: the desk's "later": off the list for a week. With keep_listed (Today's
    strip) the follow-ups only leave the strip and stay in the Follow-ups list."""
    from . import followups

    keep = validate.boolean(data, "keep_listed")
    followups.snooze(ctx, data.get("item_ids"), 0 if keep else followups.SNOOZE_DAYS)
    return Result({"ok": True})


@api("POST", perm="appointments.book", replay=lambda ctx, stored: {"ok": True})
def follow_ups_booked_view(request, ctx, data):
    """POST {item_ids, appointment_id}: these follow-ups were booked as this appointment."""
    from . import followups

    followups.link_booked(ctx, data.get("item_ids"), validate.ident(data.get("appointment_id"), "appointment_id"))
    return Result({"ok": True})
