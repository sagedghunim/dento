"""M1 pages and fragments (design 7.3): Today with its polled queue, patients, receipts and sign-up.

Pages render inside the request's shard transaction (core.api.page). Fragments are fetched by
the page's scripts and checked like a JSON GET (web.http.fragment). Every value is formatted here,
in the clinic's time zone and digits; the browser converts nothing.
"""

import datetime as dt
import hashlib

from django.conf import settings
from django.http import HttpResponse, HttpResponseNotModified
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.utils import timezone, translation
from django.utils.translation import gettext as _
from django.utils.csp import CSP
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.csp import csp_override
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET

from audit.models import AuditAccess
from audit.services import record_access
from core import paging
from core.api import page
from core.days import business_day, local_iso
from core.errors import ApiError
from core.i18n import pick

from . import format as fmt
from . import nav, places, teeth
from .http import clinic_page, fragment, route_exists

QUEUE_CODES = ("appointments.book", "appointments.queue")
VISITS_PAGE = 10
PAYMENTS_PAGE = 10
METHODS = ("cash", "card", "wallet", "instapay", "other")
SOURCES = ("clinic", "phone", "online", "whatsapp")


def _perms(ctx):
    """What the screens offer this person; the server checks every action again."""
    from scheduling.rules import may_book

    return {
        "book": may_book(ctx), "queue": ctx.can("appointments.queue"),
        "view_patients": ctx.can("patients.view"), "edit_patients": ctx.can("patients.edit"),
        "collect": ctx.can("billing.collect"), "discount": ctx.can("billing.discount"),
        "refund": ctx.can("billing.refund"), "finances": ctx.can("finances.view"),
        "chart": ctx.can("clinical.view"), "record": ctx.can("procedures.record"),
        "write_medical": ctx.can("clinical.write"),
        "shift": ctx.can("cash.shift"),  # "Close the drawer" on Today: the day ends with it (Saged, 2026-11-22)  # notes, allergies, chronic conditions (Saged, 2026-11-22)
        # Any amount at all, and the doctor's discount suggestion (Saged, 2026-11-16).
        "money": ctx.can("money.view"), "suggest": ctx.can("discounts.suggest"),
    }


def _features():
    """Endpoints that land with the parallel backend work (design Appendix D): offered once they exist."""
    return {
        "undo_arrive": route_exists("/api/v1/appointments/1/undo-arrive"),
        "undo_call": route_exists("/api/v1/appointments/1/undo-call"),
    }


def _labels():
    return {
        "methods": [(code, label) for code, label in zip(METHODS, (
            _("Cash"), _("Card"), _("Mobile wallet"), _("InstaPay"), _("Other")))],
        "sources": [(code, label) for code, label in zip(SOURCES, (
            _("At the clinic"), _("By phone"), _("Online"), _("WhatsApp")))],
    }


def _has_field(model, name):
    try:
        model._meta.get_field(name)
    except Exception:
        return False
    return True


def _treats(ctx):
    """The member's own call-in opens the visit screen (web.visit_pages)."""
    from .visit_pages import treats

    return ctx.can("visits.notes") and treats(ctx)


