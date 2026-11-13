"""Follow-ups (Saged, 2026-11-13): the desk's list of patients whose doctor asked for another visit that isn't
booked yet, grouped overdue, this week and later.

/follow-ups reads clinical.followups.to_book: a finished visit's planned follow-ups, a keyset page at a time. "Book"
opens the Book dialog on the patient and the doctor, then links the booking to the follow-up; "Later" puts the row
off for a week. A doctor sees only their own visits' follow-ups, an assistant only their sections'. The server
checks every action again.
"""

import datetime as dt

from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET

from core.api import page
from core.days import business_day

from .http import clinic_page

CODE = "appointments.book"


def _after(raw):
    """The keyset cursor "2026-11-20.123", or None."""
    day, _dot, ident = (raw or "").partition(".")
    try:
        return (dt.date.fromisoformat(day), int(ident)) if ident.isdigit() and len(ident) < 19 else None
    except ValueError:
        return None


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
        for row in group["rows"]:
            row["ids"] = ",".join(str(item["id"]) for item in row["items"])
            row["day_iso"] = max(row["day"], today).isoformat()  # an overdue one is booked from today on
    return render(request, "web/followups.html", {
        "ctx": ctx, "nav_active": "followups", "groups": groups, "perms": _perms(ctx), "paged": after is not None,
        "next": f"{cursor[0]}.{cursor[1]}" if cursor else "", **_dialog_context(ctx, today)})
