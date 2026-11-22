"""Finances (design F1, F3, F4, F5): the month's money, the cash register, expenses, staff pay and the patients who
owe. This file only renders; every save goes through /api/v1/ from finances.js and is checked there again.

Tabs are plain links (?tab=), so a slow phone loads one tab's data, never all five. Who sees what:
`finances.view` the money tabs, `expenses.manage` the expenses, `cash.collect` / `cash.shift` the register,
`finances.pay` edits pay. Money is integer piastres; times are shown in the clinic's time zone.
"""

import datetime as dt
import json

from django.db.models import Sum
from django.http import HttpResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.utils import formats
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET

from core import paging
from core.api import page
from core.days import business_day, zone
from core.errors import ApiError
from core.i18n import pick

from . import format as fmt
from .http import clinic_page, fragment

CODES = ("finances.view", "expenses.manage", "cash.collect", "cash.shift")
TABS = (
    ("month", gettext_lazy("This month"), ("finances.view",)),
    ("register", gettext_lazy("Cash register"), ("cash.collect", "cash.shift")),
    ("expenses", gettext_lazy("Expenses"), ("expenses.manage",)),
    ("pay", gettext_lazy("Staff pay"), ("finances.view",)),
    ("owed", gettext_lazy("Patients who owe"), ("finances.view",)),
)
MONTH_TABS = ("month", "expenses", "pay")
PAGE = 30
SHIFTS_PAGE = 10
METHODS = ("cash", "card", "transfer", "wallet", "instapay", "other")


def _method_labels():
    return dict(zip(METHODS, (_("Cash"), _("Card"), _("Bank transfer"), _("Mobile wallet"), _("InstaPay"),
                              _("Other"))))


def _perms(ctx):
    return {"view": ctx.can("finances.view"), "pay": ctx.can("finances.view") and ctx.can("finances.pay"),
            "expenses": ctx.can("expenses.manage"), "collect": ctx.can("cash.collect"),
            "shift": ctx.can("cash.shift"),
            "drawer_expense": ctx.can("cash.shift") and not ctx.can("expenses.manage"),  # by category, today only
            "cash_moves": ctx.can("cash.shift") and ctx.can("cash.collect"),
            "patients": ctx.can("patients.view")}


def _tabs(ctx):
    return [(key, label) for key, label, codes in TABS if any(ctx.can(code) for code in codes)]


def _month_param(raw, today):
    """?month=YYYY-MM, never after this month; this month when missing or wrong."""
    from finance import selectors

    this = today.replace(day=1)
    if not raw:
        return this
    try:
        month = selectors.parse_month(raw)
    except ApiError:
        return this
    return min(max(month, dt.date(2000, 1, 1)), this)


def _shift_month(month, by):
    year, index = divmod(month.year * 12 + month.month - 1 + by, 12)
    return dt.date(year, index + 1, 1)


def _month_label(month):
    return fmt.digits(formats.date_format(month, "F Y"))


def _month_nav(month, today):
    this = today.replace(day=1)
    choices = [_shift_month(this, -n) for n in range(24)]
    if month not in choices:
        choices.append(month)
    return {"value": month.isoformat()[:7], "label": _month_label(month), "is_current": month == this,
            "prev": _shift_month(month, -1).isoformat()[:7],
            "next": _shift_month(month, 1).isoformat()[:7] if month < this else "",
            "choices": [(m.isoformat()[:7], _month_label(m)) for m in choices]}


def _people(ctx, ids=None):
    """Membership id -> the name in the page's language."""
    from clinics.models import Membership

    found = Membership.objects.filter(clinic_id=ctx.clinic_id)
    if ids is not None:
        found = found.filter(id__in=[i for i in ids if i])
    return {m: pick(name, name_en) for m, name, name_en in found.values_list("id", "display_name", "display_name_en")}


def _local(ctx, instant):
    """An instant as the clinic's day and clock, for a row: ("السبت 7 نوفمبر", <clock html>)."""
    if instant is None:
        return {"day": "", "time": ""}
    local = instant.astimezone(zone(ctx.clinic))
    return {"day": fmt.day(local.date(), with_year=False), "time": fmt.clock(local)}