def _booking_groups(ctx, services):
    """Book and walk-in offer the consultation and the doctor's price list (Saged, 2026-11-22): `booking` marks
    those. The list comes by its main services (Saged's list, Procedure.family), each a chip the desk picks first,
    then one of its kinds: `family` is the key, `group` its name. A procedure without one is grouped by its section of
    the list. No "Follow-up": a visit that goes on with a treatment takes that treatment's service and its next
    number. The price list leaves out what the visit fee covers (an examination), as on the visit screen.
    Returns the main services in order, [{key, label}], those with something to book."""
    from clinical.followups import VISIT_FEE_CATEGORIES
    from clinical.models import Procedure
    from clinics.booking_services import COUNTED
    from clinics.specialties import FAMILY_LABELS, FAMILY_ORDER

    from .visit_pages import CATEGORY_WORDS

    ids = [s["procedure_id"] for s in services if s["procedure_id"]]
    found = {}
    for p_id, category, family, sort, unit in (
            Procedure.objects.filter(clinic_id=ctx.clinic_id, id__in=ids, is_active=True)
            .values_list("id", "category", "family", "sort", "unit") if ids else ()):
        found[p_id] = {"category": category, "family": family, "sort": sort, "unit": unit}
    first = {}  # where each main service's first item is, for those not on Saged's list
    for p in found.values():
        key = p["family"] or f"c-{p['category']}"
        first[key] = min(first.get(key, p["sort"]), p["sort"])
    families = []
    for s in services:
        p = found.get(s["procedure_id"])
        # What a booking counts (Saged, 2026-11-22): teeth, quadrants, arches or units; "" is one, not counted.
        unit = p["unit"] if p else None
        s["unit"] = unit if unit in COUNTED else ("" if unit or s["kind"] != "procedure" else "unit")
        s["booking"] = s["kind"] == "consultation" or (p is not None and p["category"] not in VISIT_FEE_CATEGORIES)
        s["family"], s["group"] = "", ""
        if p is None or not s["booking"]:
            continue
        if p["family"] in FAMILY_ORDER:
            s["family"], (ar, en) = p["family"], FAMILY_LABELS[p["family"]]
            s["group"] = pick(ar, en)
        elif p["family"]:  # a main service the clinic added (Saged, 2026-11-22)
            s["family"] = s["group"] = p["family"]
        else:
            s["family"] = f"c-{p['category']}"
            words = CATEGORY_WORDS.get(p["category"], ())
            s["group"] = pick(words[0], p["category"].replace("_", " ").capitalize()) if words else _("Other")

    def rank(key, sort=0):
        return (0, FAMILY_ORDER[key], sort) if key in FAMILY_ORDER else (1, first.get(key, 0), sort)

    def order(s):
        p = found.get(s["procedure_id"])
        return (s["kind"] != "consultation", rank(s["family"], p["sort"] if p else 0) if s["family"] else (0, 0, 0))

    services.sort(key=order)
    seen = {}
    for s in services:
        if s["family"] and s["family"] not in seen:
            seen[s["family"]] = {"key": s["family"], "label": s["group"]}
    families.extend(sorted(seen.values(), key=lambda f: rank(f["key"])))
    return families


def clinic_config(ctx, today):
    """What the dialogs need, embedded in the page with json_script (no patient data)."""
    from clinics.models import Membership, Schedule, Service

    with_section = _has_field(Service, "section")
    seen = ctx.can("money.view")  # no price without money.view (Saged, 2026-11-16)
    services = [{"id": s.id, "name": pick(s.name, s.name_en), "kind": s.kind, "price": s.price if seen else None,
                 "section_id": getattr(s, "section_id", None) if with_section else None,
                 "procedure_id": getattr(s, "procedure_id", None)}
                for s in Service.objects.filter(clinic_id=ctx.clinic_id, is_active=True).order_by("sort", "id")]
    families = _booking_groups(ctx, services)
    doctor_sections = {}
    if _has_field(Schedule, "section"):
        for doctor_id, section_id in (Schedule.objects.filter(clinic_id=ctx.clinic_id)
                                      .values_list("doctor_id", "section_id").distinct()):
            doctor_sections.setdefault(doctor_id, []).append(section_id)
    doctors = [{"id": m.id, "name": pick(m.display_name, m.display_name_en), "sections": doctor_sections.get(m.id, [])}
               for m in Membership.objects.filter(clinic_id=ctx.clinic_id, is_active=True,
                                                   role__in=["owner", "doctor"]).order_by("id")]
    from clinics import settings as clinic_settings

    effective = clinic_settings.effective(ctx.clinic.settings)
    return {
        "services": services, "families": families, "doctors": doctors, "sections": places.sections(ctx),
        "me": {"membership_id": ctx.membership_id, "role": ctx.membership.role, "treats": _treats(ctx)},
        "today": today.isoformat(), "last_day": (today + dt.timedelta(days=365)).isoformat(),
        "digits": effective["digits"], "print_language": effective["print_language"],
        "perms": _perms(ctx),
        # The booking deposit to offer in Book, ticked by default (Saged, 2026-11-16); 0 when off or not allowed.
        "deposit": clinic_settings.deposit(ctx.clinic) if ctx.can("billing.collect") else 0,
        # A percent of the visit's fee instead (Saged, 2026-11-19): Book works the amount out from the quote.
        "deposit_percent": clinic_settings.deposit_percent(ctx.clinic) if ctx.can("billing.collect") else 0,
    }


def _calendar_names():
    """The Book calendar's month and weekday names (Django's own translations), weeks from Saturday."""
    from django.utils.dates import MONTHS, WEEKDAYS, WEEKDAYS_ABBR

    order = (5, 6, 0, 1, 2, 3, 4)  # Saturday first, as in Egypt; Monday is 0
    return {"months": [str(MONTHS[m]) for m in range(1, 13)], "weekdays": [str(WEEKDAYS_ABBR[d]) for d in order],
            "days": [str(WEEKDAYS[d]) for d in order]}


