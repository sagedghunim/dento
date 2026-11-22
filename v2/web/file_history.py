"""The file's history (Saged, 2026-11-22): everything done to one patient's file, newest first, for the owner.

The change log (audit_change) holds each change with who and when, and the patient it belongs to (patient_id, index
audit_change_patient); rows written before that column existed are found through their patient target. A doctor
may write in a file at any time, also with the patient outside: such a change is marked "outside the visit", and one
made after the visit had finished "after the visit ended" (it also reaches the owner's alerts).

Each row is described here, in the reader's language, from what the row itself holds; names of procedures are read
in one query.
"""

import json

from django.shortcuts import render
from django.utils.translation import get_language
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET

from audit.models import AuditAccess
from audit.services import record_access
from core import sql
from core.api import page
from core.days import local_iso
from core.errors import ApiError

from .http import clinic_page

PAGE = 100
# The actions of a file's history, and nothing else the log holds (a booked line priced by the system is not news).
ACTIONS = ("visit_procedure.add", "visit_procedure.remove", "visit_note.save", "vitals.save", "tooth.set",
           "prescription.add", "attachment.add", "plan.create", "plan.items_add", "plan_item.status", "follow_up.add",
           "follow_up.due", "follow_up.drop", "patient.create", "patient.identity", "patient.edit", "patient.tags",
           "patient.restore", "invoice.line_remove", "invoice.discount", "invoice.void", "invoice.visit_fee",
           "payment.refund")


def _where():
    return ('clinic_id = %s AND action = ANY(%s) AND (patient_id = %s '
            "OR (patient_id IS NULL AND target_type = 'patient' AND target_id = %s))")


def rows(ctx, patient_id, before=None, size=PAGE):
    """A page of the file's history, newest first; `before` is the id of the last row of the page above."""
    params = [ctx.clinic_id, list(ACTIONS), patient_id, patient_id]
    after = ""
    if before is not None:
        after = (' AND (at, id) < (SELECT at, id FROM "audit_change" WHERE clinic_id = %s AND id = %s)')
        params += [ctx.clinic_id, before]
    found = sql.dicts(ctx, f'SELECT id, action, membership_id, actor_kind, details, at FROM "audit_change" '
                           f"WHERE {_where()}{after} ORDER BY at DESC, id DESC LIMIT %s", [*params, size + 1])
    for r in found:  # jsonb read as text by the raw cursor
        if isinstance(r["details"], str):
            r["details"] = json.loads(r["details"])
    return found[:size], len(found) > size


def _field_labels():
    return {"full_name": _("Name"), "phone": _("Mobile"), "phone_owner": _("Whose mobile"),
            "guardian_relation": _("Guardian"), "sex": _("Sex"), "birth_date": _("Date of birth"),
            "birth_date_estimated": _("Date of birth"), "address": _("Address"), "notes": _("Notes"),
            "allergies": _("Allergies"), "chronic_conditions": _("Chronic conditions"),
            "complaint": _("Complaint"), "diagnosis": _("Diagnosis")}


def _plan_statuses():
    return {"planned": _("Planned"), "in_progress": _("In progress"), "done": _("Done"), "dropped": _("Dropped")}


def _text(value):
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return _("Yes") if value else _("No")
    if isinstance(value, list):
        return "، ".join(str(v) for v in value) or "—"
    return str(value)


def _changes(before, after):
    """[(label, was, now)] for the fields a change touched, in the reader's words."""
    labels = _field_labels()
    out = []
    for name in after or before or {}:
        out.append((labels.get(name, name), _text((before or {}).get(name)), _text((after or {}).get(name))))
    return out