def _bars(rows, label_of):
    top = max([r["net"] for r in rows] + [1])
    out = [{"label": label_of(r["key"]), "amount": r["net"], "value": max(r["net"], 0), "max": top} for r in rows]
    return sorted(out, key=lambda r: -r["amount"])


# --- This month ---------------------------------------------------------------------------------------------------


def _month_context(ctx, month):
    from clinics.models import Section
    from finance import selectors, services

    first, last = services.month_bounds(month)
    profit = selectors.profit(ctx, month)
    owed = selectors.receivables(ctx, 1, None)[1]
    people = _people(ctx)
    by_doctor = selectors.revenue(ctx, first, last, "doctor")["items"]
    by_method = selectors.revenue(ctx, first, last, "method")["items"]
    sections = {s: pick(n, e) for s, n, e in Section.objects.filter(clinic_id=ctx.clinic_id)
                .values_list("id", "name", "name_en")}
    by_section = selectors.revenue(ctx, first, last, "section")["items"] if len(sections) > 1 else []
    methods = _method_labels()
    return {
        "profit": profit, "owed": owed,
        "splits": [s for s in (
            {"key": "doctor", "title": _("By doctor"), "rows": _bars(by_doctor, lambda k: people.get(k) or _("No doctor"))},
            {"key": "section", "title": _("By section"),
             "rows": _bars(by_section, lambda k: sections.get(k) or _("No section"))} if sections and len(sections) > 1 else None,
            {"key": "method", "title": _("By payment method"), "rows": _bars(by_method, lambda k: methods.get(k, k))},
        ) if s is not None],
    }


# --- The register --------------------------------------------------------------------------------------------------


def _shift_rows(ctx, shifts, people):
    rows = []
    for s in shifts:
        opened_by, closed_by = people.get(s["opened_by_id"], ""), people.get(s["closed_by_id"], "")
        line = (_("Opened by itself on the first cash entry, by %(name)s") if s["auto_opened"]
                else _("Opened and counted by %(name)s"))
        rows.append({**s, "opened": _local(ctx, s["opened_at"]), "closed": _local(ctx, s["closed_at"]),
                     "opened_by": opened_by, "closed_by": closed_by, "opened_line": line % {"name": opened_by},
                     "opened_by_line": _("Opened by %(name)s") % {"name": opened_by},
                     "closed_by_line": _("Closed by %(name)s") % {"name": closed_by}})
    return rows


def _drawer_expenses(ctx, shift_id):
    """The expenses paid from this shift's drawer, by their category's name (one indexed read, a day's worth)."""
    from finance.models import Category, Expense

    found = list(Expense.objects.filter(clinic_id=ctx.clinic_id, shift_id=shift_id, voided_at__isnull=True)
                 .values("id", "category_id", "amount", "note", "created_at", "created_by_id"))
    names = {c: pick(n, e) for c, n, e in Category.objects.filter(
        clinic_id=ctx.clinic_id, id__in={e["category_id"] for e in found}).values_list("id", "name", "name_en")}
    return [{"kind": "expense", "amount": e["amount"], "reason": names.get(e["category_id"], ""), "note": e["note"],
             "at": e["created_at"], "by_id": e["created_by_id"]} for e in found]


def _shift_report(ctx, shift):
    from finance import services

    totals = services.shift_totals(ctx, shift["id"])
    spent = _drawer_expenses(ctx, shift["id"]) if totals["expenses"] else []
    rows = sorted([*totals["movements"], *spent], key=lambda m: m["at"])
    people = _people(ctx, [shift["opened_by_id"], shift["closed_by_id"], *(m["by_id"] for m in rows)])
    methods = _method_labels()
    cash = totals["methods"].get("cash", {"collected": 0, "refunded": 0, "net": 0})
    lines = [(_("Opening float"), shift["opening_float"], ""), (_("Cash payments"), cash["collected"], "+")]
    if cash["refunded"]:
        lines.append((_("Cash refunds"), cash["refunded"], "-"))
    for label, amount, sign in ((_("Other cash in"), totals["cash_in"], "+"), (_("Other cash out"), totals["cash_out"], "-"),
                                (_("Expenses paid from the drawer"), totals["expenses"], "-"),
                                (_("Staff pay in cash"), totals["payouts"], "-")):
        if amount:
            lines.append((label, amount, sign))
    other = [{"label": methods.get(m, m), "net": v["net"]} for m, v in sorted(totals["methods"].items()) if m != "cash"]
    movements = [{**m, "when": _local(ctx, m["at"]), "by": people.get(m["by_id"], "")} for m in rows]
    return {"shift": _shift_rows(ctx, [shift], people)[0], "expected": services.expected_cash(shift["opening_float"], totals),
            "lines": [{"label": label, "amount": -amount if sign == "-" else amount} for label, amount, sign in lines],
            "other": other, "movements": movements}