def _dialog_context(ctx, today):
    return {"config": clinic_config(ctx, today), "cal": _calendar_names(), **_labels()}


# --- Today ---------------------------------------------------------------------------------------------------


def _day_param(ctx, raw):
    today = business_day(ctx.clinic)
    if not raw:
        return today, today
    try:
        day = dt.date.fromisoformat(raw)
    except ValueError:
        return today, today
    return (day, today) if abs((day - today).days) <= 400 else (today, today)


def _section_of(appointment):
    return appointment.get("section_id") or (appointment.get("service") or {}).get("section_id")


def _dental_rows(ctx):
    """Which rows link to the dental chart: a visit in a dental section (design E3), for those who read it."""
    if not ctx.can("clinical.view"):
        return lambda section_id: False
    ids = {s["id"] for s in places.sections(ctx) if s["specialty"] == "dental"}
    whole = ctx.clinic.specialty_code == "dental"
    return lambda section_id: section_id in ids if section_id else whole


# Each doctor's list puts who needs the desk first (desk review 5): with the doctor, ready, with the assistant,
# waiting, finished but still owing, booked, then didn't come and cancelled. Finished and paid rows fold away.
# The order inside a rank stays the selector's (slot time, then turn).
ROW_RANK = {"in_room": 0, "ready": 1, "with_assistant": 2, "waiting": 3, "done": 4, "booked": 5, "no_show": 6,
            "cancelled": 7}


def _earlier_balances(ctx, day, data):
    """What each patient on the day owes from before, for those who see money (money.view)."""
    if not ctx.can("money.view"):
        return {}
    from billing.selectors import balances

    ids = {a["patient"]["id"] for queue in data["queues"] for a in queue["appointments"]}
    return balances(ctx, ids, day)


def queue_context(ctx, day, today, data):
    """Today's groups, rows and recent changes, from scheduling.selectors.today_data."""
    names = {}
    groups = []
    stats = {"total": 0, "waiting": 0, "in_room": 0, "done": 0}
    dental = _dental_rows(ctx)
    earlier = _earlier_balances(ctx, day, data)
    now = timezone.now()
    for queue in data["queues"]:
        doctor = queue["doctor"]
        names[doctor["id"]] = doctor["name"]
        rows, booked, folded = [], [], []
        for appointment in queue["appointments"]:
            appointment = {**appointment, "section_id": _section_of(appointment)}
            appointment["dental"] = dental(appointment["section_id"])
            appointment["earlier"] = earlier.get(appointment["patient"]["id"], 0)
            starts = appointment["starts_at"]
            appointment["started"] = not starts or dt.datetime.fromisoformat(starts) <= now  # "didn't come" offered
            status = appointment["status"]
            # The wait runs from arrival, only while waiting or ready (never from the booking's time).
            if status in ("waiting", "ready") and appointment["arrived_at"]:
                waited = (now - dt.datetime.fromisoformat(appointment["arrived_at"])).total_seconds() // 60
                appointment["wait"] = fmt.wait_text(waited)
            # Booked visits (not arrived) are a list of their own, by time, never among the waiting (Saged, 2026-11-13).
            # Without money.view every finished visit folds: whether it is paid is the desk's (Saged, 2026-11-16).
            (folded if status == "done" and (appointment["due"] is None or appointment["due"] <= 0)
             else booked if status == "booked" else rows).append(appointment)
            if status != "cancelled":
                stats["total"] += 1
            if status in ("waiting", "in_room", "done"):
                stats[status] += 1
        rows.sort(key=lambda a: ROW_RANK.get(a["status"], 9))  # stable: keeps time and turn inside a rank
        sessions = doctor["sessions"]
        section_ids = sorted({s["section_id"] for s in sessions if s.get("section_id")} |
                             {a["section_id"] for a in rows + booked + folded if a["section_id"]})
        groups.append({"id": doctor["id"], "name": doctor["name"], "sessions": sessions, "rows": rows,
                       "booked": booked, "folded": folded, "counts": queue["counts"], "section_ids": section_ids})
    recent = [{**event, "doctor": names.get(event["doctor_id"], "")} for event in data["recent"]]
    perms = _perms(ctx)
    just_out = []
    if day == today and perms["book"]:  # "just out of the visit": today only, for those who book (one statement)
        from clinical import followups

        from .followup_pages import decorate

        just_out = decorate(followups.just_out(ctx, today))
    return {
        "just_out": just_out,
        "day": day, "today": today, "is_today": day == today, "groups": groups, "stats": stats,
        "recent": recent, "totals": data.get("totals"), "truncated": data["truncated"],
        "version": data["version"], "server_time": data["server_time"],
        "perms": perms, "features": _features(), "show_doctor": len(groups) > 1,
    }


