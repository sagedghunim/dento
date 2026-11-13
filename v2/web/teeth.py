"""The dental chart page (design E3): one patient's teeth, for a visit or from the patient file.

/visits/<appointment>/teeth is the chart inside a visit: findings, procedures (invoice lines) and plan items.
/patients/<patient>/teeth is the same chart without a visit: findings and plan items only.
The page renders the teeth and the procedure list here; the script changes them through the clinical API.
"""

from django.http import HttpResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.utils.translation import get_language
from django.utils.translation import gettext as _
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET

from audit.models import AuditAccess
from audit.services import record_access
from core import paging, sql
from core.api import page
from core.errors import ApiError

from . import places
from .http import clinic_page, fragment

HISTORY_PAGE = 20
UPPER = ([18, 17, 16, 15, 14, 13, 12, 11], [21, 22, 23, 24, 25, 26, 27, 28])
LOWER = ([48, 47, 46, 45, 44, 43, 42, 41], [31, 32, 33, 34, 35, 36, 37, 38])
UPPER_PRIMARY = ([55, 54, 53, 52, 51], [61, 62, 63, 64, 65])
LOWER_PRIMARY = ([85, 84, 83, 82, 81], [71, 72, 73, 74, 75])
SURFACE_CONDITIONS = ("caries", "filling")
# Visits that take procedures (clinical.services.record_procedure refuses booked, cancelled and no-show).
OPEN_VISIT = ("waiting", "in_room", "done")


def conditions():
    """The backend's conditions (clinical.models.CONDITIONS) with their labels, in the legend's order."""
    return [
        ("healthy", _("Healthy")), ("caries", _("Caries")), ("pulp_caries", _("Pulp caries")),
        ("planned", _("Planned")), ("filling", _("Filling")), ("crown", _("Crown")),
        ("root_canal", _("Root canal")), ("open_root_canal", _("Open root canal")),
        ("extracted", _("Extracted")), ("missing", _("Missing")), ("implant", _("Implant")),
        ("bridge", _("Bridge")), ("fractured", _("Fractured")), ("impacted", _("Impacted")),
    ]


def shape(number):
    """The outline drawn for an FDI number: molars have two roots; premolars, canines and incisors one."""
    position = number % 10
    if number > 50:
        return "molar" if position >= 4 else ("canine" if position == 3 else "incisor")
    if position >= 6:
        return "molar"
    return {4: "premolar", 5: "premolar", 3: "canine"}.get(position, "incisor")


def is_dental(ctx, section_id=None):
    """A section (or, without one, the place) whose specialty is dental."""
    if section_id is not None:
        return any(s["id"] == section_id and s["specialty"] == "dental" for s in places.sections(ctx))
    return ctx.clinic.specialty_code == "dental"


def dental_sections(ctx):
    return [s for s in places.sections(ctx) if s["specialty"] == "dental"]


def has_chart(ctx):
    """The patient file links to the chart: a dental place or a center with a dental section."""
    return ctx.can("clinical.view") and (is_dental(ctx) or bool(dental_sections(ctx)))


def _procedures(ctx, section_id):
    """The section's tooth procedures, named in the page's language."""
    from clinical.selectors import procedures

    english = (get_language() or "ar")[:2] == "en"
    items = []
    for p in procedures(ctx, section_id)["items"]:
        if p["unit"] != "tooth":
            continue
        items.append({"id": p["id"], "name": (p["name_en"] if english and p["name_en"] else p["name_ar"]),
                      "price": p["price"], "condition": p["condition"]})
    return items


def _with_procedures(ctx, patient_id):
    """Teeth with at least one recorded procedure (index visit_procedure_patient)."""
    rows = sql.rows(ctx, 'SELECT DISTINCT tooth FROM "clinical_visit_procedure" '
                         "WHERE clinic_id = %s AND patient_id = %s AND tooth IS NOT NULL", [ctx.clinic_id, patient_id])
    return {r[0] for r in rows}


def _teeth(chart, done, labels):
    states = {t["tooth"]: t for t in chart["teeth"]}

    def one(number):
        state = states.get(number) or {}
        condition = state.get("condition") or "healthy"
        return {"n": number, "shape": shape(number), "condition": condition, "label": labels[condition],
                "surfaces": state.get("surfaces") or [], "in_progress": bool(state.get("in_progress")),
                "done": number in done, "recorded": bool(state.get("id"))}

    def rows(halves):
        return [[one(n) for n in half] for half in halves]

    return {"permanent": {"upper": rows(UPPER), "lower": rows(LOWER)},
            "primary": {"upper": rows(UPPER_PRIMARY), "lower": rows(LOWER_PRIMARY)}}


