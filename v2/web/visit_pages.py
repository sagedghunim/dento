"""The doctor's visit screen (Saged, 2026-11-13): it opens when the doctor calls a patient in and stays put.

/visits/<appointment> is for the visit's own doctor while the patient is in the room: the patient's band, the
visit notes (autosaved through PUT appointments/{id}/note), the procedures with their prices, a prescription,
the dental chart on demand (/visits/<id>/chart, the same part as the teeth page) and "Finish the visit".
Opening Today or the home page while a visit is open comes back here (`open_visit`). The server checks every
action again; hiding a button protects nothing.
"""

from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET

from audit.models import AuditAccess
from audit.services import record_access
from core import sql
from core.api import page
from core.days import business_day, local_iso
from core.errors import ApiError
from core.i18n import pick

from . import format as fmt
from . import teeth
from .http import clinic_page, fragment

NOTE_CODE = "visits.notes"


def treats(ctx):
    """Whether this member treats patients: a doctor, or an owner who works sessions or runs the clinic alone."""
    from scheduling.rules import owner_mode

    member = ctx.membership
    return member is not None and (member.role == "doctor" or owner_mode(ctx) in ("solo", "doctor"))


def open_visit(ctx):
    """The id of this member's own visit in the room today, or None. One read on (clinic, day, doctor)."""
    if not ctx.can(NOTE_CODE) or not treats(ctx):
        return None
    found = sql.row(ctx, 'SELECT id FROM "scheduling_appointment" WHERE clinic_id = %s AND day = %s AND doctor_id = %s '
                         "AND status = 'in_room' ORDER BY called_at DESC LIMIT 1",
                    [ctx.clinic_id, business_day(ctx.clinic), ctx.membership_id])
    return found[0] if found else None


def _own_visit(ctx, appointment_id):
    """The visit, if it is this member's own: anyone else's is 404, as another clinic's is."""
    from scheduling.selectors import appointment

    try:
        visit = appointment(ctx, appointment_id)
    except ApiError:
        return None
    return visit if visit["doctor_id"] == ctx.membership_id else None


def _last_visit(ctx, patient_id, appointment_id):
    """The patient's previous finished visit (a doctor's own only) and what was done in it."""
    from clinical.selectors import note, visit_procedures
    from scheduling.rules import visit_sql

    own, params = visit_sql(ctx, "a.doctor_id")
    found = sql.row(ctx, 'SELECT a.id, a.day, s.name, s.name_en FROM "scheduling_appointment" a '
                         'JOIN "clinics_service" s ON s.clinic_id = a.clinic_id AND s.id = a.service_id '
                         "WHERE a.clinic_id = %s AND a.patient_id = %s AND a.id <> %s AND a.status = 'done'" + own +
                         " ORDER BY a.day DESC, a.id DESC LIMIT 1", [ctx.clinic_id, patient_id, appointment_id, *params])
    if found is None:
        return None
    done = [pick(p["name_ar"], p["name_en"]) for p in visit_procedures(ctx, found[0])]
    text = note(ctx, found[0])
    return {"day": found[1], "service": pick(found[2], found[3]), "procedures": done,
            "note": (text["notes"] or text["complaint"] or text["diagnosis"]) if text else ""}


def _discount_types(ctx, appointment_id):
    """The clinic's active discount types as the doctor's chips, the one suggested for this visit marked (two reads;
    one when the clinic has none). For discounts.suggest only; the percent or amount only with money.view, otherwise
    the names alone (Saged, 2026-11-16)."""
    from billing import discounts

    if not ctx.can("discounts.suggest"):
        return []
    found = discounts.types(ctx)
    if not found:
        return []
    suggested = discounts.suggested(ctx, appointment_id)
    values = ctx.can("money.view")
    return [{"id": d["id"], "name": d["name"], "label": fmt.discount_label(d) if values else "",
             "suggested": bool(suggested and suggested["id"] == d["id"])} for d in found]


# Procedure search (Saged, 2026-11-17): typing the start of a section's name finds all its procedures.
CATEGORY_WORDS = {
    "assessment": ("تقييم", "assessment"), "casting": ("جبس", "casting"), "consultation": ("استشارة", "consultation"),
    "cosmetic": ("تجميل", "cosmetic"), "diagnosis": ("كشف", "تشخيص", "diagnosis"), "endodontics": ("عصب", "endodontics"),
    "endoscopy": ("مناظير", "endoscopy"), "grooming": ("تنظيف", "grooming"), "imaging": ("أشعة", "imaging"),
    "implants": ("زراعة", "implants"), "injectables": ("حقن", "injectables"), "injections": ("حقن", "injections"),
    "laser": ("ليزر", "laser"), "orthodontics": ("تقويم", "orthodontics"), "packages": ("باقات", "packages"),
    "pediatric": ("أطفال", "pediatric"), "periodontics": ("لثة", "periodontics"), "preventive": ("وقاية", "preventive"),
    "procedures": ("إجراءات", "procedures"), "prosthodontics": ("تركيبات", "prosthodontics"),
    "radiology": ("أشعة", "radiology", "x-ray"), "reports": ("تقارير", "reports"), "restorative": ("حشو", "restorative"),
    "sessions": ("جلسات", "sessions"), "surgery": ("جراحة", "خلع", "surgery"), "tests": ("تحاليل", "tests"),
    "treatment": ("علاج", "treatment"), "ultrasound": ("سونار", "ultrasound"), "vaccination": ("تطعيم", "vaccination"),
}