def _queue_etag(ctx, day, version, language):
    """The API's ETag, plus what only the HTML shows: digits, the release and which endpoints exist."""
    from scheduling.selectors import today_etag

    extra = f"{fmt.digit_setting()}|{settings.APP_VERSION}|{sorted(_features().items())}"
    tag = hashlib.sha256(extra.encode()).hexdigest()[:8]
    return today_etag(ctx, day, version, language)[:-1] + f'.h{tag}"'


@fragment(any_of=QUEUE_CODES)
def today_queue(request, ctx):
    """GET /today/queue (design 5.11): 304 while the day's version is unchanged; every answer has Poll-After."""
    from scheduling import selectors

    day, today = _day_param(ctx, request.GET.get("day"))
    state = selectors.today_state(ctx, day)  # the version before the data
    etag = _queue_etag(ctx, day, state["version"], getattr(request, "LANGUAGE_CODE", ""))
    if selectors.etag_matches(request.headers.get("If-None-Match"), etag):
        response = HttpResponseNotModified()
    else:
        from scheduling.services import carry_over_today

        if carry_over_today(ctx, day):  # a page left open across the night (Saged, 2026-11-24)
            state = selectors.today_state(ctx, day)
            etag = _queue_etag(ctx, day, state["version"], getattr(request, "LANGUAGE_CODE", ""))
        data = selectors.today_data(ctx, day, state)
        context = {"ctx": ctx, **queue_context(ctx, day, today, data)}
        response = HttpResponse(render_to_string("web/_queue.html", context, request=request))
    response["ETag"] = etag
    response["Poll-After"] = str(settings.POLL_AFTER_SECONDS)
    response["X-Server-Time"] = local_iso(ctx.clinic, timezone.now())
    return response


@require_GET
@ensure_csrf_cookie
@page()
@clinic_page
def today_page(request, ctx):
    # An owner who only manages, and anyone with reports but no queue, get the Overview instead (Saged, 2026-11-13).
    if nav.is_manager(ctx) or (request.path == "/" and ctx.can("reports.view") and not any(
            ctx.can(code) for code in QUEUE_CODES)):
        return redirect("/overview")
    # A doctor with a patient in the room goes back to that visit: it stays put until "Finish" (Saged, 2026-11-13).
    from .visit_pages import open_visit

    in_room = open_visit(ctx)
    if in_room is not None:
        return redirect(f"/visits/{in_room}")
    if not any(ctx.can(code) for code in QUEUE_CODES):
        if ctx.can("patients.view"):
            return redirect("/patients")
        return render(request, "web/forbidden.html", {"ctx": ctx}, status=403)
    from scheduling import selectors

    from scheduling.services import carry_over_today

    day, today = _day_param(ctx, request.GET.get("day"))
    carry_over_today(ctx, day)  # last night's unfinished visits come to the new day first (Saged, 2026-11-24)
    state = selectors.today_state(ctx, day)
    data = selectors.today_data(ctx, day, state)
    sections = places.sections(ctx)
    context = {
        "ctx": ctx, "nav_active": "today", **queue_context(ctx, day, today, data),
        "etag": _queue_etag(ctx, day, state["version"], getattr(request, "LANGUAGE_CODE", "")),
        "sections": sections if len(sections) > 1 else [],
        "prev_day": day - dt.timedelta(days=1), "next_day": day + dt.timedelta(days=1),
        "poll_after": settings.POLL_AFTER_SECONDS, **_dialog_context(ctx, today),
    }
    if day == today and ctx.can("billing.collect") and ctx.can("money.view"):
        from billing import installments

        context["installments_due"] = installments.due(ctx, today)  # Saged, 2026-11-19
    return render(request, "web/today.html", context)


@fragment(perm=QUEUE_CODES)
def walk_in_defaults(request, ctx):
    """GET /today/walk-in: the doctor and service a walk-in gets now when the desk picks none (design G5).

    The same rule POST /appointments/walk-in applies, read only, so the dialog opens on what the server would
    choose; the desk may change both. {doctor_id, service_id, message}: ids are null when the rule finds none.
    """
    from django.http import JsonResponse

    from scheduling import services

    today = business_day(ctx.clinic)
    body = {"doctor_id": None, "service_id": None, "message": ""}
    try:
        body["doctor_id"], sections = services._walk_in_doctor(ctx, today)
        body["service_id"] = services._walk_in_service(ctx, body["doctor_id"], today, sections)
    except ApiError as error:
        body["message"] = str(error.message)
    response = JsonResponse(body)
    response["Cache-Control"] = "no-store"
    return response