def chart_context(ctx, patient, visit=None):
    """What the chart part (_chart.html) renders: the teeth page and the visit screen's chart share it."""
    from clinical.selectors import chart, plans

    from .pages import _perms

    labels = dict(conditions())
    data = chart(ctx, patient["id"])
    done = _with_procedures(ctx, patient["id"])
    if visit is not None:
        section_id = visit["section_id"]
    else:
        found = dental_sections(ctx)
        section_id = found[0]["id"] if found else None
    teeth = _teeth(data, done, labels)
    primary_only = bool(data["teeth"]) and all(t["tooth"] > 50 for t in data["teeth"] if t["id"])
    perms = _perms(ctx)
    patient_plans = plans(ctx, patient["id"])["items"]
    section_plans = [p for p in patient_plans if section_id is None or p["section_id"] == section_id]
    open_plan = next((p for p in section_plans if p["unfinished"]), None)  # none open: "add to plan" starts one
    procedures = _procedures(ctx, section_id) if perms["record"] else []
    names = {p["id"]: p["name"] for p in procedures}
    english = (get_language() or "ar")[:2] == "en"
    # Every open item, whatever the section: "in progress" on the chart follows them all (design E3).
    open_items = [{"id": i["id"], "plan_id": p["id"], "tooth": i["tooth"], "procedure_id": i["procedure_id"],
                   "status": i["status"], "price": i["price"], "name": names.get(i["procedure_id"])
                   or (i["name_en"] if english and i["name_en"] else i["name_ar"])}
                  for p in patient_plans for i in p["items"] if i["status"] in ("planned", "in_progress")]
    can_record = perms["record"] and visit is not None and visit["status"] in OPEN_VISIT
    config = {
        "patient_id": patient["id"], "appointment_id": visit["id"] if visit else None, "section_id": section_id,
        "plan_id": open_plan["id"] if open_plan else None, "can_record": can_record,
        "conditions": conditions(), "surface_conditions": SURFACE_CONDITIONS,
        "teeth": {t["tooth"]: {"condition": t["condition"], "surfaces": t["surfaces"], "note": t["note"],
                               "recorded": bool(t["id"])} for t in data["teeth"]},
        "done": sorted(done), "procedures": procedures, "plan_items": open_items,
    }
    return {
        "ctx": ctx, "patient": patient, "visit": visit,
        "teeth": teeth, "start_primary": primary_only, "legend": conditions(), "perms": perms,
        "can_record": can_record, "procedures": procedures, "chart": config,
    }


def _render(request, ctx, patient, visit=None):
    record_access(ctx, patient["id"], AuditAccess.Action.HISTORY)
    return render(request, "web/teeth.html", {**chart_context(ctx, patient, visit),
                                              "nav_active": "today" if visit else "patients"})


def _not_found(request, ctx):
    return render(request, "web/not_found.html", {"ctx": ctx}, status=404)


@require_GET
@ensure_csrf_cookie
@page(perm="clinical.view")
@clinic_page
def visit_teeth_page(request, ctx, appointment_id):
    """The chart inside a visit: the visit must be this place's and in a dental section."""
    from scheduling.selectors import appointment

    try:
        visit = appointment(ctx, appointment_id)
    except ApiError:
        return _not_found(request, ctx)
    if not is_dental(ctx, visit["section_id"]):
        return _not_found(request, ctx)
    patient = {"id": visit["patient"]["id"], "full_name": visit["patient"]["name"],
               "file_no": visit["patient"]["file_no"]}
    return _render(request, ctx, patient, visit)


@require_GET
@ensure_csrf_cookie
@page(perm="clinical.view")
@clinic_page
def patient_teeth_page(request, ctx, patient_id):
    """The chart from the patient file, without a visit: findings and plan items only."""
    from patients import selectors

    try:
        found = selectors.get(ctx, patient_id)
    except ApiError:
        return _not_found(request, ctx)
    if not has_chart(ctx):
        return _not_found(request, ctx)
    return _render(request, ctx, {"id": found.id, "full_name": found.full_name, "file_no": found.file_no})


@fragment(perm="clinical.view")
def tooth_history(request, ctx, patient_id, tooth):
    """GET /patients/<id>/teeth/<tooth>/history: that tooth's rows, newest first, formatted here."""
    from clinical import selectors
    from clinical.services import TEETH
    from patients.selectors import exists

    if tooth not in TEETH:
        raise ApiError("not_found", _("Use an FDI tooth number: 11-48 or 51-85."), 404)
    exists(ctx, patient_id)
    record_access(ctx, patient_id, AuditAccess.Action.HISTORY)
    found = selectors.tooth_history(ctx, patient_id, tooth, HISTORY_PAGE, paging.time_cursor(request))
    from .pages import _doctor_names

    english = (get_language() or "ar")[:2] == "en"
    labels = dict(conditions())
    for item in found["items"]:
        item["label"] = labels.get(item["condition"], item["condition"])
        item["procedure"] = (item["name_en"] if english and item["name_en"] else item["name_ar"]) or ""
    response = HttpResponse(render_to_string("web/_tooth_history.html", {
        "items": found["items"], "names": _doctor_names(ctx)}, request=request))
    response["X-Next"] = found["next"] or ""
    return response
