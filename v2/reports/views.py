"""Insights and performance endpoints (design F6, F8)."""

import csv
import io

from django.http import HttpResponse
from django.utils import timezone
from django.utils.translation import gettext as _

from audit.models import AuditAccess
from core import paging, validate
from core.api import api
from core.days import business_day
from core.errors import ApiError
from finance.selectors import check_range

from . import selectors


def _list_name(name):
    if name not in selectors.LISTS:
        raise ApiError("not_found", _("This list wasn't found."), 404)
    return name


def _tag(request):
    tag = request.GET.get("tag")
    return validate.ident(tag, "tag") if tag else None


MONEY_FIELDS = ("revenue_this_month", "revenue_per_patient", "collected", "refunded", "revenue")


def _money_for(ctx, body):
    """reports.view isn't owner-only: the money in a report goes only to holders of finances.view."""
    hidden = not ctx.can("finances.view")
    body["money_hidden"] = hidden
    if hidden:
        for row in [body, *body.get("items", [])]:
            for field in MONEY_FIELDS:
                row.pop(field, None)
    return body


@api("GET", perm="reports.view")
def insights_view(request, ctx, data):
    return _money_for(ctx, selectors.insights(ctx, business_day(ctx.clinic)))


@api("GET", perm="reports.view")
def patient_list_view(request, ctx, data, name):
    """?tag=<id>, ?limit=, ?after=<patient id>: one F6 list, by patient id."""
    name = _list_name(name)
    size = paging.limit(request)
    after = request.GET.get("after")
    items = selectors.patient_list(ctx, name, business_day(ctx.clinic), _tag(request),
                                   validate.ident(after, "after") if after else 0, size + 1)
    return paging.page(items, size, lambda i: str(i["patient"]["id"]))


@api("GET", perm=("reports.view", "patients.export"))
def export_view(request, ctx, data, name):
    """The whole list as CSV, and an access event (export) for each patient in it.

    A list longer than EXPORT_LIMIT (5,000) is refused with 409 export_too_large, never cut short silently.
    """
    name = _list_name(name)
    limit = selectors.EXPORT_LIMIT
    items = selectors.patient_list(ctx, name, business_day(ctx.clinic), _tag(request), 0, limit + 1)
    if len(items) > limit:
        raise ApiError("export_too_large",
                       _("This list has more than %(limit)s patients. Choose a tag to narrow it, then export again.")
                       % {"limit": f"{limit:,}"}, 409, extra={"limit": limit})
    now = timezone.now()
    AuditAccess.objects.bulk_create([
        AuditAccess(clinic_id=ctx.clinic_id, at=now, membership_id=ctx.membership_id, patient_id=i["patient"]["id"],
                    action=AuditAccess.Action.EXPORT, ip=ctx.ip, request_id=ctx.request_id) for i in items])
    out = io.StringIO()
    out.write("﻿")  # so spreadsheet programs read the Arabic names as UTF-8
    writer = csv.writer(out)
    extra = selectors.EXTRA.get(name)
    writer.writerow(["file_no", "name", "phone", "last_visit", "visits", *([extra] if extra else [])])
    for i in items:
        p = i["patient"]
        writer.writerow([p["file_no"], _safe(p["name"]), _safe(p["phone"]), i["last_visit"] or "", i["visits"],
                         *([i[extra]] if extra else [])])
    response = HttpResponse(out.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{name}.csv"'
    response["Cache-Control"] = "no-store"
    return response


def _safe(value):
    """A cell a spreadsheet won't run as a formula (CSV injection)."""
    value = value or ""
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


@api("GET", perm="reports.view")
def performance_view(request, ctx, data):
    """?from=&to=&by=day|month: clinic performance (F8)."""
    by = request.GET.get("by") or "day"
    if by not in ("day", "month"):
        raise ApiError("invalid", "by must be day or month", fields={"by": "choice"})
    first, last = _dates(request, ctx, by)
    return _money_for(ctx, {"by": by, "from": first.isoformat(), "to": last.isoformat(),
                            "items": selectors.performance(ctx, first, last, by)})


@api("GET", perm="reports.view")
def staff_performance_view(request, ctx, data):
    """?from=&to=: staff performance (F8)."""
    first, last = _dates(request, ctx, "doctor")
    items = selectors.staff_performance(ctx, first, last)
    if not ctx.can("finances.view"):
        items.sort(key=lambda i: (-i["visits"], i["membership_id"]))  # the revenue order would show the revenue
    return _money_for(ctx, {"from": first.isoformat(), "to": last.isoformat(), "items": items})


def _dates(request, ctx, by):
    today = business_day(ctx.clinic)
    first = validate.iso_day(request.GET["from"], "from") if request.GET.get("from") else today.replace(day=1)
    last = validate.iso_day(request.GET["to"], "to") if request.GET.get("to") else today
    check_range(first, last, by)
    return first, last
