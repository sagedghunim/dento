"""Follow-ups (Saged, 2026-11-13): the desk's list of patients whose doctor asked for another visit that isn't
booked yet, grouped overdue, this week and later.

/follow-ups reads clinical.followups.to_book: a finished visit's planned follow-ups, a keyset page at a time. "Book"
opens the Book dialog on the patient and the doctor and walks through the row's sessions, each booking claiming its
follow-up in the same request; "Later" puts the row off for a week. A doctor sees only their own visits' follow-ups,
an assistant only their sections'. The server checks every action again.

Today's strip "just out of the visit" (web.pages.queue_context) shows the same rows for today's finished visits.
/follow-ups/whatsapp/<patient> opens WhatsApp on the patient's phone with their coming sessions written out: a
plain wa.me link, built at the tap so it always says what is booked now.
"""

import datetime as dt

from urllib.parse import quote

from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.utils.translation import gettext as _
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET

from core.api import page
from core.days import business_day

from . import format as fmt
from .http import clinic_page

CODE = "appointments.book"
SEND_CODES = ("appointments.book", "procedures.record")  # the desk, and the doctor from the visit screen


def _after(raw):
    """The keyset cursor "2026-11-20.123", or None."""
    day, _dot, ident = (raw or "").partition(".")
    try:
        return (dt.date.fromisoformat(day), int(ident)) if ident.isdigit() and len(ident) < 19 else None
    except ValueError:
        return None


def decorate(rows):
    """What a follow-up row's template needs beyond the service's row: whether WhatsApp can reach the patient."""
    for row in rows:
        row["wa"] = bool(fmt.whatsapp(row["patient"]["phone"]))
    return rows


def plan_text(ctx, patient, sessions):
    """The message the patient gets: the clinic, then each coming session with what and when."""
    lines = [_("Hello %(name)s, these are your coming sessions at %(clinic)s:") % {
        "name": patient["name"], "clinic": ctx.clinic.name}]
    for n, session in enumerate(sessions, 1):
        what = session["name"]
        if session["tooth"]:
            what = _("%(what)s (tooth %(tooth)s)") % {"what": what, "tooth": fmt.digits(session["tooth"])}
        names = {"n": fmt.digits(n), "what": what}
        if session["booked_day"] is None:
            lines.append(_("%(n)s. %(what)s: around %(day)s (not booked yet)") % {
                **names, "day": fmt.day(session["due_on"], with_year=False)})
        elif session["starts_at"] is None:  # a queue day: the day, no time
            lines.append(_("%(n)s. %(what)s: %(day)s") % {**names, "day": fmt.day(session["booked_day"], with_year=False)})
        else:
            lines.append(_("%(n)s. %(what)s: %(day)s at %(time)s") % {
                **names, "day": fmt.day(session["booked_day"], with_year=False),
                "time": fmt.clock_text(session["starts_at"], ctx.clinic)})
    return "\n".join(lines)


@require_GET
@page()
@clinic_page
def whatsapp_plan(request, ctx, patient_id):
    """GET /follow-ups/whatsapp/<patient>: on to wa.me with the plan written out. 404 for a patient the member
    doesn't see (a doctor's own visits, an assistant's sections), one with no coming sessions, or no usable phone."""
    from audit.models import AuditAccess
    from audit.services import record_access
    from clinical import followups

    if not any(ctx.can(code) for code in SEND_CODES):
        return render(request, "web/forbidden.html", {"ctx": ctx}, status=403)
    patient, sessions = followups.plan_sessions(ctx, patient_id) if patient_id < 2**63 else (None, [])
    number = fmt.whatsapp(patient["phone"]) if patient else ""
    if not number:
        return render(request, "web/not_found.html", {"ctx": ctx}, status=404)
    record_access(ctx, patient_id, AuditAccess.Action.EXPORT)  # the plan leaves the system
    response = HttpResponseRedirect(f"https://wa.me/{number}?text={quote(plan_text(ctx, patient, sessions))}")
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    return response


@require_GET
@ensure_csrf_cookie
@page(perm=CODE)
@clinic_page
def followups_page(request, ctx):
    from clinical import followups

    from .pages import _dialog_context, _perms

    today = business_day(ctx.clinic)
    after = _after(request.GET.get("after"))
    groups, cursor = followups.to_book(ctx, today, after)
    for group in groups:
        decorate(group["rows"])
    return render(request, "web/followups.html", {
        "ctx": ctx, "nav_active": "followups", "groups": groups, "perms": _perms(ctx), "paged": after is not None,
        "next": f"{cursor[0]}.{cursor[1]}" if cursor else "", **_dialog_context(ctx, today)})