def _shift_page(ctx, request=None):
    from finance import services

    size = SHIFTS_PAGE
    found = paging.page(services.shifts(ctx, size, paging.time_cursor(request) if request else None), size,
                        lambda s: paging.encode_time(s["opened_at"], s["id"]))
    people = _people(ctx, {i for s in found["items"] for i in (s["opened_by_id"], s["closed_by_id"])})
    return _shift_rows(ctx, found["items"], people), found["next"]


def _register_context(ctx, perms):
    from finance import services

    shift = services.current_shift(ctx)
    context = {"current": _shift_report(ctx, shift) if shift else None}
    if perms["expenses"] or perms["drawer_expense"]:
        context.update(_pay_choices(ctx, _categories(ctx)))
    if perms["shift"]:
        context["shifts"], context["shifts_next"] = _shift_page(ctx)
    return context


# --- Expenses ------------------------------------------------------------------------------------------------------


def _categories(ctx):
    from finance import services

    items = services.categories(ctx, include_inactive=True)
    if not any(c["is_active"] for c in items):
        services.ensure_categories(ctx)  # a place made before M3 gets the defaults on first look, as the API does
        items = services.categories(ctx, include_inactive=True)
    for c in items:
        c["json"] = json.dumps({k: c[k] for k in ("id", "name", "name_ar", "name_en", "code", "is_active")},
                               ensure_ascii=False)
    return items


def _pay_choices(ctx, categories):
    """What "Pay an expense" offers: the place's active categories as taps, "Other" last. "Other" is the way out
    for money that fits no category, so a place without one gets it back (named in its language)."""
    from finance import services

    active = [c for c in categories if c["is_active"]]
    if not any(c["code"] == "other" for c in active):
        services.category_by_code(ctx, "other")
        active = [c for c in _categories(ctx) if c["is_active"]]
    return {"pay_categories": sorted(active, key=lambda c: c["code"] == "other"),
            "today": business_day(ctx.clinic).isoformat()}


def _expense_rows(ctx, month, request=None, categories=None):
    from finance import services

    first, last = services.month_bounds(month)
    last = min(last, business_day(ctx.clinic))
    categories = categories if categories is not None else services.categories(ctx, include_inactive=True)
    names = {c["id"]: c["name"] for c in categories}
    items, total = services.expenses(ctx, first, last, None, PAGE, paging.day_cursor(request) if request else None)
    found = paging.page(items, PAGE, lambda e: paging.encode_day(e["day"], e["id"]))
    people = _people(ctx, {e["created_by_id"] for e in found["items"]})
    rows = []
    for e in found["items"]:
        rows.append({**e, "category": names.get(e["category_id"], ""), "day_text": fmt.day(e["day"], with_year=False),
                     "by": people.get(e["created_by_id"], ""), "voided": e["voided_at"] is not None,
                     "json": json.dumps({"id": e["id"], "amount": e["amount"], "category": names.get(e["category_id"], ""),
                                         "day": e["day"].isoformat()}, ensure_ascii=False)})
    return rows, found["next"], total


def _expenses_context(ctx, month):
    categories = _categories(ctx)
    rows, more, total = _expense_rows(ctx, month, categories=categories)
    choices = _pay_choices(ctx, categories)
    if len(choices["pay_categories"]) != sum(c["is_active"] for c in categories):
        categories = _categories(ctx)  # "Other" came back
    return {"categories": categories, "active_categories": [c for c in categories if c["is_active"]],
            "removed_categories": [c for c in categories if not c["is_active"]],
            "expenses": rows, "expenses_next": more, "expenses_total": total, **choices}


