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


def clinic_config(ctx, today):
    """What the dialogs need, embedded in the page with json_script (no patient data)."""
    from clinics.models import Membership, Schedule, Service

    with_section = _has_field(Service, "section")
    services = [{"id": s.id, "name": pick(s.name, s.name_en), "kind": s.kind, "price": s.price,
                 "section_id": getattr(s, "section_id", None) if with_section else None}
                for s in Service.objects.filter(clinic_id=ctx.clinic_id, is_active=True).order_by("sort", "id")]
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
        "services": services, "doctors": doctors, "sections": places.sections(ctx),
        "me": {"membership_id": ctx.membership_id, "role": ctx.membership.role},
        "today": today.isoformat(), "last_day": (today + dt.timedelta(days=365)).isoformat(),
        "digits": effective["digits"], "print_language": effective["print_language"],
        "perms": _perms(ctx),
    }


def _day_choices(today):
    """Day chips for the dialogs: the server decides "today" (design 5.11)."""
    return [{"value": (today + dt.timedelta(days=n)).isoformat(),
             "label": label or fmt.day(today + dt.timedelta(days=n), with_year=False)}
            for n, label in ((0, _("Today")), (1, _("Tomorrow")), (2, ""))]


def _dialog_context(ctx, today):
    return {"config": clinic_config(ctx, today), "day_choices": _day_choices(today), **_labels()}


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
    """What each patient on the day owes from before, for those who see money (collect or finances)."""
    if not (ctx.can("billing.collect") or ctx.can("finances.view")):
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
        rows, folded = [], []
        for appointment in queue["appointments"]:
            appointment = {**appointment, "section_id": _section_of(appointment)}
            appointment["dental"] = dental(appointment["section_id"])
            appointment["earlier"] = earlier.get(appointment["patient"]["id"], 0)
            starts = appointment["starts_at"]
            appointment["started"] = not starts or dt.datetime.fromisoformat(starts) <= now  # "didn't come" offered
            status = appointment["status"]
            (folded if status == "done" and appointment["due"] <= 0 else rows).append(appointment)
            if status != "cancelled":
                stats["total"] += 1
            if status in ("waiting", "in_room", "done"):
                stats[status] += 1
        rows.sort(key=lambda a: ROW_RANK.get(a["status"], 9))  # stable: keeps time and turn inside a rank
        sessions = doctor["sessions"]
        section_ids = sorted({s["section_id"] for s in sessions if s.get("section_id")} |
                             {a["section_id"] for a in rows + folded if a["section_id"]})
        groups.append({"id": doctor["id"], "name": doctor["name"], "sessions": sessions, "rows": rows,
                       "folded": folded, "counts": queue["counts"], "section_ids": section_ids})
    recent = [{**event, "doctor": names.get(event["doctor_id"], "")} for event in data["recent"]]
    return {
        "day": day, "today": today, "is_today": day == today, "groups": groups, "stats": stats,
        "recent": recent, "totals": data.get("totals"), "truncated": data["truncated"],
        "version": data["version"], "server_time": data["server_time"],
        "perms": _perms(ctx), "features": _features(), "show_doctor": len(groups) > 1,
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
    if not any(ctx.can(code) for code in QUEUE_CODES):
        if ctx.can("patients.view"):
            return redirect("/patients")
        return render(request, "web/forbidden.html", {"ctx": ctx}, status=403)
    from scheduling import selectors

    day, today = _day_param(ctx, request.GET.get("day"))
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


def _with_owed(ctx, results):
    """Each result's balance, for those who see money (desk review 3, 9): one grouped statement for the page."""
    if results and (ctx.can("billing.collect") or ctx.can("finances.view")):
        from billing.selectors import balances

        owed = balances(ctx, [p["id"] for p in results])
        for p in results:
            p["owed"] = owed.get(p["id"], 0)
    return results


@require_GET
@ensure_csrf_cookie
@page(perm="patients.view")
@clinic_page
def patients_page(request, ctx):
    from patients import selectors

    today = business_day(ctx.clinic)
    query = request.GET.get("q", "")[:60]
    results = _with_owed(ctx, selectors.search(ctx, query, today)) if query else None
    return render(request, "web/patients.html", {
        "ctx": ctx, "nav_active": "patients", "q": query, "results": results, "perms": _perms(ctx),
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
    payments = patient_payments(ctx, patient.id, PAYMENTS_PAGE, None)
    return render(request, "web/patient.html", {
        "ctx": ctx, "nav_active": "patients", "patient": selectors.file_payload(patient, today),
        "visits": visits, "payments": payments, "names": _doctor_names(ctx), "perms": _perms(ctx),
        "features": _features(), "dental": teeth.has_chart(ctx), **_dialog_context(ctx, today)})


def _more(request, ctx, patient_id, kind):
    from billing.selectors import patient_payments
    from scheduling.selectors import patient_visits

    patient = _patient_or_404(request, ctx, patient_id)
    if patient is None:
        raise ApiError("not_found", _("This patient wasn't found."), 404)
    record_access(ctx, patient.id, AuditAccess.Action.HISTORY)
    if kind == "visits":
        found = patient_visits(ctx, patient.id, VISITS_PAGE, paging.day_cursor(request))
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
    return _more(request, ctx, patient_id, "payments")


# --- Receipts (design 7.5) ----------------------------------------------------------------------------------------


@require_GET
@page(perm="patients.view")
@clinic_page
def receipt_page(request, ctx, invoice_id):
    """/print/receipts/<invoice>?lang=ar|en&format=a5|80mm. Printing writes an access event."""
    from billing.selectors import receipt

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
    if request.user.is_platform_admin:
        return render(request, "web/forbidden.html", status=403)
    return render(request, "web/signup.html", _signup_context(request, "place"))