# --- Patients --------------------------------------------------------------------------------------------------


def _with_tags(ctx, results):
    """Each result's tags (one statement): VIP, blocklist and the rest show next to the name (Saged, 2026-11-19)."""
    if results:
        from core import sql

        found = {}
        for pid, name, color in sql.rows(ctx, """
            SELECT pt.patient_id, t.name, t.color FROM "patients_patient_tag" pt
              JOIN "patients_tag" t ON t.clinic_id = pt.clinic_id AND t.id = pt.tag_id
             WHERE pt.clinic_id = %s AND pt.patient_id = ANY(%s::bigint[]) AND t.is_active ORDER BY t.sort, t.id""",
                                         [ctx.clinic_id, [p["id"] for p in results]]):
            found.setdefault(pid, []).append({"name": name, "color": color})
        for p in results:
            p["tags"] = found.get(p["id"], [])
    return results


def _with_owed(ctx, results):
    """Each result's balance, for those who see money (desk review 3, 9): one grouped statement for the page."""
    _with_tags(ctx, results)
    if results and ctx.can("money.view"):
        from billing.selectors import balances

        from billing.installments import late_by_patient

        owed = balances(ctx, [p["id"] for p in results])
        late = late_by_patient(ctx, [p["id"] for p in results])
        for p in results:
            p["owed"] = owed.get(p["id"], 0)
            p["late"] = late.get(p["id"], 0)
    return results


@require_GET
@ensure_csrf_cookie
@page(perm="patients.view")
@clinic_page
def patients_page(request, ctx):
    from patients import selectors

    today = business_day(ctx.clinic)
    query = request.GET.get("q", "")[:60]
    # The tags on top (Saged, 2026-11-19): a tap shows the patients who carry it.
    from patients import tags as patient_tags

    all_tags = patient_tags.tags(ctx)
    tag = next((t for t in all_tags if str(t["id"]) == request.GET.get("tag", "")), None)
    if tag is not None:
        ids = patient_tags.patients_with_tag(ctx, tag["id"])
        results = _with_owed(ctx, selectors.by_ids(ctx, ids, today)) or None  # none: "no patient has this tag"
    else:
        results = _with_owed(ctx, selectors.search(ctx, query, today)) if query else None
    recent = None if query or tag else _with_owed(ctx, selectors.recent(ctx, today))
    from patients import services as patient_services

    hidden = patient_services.hidden(ctx) if ctx.membership.is_owner else []
    return render(request, "web/patients.html", {
        "ctx": ctx, "nav_active": "patients", "q": query, "results": results, "recent": recent, "hidden": hidden, "perms": _perms(ctx),
        "all_tags": all_tags, "tag": tag,
        **_dialog_context(ctx, today)})


@fragment(perm="patients.view")
def patient_search(request, ctx):
    """GET /patients/search?q= : at most 20 files (design 5.7). Picking one opens nothing, so no access event."""
    from patients import selectors

    query = request.GET.get("q", "")
    if len(query) > 60:
        raise ApiError("invalid", "q is too long", fields={"q": "too_long"})  # answered as JSON by @fragment
    today = business_day(ctx.clinic)
    results = _with_owed(ctx, selectors.search(ctx, query, today))
    return HttpResponse(render_to_string("web/_patient_results.html", {
        "results": results, "q": query, "pick": request.GET.get("pick") == "1", "today": today,
        "doctors": {d["id"]: d["name"] for d in clinic_config(ctx, today)["doctors"]} if results else {},
    }, request=request))


def _patient_or_404(request, ctx, patient_id):
    from patients import selectors

    try:
        return selectors.get(ctx, patient_id)
    except ApiError:
        return None


def _doctor_names(ctx):
    from clinics.models import Membership

    return dict(Membership.objects.filter(clinic_id=ctx.clinic_id).values_list("id", "display_name"))