# --- Staff pay -----------------------------------------------------------------------------------------------------


def _rule_text(rule):
    parts = []
    if rule["percent_bp"]:
        parts.append(_("%(percent)s%% of their revenue") % {"percent": fmt.digits(_percent(rule["percent_bp"]))})
    if rule["per_visit"]:
        parts.append(_("%(amount)s per visit") % {"amount": fmt.money_plain(rule["per_visit"])})
    if rule["per_procedure"]:
        parts.append(_("%(amount)s per procedure") % {"amount": fmt.money_plain(rule["per_procedure"])})
    if rule["monthly_salary"]:
        parts.append(_("salary %(amount)s a month") % {"amount": fmt.money_plain(rule["monthly_salary"])})
    return " + ".join(parts)


def _percent(bp):
    whole, rest = divmod(bp, 100)
    return f"{whole}" + (f".{rest:02d}".rstrip("0") if rest else "")


def _pay_context(ctx, month):
    from clinics.models import Membership
    from finance import selectors, services
    from finance.models import Payout

    first, last = services.month_bounds(month)
    earned = selectors.staff_pay(ctx, first, last)
    paid = dict(Payout.objects.filter(clinic_id=ctx.clinic_id, month=first).values("membership_id")
                .annotate(total=Sum("amount")).values_list("membership_id", "total"))
    english = dict(Membership.objects.filter(clinic_id=ctx.clinic_id).values_list("id", "display_name_en"))
    members = []
    for m in services.rules(ctx):
        rule = m["rule"]
        if not m["is_active"] and not rule["version"] and not paid.get(m["id"]):
            continue
        members.append({**m, "name": pick(m["name"], english.get(m["id"], "")), "rule_text": _rule_text(rule),
                        "earned": earned.get(m["id"], 0), "paid": paid.get(m["id"], 0),
                        "balance": earned.get(m["id"], 0) - paid.get(m["id"], 0),
                        "json": json.dumps({"id": m["id"], "name": pick(m["name"], english.get(m["id"], "")), **rule},
                                           ensure_ascii=False)})
    return {"members": members, "methods": list(_method_labels().items())}


def _statement_context(ctx, membership_id, month):
    from finance import services

    body = services.statement(ctx, membership_id, month)
    names = _people(ctx, [membership_id])
    methods = _method_labels()
    days = [{**d, "day_text": fmt.day(d["day"], with_year=False), "net": d["collected"] - d["refunded"]}
            for d in body["days"]]
    payouts = [{**p, "day_text": fmt.day(p["day"], with_year=False), "method_label": methods.get(p["method"], p["method"])}
               for p in body["payouts"]]
    parts = body["parts"]
    return {**body, "name": names.get(membership_id, body["member"]["name"]), "days": days, "payouts": payouts,
            "rule_text": _rule_text(body["rule"]), "month_label": _month_label(month), "month_value": month.isoformat()[:7],
            "part_rows": [(label, parts[key]) for key, label in (
                ("salary", _("Monthly salary")), ("share", _("Share of revenue")), ("visits", _("Per visit")),
                ("procedures", _("Per procedure"))) if parts[key]]}


# --- Patients who owe ----------------------------------------------------------------------------------------------


def _owed_rows(ctx, request=None):
    from finance import selectors

    rows, total = selectors.receivables(ctx, PAGE, paging.time_cursor(request) if request else None)
    found = paging.page(rows, PAGE, lambda r: paging.encode_time(r["oldest"], r["patient_id"]))
    today = business_day(ctx.clinic)
    items = [{**r, "age_days": (today - business_day(ctx.clinic, r["oldest"])).days,
              "age_text": fmt.digits((today - business_day(ctx.clinic, r["oldest"])).days),
              "last_text": fmt.day(r["last_day"], with_weekday=False) if r["last_day"] else ""} for r in found["items"]]
    return items, found["next"], total


def _owed_context(ctx):
    rows, more, total = _owed_rows(ctx)
    return {"owed_rows": rows, "owed_next": more, "owed_total": total}


# --- Pages and fragments -------------------------------------------------------------------------------------------


