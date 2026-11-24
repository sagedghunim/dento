"""Overview: the owner's home (Saged, 2026-11-12), for the owner and holders of `reports.view`.

Clinic performance, team performance and patient insights for today, this week or this month, from the same
summaries the reports API reads (design F6, F8). Money (collected, profit, receivables, revenue per doctor)
shows only with `finances.view`, as the reports API does. This file only renders; nothing here writes.
"""

import datetime as dt

from django.shortcuts import render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET

from core import sql
from core.api import page
from core.days import business_day
from core.i18n import pick

from . import format as fmt
from .http import clinic_page

PERIODS = (("today", gettext_lazy("Today")), ("week", gettext_lazy("This week")), ("month", gettext_lazy("This month")))
LIST_SIZE = 5


def _first_day(period, today):
    if period == "today":
        return today
    if period == "week":  # Egypt's week starts on Saturday
        return today - dt.timedelta(days=(today.weekday() - 5) % 7)
    return today.replace(day=1)


def _minutes(seconds):
    return None if seconds is None else fmt.digits(round(seconds / 60))


def _clinic(ctx, first, today, money):
    """The period's totals from the doctor-days (reports.selectors.performance), new and returning patients from
    the patient summaries (the insights counts, for any period), and money for finances.view."""
    from reports import selectors

    rows = selectors.performance(ctx, first, today, "month")
    total = {k: sum(r[k] for r in rows) for k in ("visits", "no_shows", "procedures", "timed", "collected", "refunded")}
    wait = sum((r["avg_wait_secs"] or 0) * r["timed"] for r in rows)
    room = sum((r["avg_doctor_secs"] or 0) * r["timed"] for r in rows)
    seen, new = sql.row(ctx, """
        SELECT count(*) FILTER (WHERE last_day >= %s), count(*) FILTER (WHERE first_day >= %s)
          FROM "summaries_patient" WHERE clinic_id = %s""", [first, first, ctx.clinic_id])
    booked = total["visits"] + total["no_shows"]
    clinic = {**total, "seen": seen, "new": new, "returning": max(seen - new, 0),
              "avg_wait": _minutes(wait // total["timed"]) if total["timed"] else None,
              "avg_room": _minutes(room // total["timed"]) if total["timed"] else None,
              "no_show_pct": fmt.digits(round(total["no_shows"] * 100 / booked)) if booked else None}
    if money:
        from finance import selectors as money_selectors

        clinic["revenue"] = total["collected"] - total["refunded"]
        clinic["owed"] = money_selectors.receivables(ctx, 1, None)[1]
        clinic["profit"] = money_selectors.profit(ctx, today.replace(day=1))["profit"]
    return clinic


def _team(ctx, first, today, money):
    from clinics.models import Membership
    from reports import selectors

    items = selectors.staff_performance(ctx, first, today)
    if not money:
        items.sort(key=lambda i: (-i["visits"], i["membership_id"]))
    english = dict(Membership.objects.filter(clinic_id=ctx.clinic_id, id__in=[i["membership_id"] for i in items])
                   .values_list("id", "display_name_en"))
    top = max([i["visits"] for i in items] + [1])
    rows = [{**i, "name": pick(i["name"], english.get(i["membership_id"], "")), "avg_room": _minutes(i["avg_doctor_secs"]),
             "bar": i["visits"], "bar_max": top} for i in items]
    if not money:
        return {"rows": rows}
    # What one visit brings in, per doctor, beside the clinic's average and the same length of time just before
    # (an accountant's advice, Saged 2026-11-25). A doctor alone has no one to compare with but their own past.
    last = first - dt.timedelta(days=1)
    before = {i["membership_id"]: _per_visit(i) for i in
              selectors.staff_performance(ctx, last - (today - first), last)}
    for r in rows:
        r["per_visit"] = _per_visit(r)
        r["before"] = before.get(r["membership_id"])
        r["trend"] = None if r["per_visit"] is None or r["before"] is None else (
            "up" if r["per_visit"] > r["before"] else "down" if r["per_visit"] < r["before"] else None)
    visits = sum(r["visits"] for r in rows)
    average = sum(r["revenue"] for r in rows) // visits if visits else None
    return {"rows": rows, "average": average}


def _per_visit(row):
    return row["revenue"] // row["visits"] if row["visits"] else None


def _insights(ctx, today):
    from patients.models import Tag
    from reports import selectors

    counts = selectors.insights(ctx, today)
    lists = {}
    for name in ("going_quiet", "never_came_back"):
        found = selectors.patient_list(ctx, name, today, None, 0, LIST_SIZE + 1)
        lists[name] = {"items": [{**i, "last_text": fmt.day(i["last_visit"], with_weekday=False) if i["last_visit"] else ""}
                                 for i in found[:LIST_SIZE]], "more": len(found) > LIST_SIZE}
    # Top tags: one grouped count over the patient_tag_by_tag index. No report endpoint gives this yet.
    rows = sql.rows(ctx, """
        SELECT tag_id, count(*) FROM "patients_patient_tag" WHERE clinic_id = %s
         GROUP BY tag_id ORDER BY count(*) DESC, tag_id LIMIT 8""", [ctx.clinic_id])
    names = {t: (n, e, active) for t, n, e, active in Tag.objects.filter(clinic_id=ctx.clinic_id, id__in=[r[0] for r in rows])
             .values_list("id", "name", "name_en", "is_active")}
    tags = [{"name": pick(names[t][0], names[t][1]), "count": c} for t, c in rows if t in names and names[t][2]][:5]
    return {"counts": counts, "repeat_pct": fmt.digits(counts["repeat_rate_bp"] // 100), "lists": lists, "tags": tags}


@require_GET
@ensure_csrf_cookie
@page(perm="reports.view")
@clinic_page
def overview_page(request, ctx):
    today = business_day(ctx.clinic)
    period = request.GET.get("period")
    period = period if period in dict(PERIODS) else "today"
    first = _first_day(period, today)
    money = ctx.can("finances.view")
    return render(request, "web/overview.html", {
        "ctx": ctx, "nav_active": "overview", "periods": PERIODS, "period": period,
        "period_label": dict(PERIODS)[period], "first": first, "today": today, "money": money,
        "patients_link": ctx.can("patients.view"), "clinic": _clinic(ctx, first, today, money),
        "team": _team(ctx, first, today, money), "insights": _insights(ctx, today),
    })