@require_GET
@ensure_csrf_cookie
@xframe_options_sameorigin
@csp_override({**settings.SECURE_CSP, "frame-ancestors": [CSP.SELF]})
@page(perm="patients.view")
@clinic_page
def patient_page(request, ctx, patient_id):
    """The patient file (M1: details, visits and payments). Opening it writes one access event (design 4.10)."""
    from billing.selectors import patient_payments
    from patients import selectors
    from scheduling.selectors import patient_visits

    patient = _patient_or_404(request, ctx, patient_id)
    if patient is None:
        return render(request, "web/not_found.html", {"ctx": ctx}, status=404)
    today = business_day(ctx.clinic)
    record_access(ctx, patient.id, AuditAccess.Action.OPEN)
    visits = patient_visits(ctx, patient.id, VISITS_PAGE, None)
    _with_notes(ctx, visits["items"])
    _with_bill(ctx, visits["items"])
    started = {v["continues_id"] for v in visits["items"] if v["continues_id"]}  # treatments with more visits
    for v in visits["items"]:
        v["in_course"] = v["id"] in started
    payments = patient_payments(ctx, patient.id, PAYMENTS_PAGE, None) if ctx.can("money.view") else None
    # Money paid over a bill, given back from the drawer (Saged, 2026-11-22): a link to the drawer's question.
    from billing.selectors import patient_credits

    credits = patient_credits(ctx, patient.id) if payments is not None and ctx.can("billing.collect") else []
    clinical_view = ctx.can("clinical.view")
    if clinical_view:
        from clinical import selectors as clinical

        plans = clinical.plans(ctx, patient.id)["items"]
        files = clinical.attachments(ctx, patient.id, FILES_PAGE, None)["items"]
    else:
        plans, files = [], []
    return render(request, "web/patient.html", {
        "ctx": ctx, "nav_active": "patients", "patient": selectors.file_payload(patient, today),
        "visits": visits, "payments": payments, "credits": credits, "names": _doctor_names(ctx), "perms": _perms(ctx),
        "summary": _visit_summary(ctx, patient.id), "plans": plans, "files": files,
        "statement": _statement(ctx, patient.id) if ctx.can("money.view") else None,
        "installments": _installments(ctx, patient.id),
        "patient_tags": _patient_tags(ctx, patient.id),
        "method_names": dict(_labels()["methods"]),
        "features": _features(), "dental": teeth.has_chart(ctx), **_dialog_context(ctx, today),
        # ?embed=1: inside the visit screen's window, without the side menu and top bar (Saged, 2026-11-22).
        "embed": request.GET.get("embed") == "1"})


FILES_PAGE = 24


def _patient_tags(ctx, patient_id):
    """The patient's tags and the place's active ones, for the file's tag dialog (Saged, 2026-11-19)."""
    from patients import tags

    mine = tags.patient_tags(ctx, patient_id)
    ids = {t["id"] for t in mine}
    return {"mine": mine, "all": [{**t, "on": t["id"] in ids} for t in tags.tags(ctx)],
            "manage": ctx.can("patients.tags"), "edit": ctx.can("patients.edit")}


def _installments(ctx, patient_id):
    """The running plan and the owner's defaults for a new one, for the desk with money (Saged, 2026-11-19)."""
    if not (ctx.can("billing.collect") and ctx.can("money.view")):
        return None
    from billing import installments
    from clinics import settings as clinic_settings

    values = clinic_settings.effective(ctx.clinic.settings)
    return {"plan": installments.read(ctx, patient_id),
            "defaults": {"down_percent": values["installment_down_percent"], "every": values["installment_every"],
                         "count": values["installment_count"], "today": business_day(ctx.clinic).isoformat()},
            "methods": _labels()["methods"]}


def _visit_summary(ctx, patient_id):
    """First and last finished visit and how many (a doctor's own only), for the top of the patient file."""
    from core import sql
    from scheduling.rules import visit_sql

    own, own_params = visit_sql(ctx, "doctor_id")
    found = sql.row(ctx, 'SELECT min(day), max(day), count(*) FROM "scheduling_appointment" '
                         f"WHERE clinic_id = %s AND patient_id = %s AND status = 'done'{own}",
                    [ctx.clinic_id, patient_id, *own_params])
    return {"first": found[0], "last": found[1], "count": found[2]}


def _statement(ctx, patient_id):
    """The patient's account in three numbers (Saged, 2026-11-19): everything charged, what was paid (refunds
    taken off), and what is left. One statement."""
    from core import sql

    total, paid = sql.row(ctx, """
        SELECT (SELECT coalesce(sum(total), 0) FROM "billing_invoice"
                 WHERE clinic_id = %s AND patient_id = %s AND status <> 'void'),
               (SELECT coalesce(sum(CASE WHEN kind = 'refund' THEN -amount ELSE amount END), 0) FROM "billing_payment"
                 WHERE clinic_id = %s AND patient_id = %s)""", [ctx.clinic_id, patient_id, ctx.clinic_id, patient_id])
    total, paid = int(total), int(paid)  # sums come back as decimals
    return {"total": total, "paid": paid, "left": total - paid}