def _tab_context(ctx, tab, month):
    perms = _perms(ctx)
    context = {"ctx": ctx, "perms": perms, "tab": tab, "month": _month_nav(month, business_day(ctx.clinic))}
    if tab == "month":
        context.update(_month_context(ctx, month))
    elif tab == "register":
        context.update(_register_context(ctx, perms))
    elif tab == "expenses":
        context.update(_expenses_context(ctx, month))
    elif tab == "pay":
        context.update(_pay_context(ctx, month))
    elif tab == "owed":
        context.update(_owed_context(ctx))
    return context


def _pick_tab(ctx, raw):
    tabs = _tabs(ctx)
    keys = [key for key, _label in tabs]
    return (raw if raw in keys else keys[0]) if keys else None, tabs


@require_GET
@ensure_csrf_cookie
@page()
@clinic_page
def finances_page(request, ctx):
    tab, tabs = _pick_tab(ctx, request.GET.get("tab"))
    if tab is None:
        return render(request, "web/forbidden.html", {"ctx": ctx}, status=403)
    month = _month_param(request.GET.get("month"), business_day(ctx.clinic))
    context = _tab_context(ctx, tab, month)
    context.update({"nav_active": "finances", "tabs": tabs, "tab_label": dict(tabs)[tab],
                    "month_tabs": MONTH_TABS, "methods": list(_method_labels().items())})
    return render(request, "web/finances.html", context)


TAB_PARTS = {"month": "web/_fin_month.html", "register": "web/_fin_register.html",
             "expenses": "web/_fin_expenses.html", "pay": "web/_fin_pay.html", "owed": "web/_fin_owed.html"}


@fragment(any_of=CODES)
def finances_part(request, ctx):
    """GET /finances/part?tab=&month=: one tab's body again after a save."""
    tab, _tabs_list = _pick_tab(ctx, request.GET.get("tab"))
    if tab != request.GET.get("tab"):
        raise ApiError("forbidden", _("You don't have permission to do this."), 403)
    month = _month_param(request.GET.get("month"), business_day(ctx.clinic))
    return HttpResponse(render_to_string(TAB_PARTS[tab], _tab_context(ctx, tab, month), request=request))


def _more(request, template, rows, more, extra=None):
    response = HttpResponse(render_to_string(template, {"rows": rows, **(extra or {})}, request=request))
    response["X-Next"] = more or ""
    return response


@fragment(perm="expenses.manage")
def expense_rows(request, ctx):
    """GET /finances/expenses?month=&after=: the next page of a month's expenses."""
    month = _month_param(request.GET.get("month"), business_day(ctx.clinic))
    rows, more, _total = _expense_rows(ctx, month, request)
    return _more(request, "web/_fin_expense_rows.html", rows, more)


@fragment(perm="cash.shift")
def shift_rows(request, ctx):
    """GET /finances/shifts?after=: older shifts."""
    rows, more = _shift_page(ctx, request)
    return _more(request, "web/_fin_shift_rows.html", rows, more)


@fragment(perm="cash.shift")
def shift_report(request, ctx, shift_id):
    """GET /finances/shifts/<id>: one shift's report (totals by method and the movements)."""
    from finance import services

    if shift_id > 2**63 - 1:
        raise services.not_found("shift")
    report = _shift_report(ctx, services.get_shift(ctx, shift_id))
    return HttpResponse(render_to_string("web/_fin_shift_report.html", {"report": report}, request=request))


@fragment(perm="finances.view")
def owed_rows(request, ctx):
    """GET /finances/owed?after=: the next page of patients who owe."""
    rows, more, _total = _owed_rows(ctx, request)
    return _more(request, "web/_fin_owed_rows.html", rows, more, {"perms": _perms(ctx)})


@fragment(perm="finances.view")
def statement(request, ctx, membership_id):
    """GET /finances/pay/<id>?month=: a member's monthly statement."""
    from finance import services

    if membership_id > 2**63 - 1:
        raise services.not_found("member")
    month = _month_param(request.GET.get("month"), business_day(ctx.clinic))
    context = {"s": _statement_context(ctx, membership_id, month), "perms": _perms(ctx)}
    return HttpResponse(render_to_string("web/_fin_statement.html", context, request=request))