def _procedures(ctx, section_id):
    from clinical.followups import VISIT_FEE_CATEGORIES
    from clinical.selectors import procedures

    return [{"id": p["id"], "name": pick(p["name_ar"], p["name_en"]), "price": p["price"], "unit": p["unit"],
             # What the search matches: both names and the section's words in both languages (Saged, 2026-11-17).
             "find": " ".join(filter(None, (p["name_ar"], p["name_en"], *CATEGORY_WORDS.get(p["category"], ())))),
             # Its follow-up recipe, offered once it is recorded: the days and each session's name.
             "follow_up": [{"days": step["days"], "label": pick(step["label"], step.get("label_en", ""))}
                           for step in p["follow_up"] or []]}
            for p in procedures(ctx, section_id)["items"] if p["category"] not in VISIT_FEE_CATEGORIES]


@require_GET
@ensure_csrf_cookie
@page(perm=NOTE_CODE)
@clinic_page
def visit_page(request, ctx, appointment_id):
    from clinical import followups
    from clinical.selectors import visit_record
    from patients import selectors as patients

    from .pages import _perms

    visit = _own_visit(ctx, appointment_id)
    if visit is None:
        return render(request, "web/not_found.html", {"ctx": ctx}, status=404)
    if visit["status"] != "in_room":
        return redirect("/")  # finished, or sent back: Today shows where it is
    today = business_day(ctx.clinic)
    patient_id = visit["patient"]["id"]
    record = visit_record(ctx, visit["id"])
    record_access(ctx, patient_id, AuditAccess.Action.NOTE)
    for p in record["procedures"]:
        p["name"] = pick(p["name_ar"], p["name_en"])
    patient = patients.file_payload(patients.get(ctx, patient_id), today)
    perms = _perms(ctx)
    perms["prescribe"] = ctx.can("prescriptions.write")
    perms["finish"] = ctx.can("appointments.queue")
    # The file's chronic conditions, allergies and notes, written from the visit (Saged, 2026-11-23): the same fields
    # and rule as the file (patients.edit with clinical.write); the desk still fills them when it opens a new file.
    perms["medical"] = ctx.can("patients.edit") and ctx.can("clinical.write")
    note = record["note"] or {"complaint": "", "diagnosis": "", "notes": "", "version": 0}
    config = {
        "appointment_id": visit["id"], "patient_id": patient_id, "section_id": visit["section_id"],
        "called_at": visit["called_at"], "server_time": local_iso(ctx.clinic, timezone.now()),
        "note": {k: note[k] for k in ("complaint", "diagnosis", "notes", "version")},
        "procedures": _procedures(ctx, visit["section_id"]) if perms["record"] else [],
        "upcoming": followups.upcoming(ctx, patient_id, today), "today": today.isoformat(),
        "medical": {k: patient[k] or "" for k in ("chronic_conditions", "allergies", "notes")},
    }
    # The doctor's six most used procedures are one tap each; the rest are found by typing (Saged, 2026-11-13).
    by_id = {p["id"]: p for p in config["procedures"]}
    top = [by_id[i] for i in followups.top_procedures(ctx, visit["doctor_id"], visit["section_id"], today) if i in by_id]         if by_id else []
    config["top"] = [p["id"] for p in top]
    # The plan session this visit was booked for, when its step names the procedure done in it (Saged, 2026-11-16):
    # one tap records it against the plan item, at the plan's price (from `upcoming`, read above: no extra query).
    plan_offer = [{"plan_item_id": u["id"], "procedure_id": u["procedure_id"], "name": by_id[u["procedure_id"]]["name"],
                   "price": u["price"]}
                  for u in config["upcoming"] if u["appointment_id"] == visit["id"] and u["procedure_id"] in by_id]
    config["plan_offer"] = [{k: o[k] for k in ("plan_item_id", "procedure_id")} for o in plan_offer]
    # The small bill: the consultation, then each procedure. The invoice's own total once it has one. Only for
    # holders of money.view: anyone else sees what was done without any amount (Saged, 2026-11-16).
    bill = None
    if ctx.can("money.view"):
        done = sum(p["amount"] for p in record["procedures"])
        bill = {"fee": visit["fee"], "total": record["invoice"]["total"] if record["invoice"] else visit["fee"] + done}
    return render(request, "web/visit.html", {
        "ctx": ctx, "nav_active": "today", "visit": visit, "patient": patient, "perms": perms,
        "last": _last_visit(ctx, patient_id, visit["id"]), "record": record, "config": config, "top": top, "bill": bill,
        "plan_offer": plan_offer,
        "dental": perms["chart"] and teeth.is_dental(ctx, visit["section_id"]),
        "discount_types": _discount_types(ctx, visit["id"]),
    })


@fragment(perm="clinical.view")
def visit_chart(request, ctx, appointment_id):
    """GET /visits/<id>/chart: the dental chart part for the visit screen, loaded when the doctor opens it."""
    visit = _own_visit(ctx, appointment_id)
    if visit is None or not teeth.is_dental(ctx, visit["section_id"]):
        from scheduling.rules import not_found

        raise not_found("appointment")
    patient = {"id": visit["patient"]["id"], "full_name": visit["patient"]["name"], "file_no": visit["patient"]["file_no"]}
    record_access(ctx, patient["id"], AuditAccess.Action.HISTORY)
    return HttpResponse(render_to_string("web/_chart.html", teeth.chart_context(ctx, patient, visit), request=request))