def bill_lines(lines):
    """The lines a patient reads: a line at 0 (the consultation when a paid procedure was done in the visit, a booked
    procedure counted on its own line) says nothing and goes, unless nothing is charged at all (Saged, 2026-11-22)."""
    charged = [line for line in lines if line["amount"]]
    return charged or lines


def _with_bill(ctx, items):
    """Each visit's bill lines (what was done, how many, at what price), one statement for the page: the visit shows
    what was charged instead of the booked service (Saged, 2026-11-22: never "examination 0 EGP")."""
    ids = [v["invoice"]["id"] for v in items if v.get("invoice")]
    if not ids:
        return items
    from core import sql

    money = ctx.can("money.view")
    found = {}
    for invoice_id, description, service, qty, unit_price, amount in sql.rows(ctx, """
            SELECT l.invoice_id, l.description, s.name, l.qty, l.unit_price, l.amount FROM "billing_invoice_line" l
              LEFT JOIN "clinics_service" s ON s.clinic_id = l.clinic_id AND s.id = l.service_id
             WHERE l.clinic_id = %s AND l.invoice_id = ANY(%s::bigint[]) ORDER BY l.invoice_id, l.id""",
            [ctx.clinic_id, ids]):
        found.setdefault(invoice_id, []).append({"label": description or service or "", "qty": qty,
                                                 "unit_price": unit_price if money else None,
                                                 "amount": amount})
    discounts = dict(sql.rows(ctx, 'SELECT id, discount FROM "billing_invoice" WHERE clinic_id = %s '
                                   "AND id = ANY(%s::bigint[])", [ctx.clinic_id, ids])) if money else {}
    for v in items:
        lines = found.get(v["invoice"]["id"]) if v.get("invoice") else None
        if not lines:
            continue
        lines = bill_lines(lines)
        if not money:
            for line in lines:
                line["amount"] = None
        v["bill"] = {"lines": lines, "discount": discounts.get(v["invoice"]["id"], 0)}
    return items


def _with_notes(ctx, items):
    """Each visit's notes in the patient's history, for those who read clinical notes: one statement for the page."""
    if not items or not ctx.can("clinical.view"):
        return items
    from core import sql

    rows = sql.rows(ctx, 'SELECT appointment_id, complaint, diagnosis, notes FROM "clinical_visit_note" '
                         "WHERE clinic_id = %s AND appointment_id = ANY(%s::bigint[])",
                    [ctx.clinic_id, [v["id"] for v in items]])
    notes = {r[0]: r[1:] for r in rows}
    ids = [v["id"] for v in items]
    # What was done in each visit and its prescriptions (Saged, 2026-11-17): one statement each for the page.
    procedures = {}
    for r in sql.rows(ctx, """
        SELECT v.appointment_id, c.name_ar, c.name_en, v.tooth, v.qty FROM "clinical_visit_procedure" v
          JOIN "clinical_procedure" c ON c.clinic_id = v.clinic_id AND c.id = v.procedure_id
         WHERE v.clinic_id = %s AND v.appointment_id = ANY(%s::bigint[]) ORDER BY v.id""", [ctx.clinic_id, ids]):
        procedures.setdefault(r[0], []).append({"name": pick(r[1], r[2]), "tooth": r[3], "qty": r[4]})
    medicines = {}
    for r in sql.rows(ctx, """
        SELECT p.appointment_id, i.name, i.strength, i.dose FROM "clinical_prescription" p
          JOIN "clinical_prescription_item" i ON i.clinic_id = p.clinic_id AND i.prescription_id = p.id
         WHERE p.clinic_id = %s AND p.appointment_id = ANY(%s::bigint[]) ORDER BY p.id, i.position""", [ctx.clinic_id, ids]):
        medicines.setdefault(r[0], []).append({"name": " ".join(part for part in r[1:3] if part), "dose": r[3]})
    for v in items:
        complaint, diagnosis, text = notes.get(v["id"], ("", "", ""))
        v["note_text"] = " · ".join(part for part in (complaint, diagnosis, text) if part)
        v["note"] = {"complaint": complaint, "diagnosis": diagnosis, "notes": text}
        v["procedures"] = procedures.get(v["id"], [])
        v["medicines"] = medicines.get(v["id"], [])
    return items