def describe(ctx, found):
    """Each row as {what, detail, changes, amount, outside, late}, in the reader's language."""
    from .teeth import conditions

    english = (get_language() or "ar")[:2] == "en"
    ids = set()
    for r in found:
        d = r["details"] or {}
        ids.update(i for i in d.get("items", []) if i)
        ids.add((d.get("before") or {}).get("procedure_id") if r["action"] == "visit_procedure.remove" else d.get("procedure_id"))
    ids.discard(None)
    names = {i: (en if english and en else ar) for i, ar, en in sql.rows(
        ctx, 'SELECT id, name_ar, name_en FROM "clinical_procedure" WHERE clinic_id = %s AND id = ANY(%s::bigint[])',
        [ctx.clinic_id, sorted(ids)])} if ids else {}
    states = dict(conditions())
    statuses = _plan_statuses()
    tooth = _("Tooth")
    out = []
    for r in found:
        d, action = r["details"] or {}, r["action"]
        before, after = d.get("before") or {}, d.get("after") or {}
        item = {"what": "", "detail": "", "changes": [], "amount": None,
                "outside": bool(d.get("outside")), "late": bool(d.get("late"))}
        if action == "visit_procedure.add":
            item["what"] = _("Recorded a procedure")
            name = (d.get("name_en") if english and d.get("name_en") else d.get("name")) or names.get(d.get("procedure_id"), "")
            item["detail"] = " · ".join(x for x in (name, f"{tooth} {d['tooth']}" if d.get("tooth") else "",
                                                    f"× {d['qty']}" if (d.get("qty") or 1) > 1 else "") if x)
            item["amount"] = d.get("amount")
        elif action == "visit_procedure.remove":
            item["what"] = _("Removed a recorded procedure")
            item["detail"] = " · ".join(x for x in (names.get(before.get("procedure_id"), ""),
                                                    f"{tooth} {before['tooth']}" if before.get("tooth") else "") if x)
            item["amount"] = before.get("amount")
        elif action == "visit_note.save":
            item["what"] = _("Wrote the visit's notes") if not any(before.values()) else _("Changed the visit's notes")
            item["changes"] = _changes(before, after)
        elif action == "vitals.save":
            item["what"] = _("Recorded the vital signs")
        elif action == "tooth.set":
            item["what"] = _("Changed a tooth on the chart")
            item["detail"] = f"{tooth} {d.get('tooth', '')} · {states.get(before.get('condition'), _('Healthy'))} → " \
                             f"{states.get(after.get('condition'), after.get('condition', ''))}"
        elif action == "prescription.add":
            item["what"] = _("Wrote a prescription")
            item["detail"] = "، ".join(d.get("medicines") or [])
        elif action == "attachment.add":
            item["what"] = _("Added a file")
            item["detail"] = d.get("title", "")
        elif action in ("plan.create", "plan.items_add"):
            item["what"] = _("Made a treatment plan") if action == "plan.create" else _("Added to a treatment plan")
            item["detail"] = "، ".join(names.get(i, "") for i in d.get("items", []) if names.get(i))
        elif action == "plan_item.status":
            item["what"] = _("Changed a plan item")
            item["detail"] = " · ".join(x for x in (
                names.get(d.get("procedure_id"), ""), f"{tooth} {d['tooth']}" if d.get("tooth") else "",
                f"{statuses.get(before.get('status'), '')} → {statuses.get(after.get('status'), '')}") if x)
        elif action == "follow_up.add":
            item["what"] = _("Added a follow-up")
            item["detail"] = "، ".join(s.get("label") or names.get(s.get("procedure_id"), "") for s in d.get("steps", []))
        elif action == "follow_up.due":
            item["what"] = _("Moved a follow-up's day")
        elif action == "follow_up.drop":
            item["what"] = _("Took a follow-up back")
        elif action == "patient.create":
            item["what"] = _("Opened the file")
        elif action in ("patient.identity", "patient.edit"):
            item["what"] = _("Changed the patient's details")
            item["changes"] = _changes(before, after)
        elif action == "patient.tags":
            item["what"] = _("Changed the tags")
        elif action == "patient.restore":
            item["what"] = _("Brought the file back")
        elif action == "invoice.line_remove":
            item["what"] = _("Removed a line from a bill")
            item["detail"] = before.get("description", "")
            item["amount"] = before.get("amount")
        elif action == "invoice.discount":
            item["what"] = _("Gave a discount")
            item["amount"] = after.get("discount")
        elif action == "invoice.void":
            item["what"] = _("Voided a bill")
            item["amount"] = before.get("total")
        elif action == "invoice.visit_fee":
            item["what"] = _("Changed the visit fee")
            item["amount"] = after.get("amount")
        elif action == "payment.refund":
            item["what"] = _("Refunded money to a patient")
            item["amount"] = d.get("amount")
        out.append(item)
    return out


@require_GET
@page()
@clinic_page
def file_history_page(request, ctx, patient_id):
    """GET /patients/<id>/history: the owner only (the alerts' rule)."""
    from patients import selectors

    from .alerts import sees

    if not sees(ctx):
        return render(request, "web/forbidden.html", {"ctx": ctx}, status=403)
    try:
        patient = selectors.get(ctx, patient_id)
    except ApiError:
        return render(request, "web/not_found.html", {"ctx": ctx}, status=404)
    raw = request.GET.get("before", "")
    before = int(raw) if raw.isdigit() and len(raw) < 19 else None
    found, more = rows(ctx, patient.id, before)
    record_access(ctx, patient.id, AuditAccess.Action.HISTORY)
    who = dict(sql.rows(ctx, 'SELECT id, display_name FROM "clinics_membership" WHERE clinic_id = %s', [ctx.clinic_id]))
    items = []
    for r, text in zip(found, describe(ctx, found)):
        items.append({**text, "id": r["id"], "at": local_iso(ctx.clinic, r["at"]),
                      "who": who.get(r["membership_id"]) or _("The system")})
    return render(request, "web/file_history.html", {
        "ctx": ctx, "nav_active": "patients", "patient": patient, "items": items,
        "older": found[-1]["id"] if more and found else None, "first_page": before is None})