def _more(request, ctx, patient_id, kind):
    from billing.selectors import patient_payments
    from scheduling.selectors import patient_visits

    patient = _patient_or_404(request, ctx, patient_id)
    if patient is None:
        raise ApiError("not_found", _("This patient wasn't found."), 404)
    record_access(ctx, patient.id, AuditAccess.Action.HISTORY)
    if kind == "visits":
        found = patient_visits(ctx, patient.id, VISITS_PAGE, paging.day_cursor(request))
        _with_notes(ctx, found["items"])
        _with_bill(ctx, found["items"])
        template = "web/_visit_rows.html"
    else:
        found = patient_payments(ctx, patient.id, PAYMENTS_PAGE, paging.time_cursor(request))
        template = "web/_payment_rows.html"
    response = HttpResponse(render_to_string(template, {
        "items": found["items"], "names": _doctor_names(ctx), "perms": _perms(ctx), "patient": {"id": patient.id},
        **_labels()}, request=request))
    response["X-Next"] = found["next"] or ""
    response["X-Count"] = str(len(found["items"]))
    return response


@fragment(perm="patients.view")
def patient_visits_more(request, ctx, patient_id):
    return _more(request, ctx, patient_id, "visits")


@fragment(perm="patients.view")
def patient_payments_more(request, ctx, patient_id):
    from billing import visibility

    visibility.require(ctx)  # payments are money only (Saged, 2026-11-16)
    return _more(request, ctx, patient_id, "payments")


# --- Receipts (design 7.5) ----------------------------------------------------------------------------------------


@require_GET
@page(perm="patients.view")
@clinic_page
def receipt_page(request, ctx, invoice_id):
    """/print/receipts/<invoice>?lang=ar|en&format=a5|80mm. Printing writes an access event."""
    from billing.selectors import receipt

    if not ctx.can("money.view"):  # a receipt is money only (Saged, 2026-11-16)
        return render(request, "web/forbidden.html", {"ctx": ctx}, status=403)
    try:
        data = receipt(ctx, invoice_id)
    except ApiError:
        return render(request, "web/not_found.html", {"ctx": ctx}, status=404)
    record_access(ctx, data["patient"]["id"], AuditAccess.Action.PRINT)
    from clinics.models import Service

    names = dict(Service.objects.filter(clinic_id=ctx.clinic_id, id__in=[line["service_id"] for line in data["lines"]
                                                                          if line["service_id"]]).values_list("id", "name"))
    for line in data["lines"]:
        line["label"] = line["description"] or names.get(line["service_id"]) or "—"
    # A free line (a plan session's visit at 0) only confuses the patient: the receipt hides it (Saged, 2026-11-17).
    data["lines"] = bill_lines(data["lines"])
    language = request.GET.get("lang")
    if language not in ("ar", "en"):
        language = data["print"]["print_language"]
    paper = "80mm" if request.GET.get("format") == "80mm" else "a5"
    with translation.override(language):
        body = render_to_string("web/receipt.html", {
            "r": data, "lang": language, "paper": paper, "auto_print": request.GET.get("print") == "1",
            "printed_at": local_iso(ctx.clinic, timezone.now()), "invoice_id": data["id"],
            "methods": dict(_labels()["methods"]),
            "other_lang": "en" if language == "ar" else "ar",
            "other_paper": "a5" if paper == "80mm" else "80mm",
        }, request=request)
    response = HttpResponse(body)
    response["Content-Language"] = language
    return response


# --- Sign-up (design D2, D3) -----------------------------------------------------------------------------------------


def _signup_context(request, mode):
    return {"mode": mode, "specialties": places.specialty_choices(getattr(request, "LANGUAGE_CODE", "ar")),
            "signup_ready": route_exists("/api/v1/signup"), "places_ready": route_exists("/api/v1/me/places")}


@require_GET
@ensure_csrf_cookie
def signup_page(request):
    """Public: a new clinic or center with its owner, in four steps."""
    if request.user.is_authenticated:
        return redirect("/places/new")
    return render(request, "web/signup.html", _signup_context(request, "signup"))


@require_GET
@ensure_csrf_cookie
@page(member=False)
def place_new_page(request, ctx):
    """Signed in: add a place and become its owner (from the clinic picker)."""
    from directory.services import may_add_place

    if not may_add_place(request.user):
        return render(request, "web/forbidden.html", status=403)
    return render(request, "web/signup.html", _signup_context(request, "place"))
