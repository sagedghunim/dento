"""Back-office money endpoints (design F1, F3, F4, F5). Views validate, check codes and call one service."""

from core import paging, validate
from core.api import Result, api
from core.days import business_day, local_iso
from core.errors import ApiError
from core.numbers import parse_int, parse_money

from . import selectors, services
from .models import PAYOUT_METHODS


def _shift_id(value):
    return validate.path_id(value, services.not_found("shift"))


def _member_id(value):
    return validate.path_id(value, services.not_found("member"))


def _shift_payload(ctx, shift):
    body = dict(shift)
    for name in ("opened_at", "closed_at"):
        body[name] = local_iso(ctx.clinic, body[name]) if body[name] else None
    return body


def _totals_payload(ctx, shift, totals):
    movements = [{**m, "at": local_iso(ctx.clinic, m["at"])} for m in totals["movements"]]
    return {**totals, "movements": movements,
            "expected_cash": services.expected_cash(shift["opening_float"], totals)}


def _report(ctx, shift):
    return {"shift": _shift_payload(ctx, shift), "totals": _totals_payload(ctx, shift, services.shift_totals(ctx, shift["id"]))}


def _replay_shift(ctx, stored):
    return _report(ctx, services.get_shift(ctx, stored["target_id"]))


# --- The register (F1) -----------------------------------------------------------------------------------------


@api("GET", perm="cash.collect")
def register_view(request, ctx, data):
    """The open shift and what it holds so far, or {"shift": null}."""
    shift = services.current_shift(ctx)
    return _report(ctx, shift) if shift else {"shift": None, "totals": None}


@api("POST", perm="cash.shift", replay=_replay_shift)
def open_shift_view(request, ctx, data):
    """POST {opening_float, note?}: the counted float. A second open shift is 409 shift_open."""
    shift = services.open_shift(ctx, parse_money(data.get("opening_float"), "opening_float"),
                                validate.text(data, "note", 200, required=False))
    return Result(_report(ctx, shift), status=201, target_type="shift", target_id=shift["id"])


@api("POST", perm="cash.shift", replay=_replay_shift)
def close_shift_view(request, ctx, data, shift_id):
    """POST {counted, note?}: close with the counted cash; the answer carries expected and difference."""
    shift_id = _shift_id(shift_id)
    shift, totals = services.close_shift(ctx, shift_id, parse_money(data.get("counted"), "counted"),
                                         validate.text(data, "note", 200, required=False))
    return Result({"shift": _shift_payload(ctx, shift), "totals": _totals_payload(ctx, shift, totals)},
                  target_type="shift", target_id=shift_id)


@api("POST", perm="cash.shift", replay=_replay_shift)
def cash_movement_view(request, ctx, data):
    """POST {kind: in|out, amount, reason}: cash into or out of the open shift's drawer."""
    shift_id, _movement = services.cash_movement(ctx, validate.choice(data, "kind", ["in", "out"]),
                                                 parse_money(data.get("amount"), "amount", low=1),
                                                 validate.text(data, "reason", 200))
    return Result(_report(ctx, services.get_shift(ctx, shift_id)), status=201, target_type="shift", target_id=shift_id)


@api("GET", perm="finances.view")  # past shifts: the owner's (Saged, 2026-11-22)
def shifts_view(request, ctx, data):
    size = paging.limit(request)
    items = services.shifts(ctx, size, paging.time_cursor(request))
    result = paging.page(items, size, lambda s: paging.encode_time(s["opened_at"], s["id"]))
    result["items"] = [_shift_payload(ctx, s) for s in result["items"]]
    return result


@api("GET", perm="finances.view")
def shift_view(request, ctx, data, shift_id):
    """The shift report: totals by method and the movements (F1)."""
    return _report(ctx, services.get_shift(ctx, _shift_id(shift_id)))


# --- Expenses (F3) ---------------------------------------------------------------------------------------------


def _category_id(value):
    return validate.path_id(value, services.not_found("category"))


def _expense_payload(ctx, expense):
    return {**expense, "day": expense["day"].isoformat(), "created_at": local_iso(ctx.clinic, expense["created_at"]),
            "voided_at": local_iso(ctx.clinic, expense["voided_at"]) if expense["voided_at"] else None}


@api("GET", perm="expenses.manage")
def categories_view(request, ctx, data):
    items = services.categories(ctx, include_inactive=request.GET.get("all") == "1")
    if not items and request.GET.get("all") != "1":
        services.ensure_categories(ctx)  # a place made before M3 gets the defaults on first look
        items = services.categories(ctx)
    return {"items": items}


def _replay_category(ctx, stored):
    return {"category": next(c for c in services.categories(ctx, True) if c["id"] == stored["target_id"])}


@api("POST", perm="expenses.manage", replay=_replay_category)
def create_category_view(request, ctx, data):
    category = services.create_category(ctx, validate.text(data, "name", 60),
                                        validate.text(data, "name_en", 60, required=False))
    return Result({"category": category}, status=201, target_type="category", target_id=category["id"])


@api("PATCH", perm="expenses.manage", replay=_replay_category)
def update_category_view(request, ctx, data, category_id):
    """PATCH {name?, name_en?, is_active?, sort?}."""
    category_id = _category_id(category_id)
    values = {}
    if "name" in data:
        values["name"] = validate.text(data, "name", 60)
    if "name_en" in data:
        values["name_en"] = validate.text(data, "name_en", 60, required=False)
    if "is_active" in data:
        values["is_active"] = validate.boolean(data, "is_active")
    if "sort" in data:
        values["sort"] = parse_int(data.get("sort"), "sort", 0, 32767)
    category = services.update_category(ctx, category_id, values)
    return Result({"category": category}, target_type="category", target_id=category_id)


def _replay_expense(ctx, stored):
    return {"expense": _expense_payload(ctx, services.get_expense(ctx, stored["target_id"]))}


@api("GET", perm="expenses.manage")
def expenses_view(request, ctx, data):
    """?from=&to= (days, at most 400 apart; default this month), ?category=, keyset paging."""
    today = business_day(ctx.clinic)
    first = validate.iso_day(request.GET["from"], "from") if request.GET.get("from") else today.replace(day=1)
    last = validate.iso_day(request.GET["to"], "to") if request.GET.get("to") else today
    selectors.check_range(first, last, "day")
    category = request.GET.get("category")
    size = paging.limit(request)
    items, total = services.expenses(ctx, first, last, validate.ident(category, "category") if category else None,
                                     size, paging.day_cursor(request))
    result = paging.page(items, size, lambda e: paging.encode_day(e["day"], e["id"]))
    result["items"] = [_expense_payload(ctx, e) for e in result["items"]]
    result["total"] = total
    return result


@api("POST", perm="expenses.manage", replay=_replay_expense)
def add_expense_view(request, ctx, data):
    """POST {category_id, amount, day?, note?, from_shift?}: from_shift takes it out of the open shift's drawer."""
    day = validate.iso_day(data["day"]) if data.get("day") else business_day(ctx.clinic)
    expense = services.add_expense(ctx, validate.ident(data.get("category_id"), "category_id"),
                                   parse_money(data.get("amount"), "amount", low=1), day,
                                   validate.text(data, "note", 200, required=False),
                                   validate.boolean(data, "from_shift", False))
    return Result({"expense": _expense_payload(ctx, expense)}, status=201, target_type="expense",
                  target_id=expense["id"])


@api("POST", perm="cash.shift", replay=_replay_expense)
def drawer_expense_view(request, ctx, data):
    """POST {category_id, amount, note?}: the desk pays rent, a salary… out of today's drawer (Saged, 2026-11-22).
    Other days, expenses not from the drawer and voiding stay with expenses.manage."""
    expense = services.add_expense(ctx, validate.ident(data.get("category_id"), "category_id"),
                                   parse_money(data.get("amount"), "amount", low=1), business_day(ctx.clinic),
                                   validate.text(data, "note", 200, required=False), True)
    return Result({"expense": _expense_payload(ctx, expense)}, status=201, target_type="expense",
                  target_id=expense["id"])


@api("POST", perm="expenses.manage", replay=_replay_expense)
def void_expense_view(request, ctx, data, expense_id):
    expense_id = validate.path_id(expense_id, services.not_found("expense"))
    services.void_expense(ctx, expense_id)
    return Result({"expense": _expense_payload(ctx, services.get_expense(ctx, expense_id))}, target_type="expense",
                  target_id=expense_id)


# --- Staff pay (F4) --------------------------------------------------------------------------------------------


def _statement_payload(ctx, body):
    return {**body, "month": body["month"].isoformat()[:7],
            "days": [{**d, "day": d["day"].isoformat()} for d in body["days"]],
            "payouts": [{**p, "day": p["day"].isoformat(), "created_at": local_iso(ctx.clinic, p["created_at"])}
                        for p in body["payouts"]]}


@api("GET", perm="finances.view")
def pay_rules_view(request, ctx, data):
    return {"items": services.rules(ctx)}


def _replay_rule(ctx, stored):
    rule = next(m["rule"] for m in services.rules(ctx) if m["id"] == stored["target_id"])
    return {"rule": rule}


@api("PUT", perm="finances.pay", replay=_replay_rule)
def set_pay_rule_view(request, ctx, data, membership_id):
    """PUT {percent_bp, per_visit, per_procedure, monthly_salary, version}: absolute values; version 0 when new."""
    membership_id = _member_id(membership_id)
    values = {"percent_bp": parse_int(data.get("percent_bp", 0), "percent_bp", 0, 10000),
              "per_visit": parse_money(data.get("per_visit", 0), "per_visit"),
              "per_procedure": parse_money(data.get("per_procedure", 0), "per_procedure"),
              "monthly_salary": parse_money(data.get("monthly_salary", 0), "monthly_salary")}
    rule = services.set_rule(ctx, membership_id, values, parse_int(data.get("version"), "version", 0, 2**31 - 1))
    return Result({"rule": services.rule_payload(rule)}, target_type="membership", target_id=membership_id)


def _month(request, today):
    raw = request.GET.get("month")
    return selectors.parse_month(raw) if raw else today.replace(day=1)


@api("GET", perm="finances.view")
def statement_view(request, ctx, data, membership_id):
    """?month=YYYY-MM (default this month): the member's monthly statement."""
    membership_id = _member_id(membership_id)
    body = services.statement(ctx, membership_id, _month(request, business_day(ctx.clinic)))
    return _statement_payload(ctx, body)


def _replay_payout(ctx, stored):
    return {"replayed": True, "target": {"type": "payout", "id": stored["target_id"]}}


@api("POST", perm="finances.pay", replay=_replay_payout)
def payout_view(request, ctx, data, membership_id):
    """POST {month: YYYY-MM, amount, method, note?}. Cash comes out of the open shift."""
    membership_id = _member_id(membership_id)
    month = selectors.parse_month(data.get("month"))
    payout_id = services.payout(ctx, membership_id, month, parse_money(data.get("amount"), "amount", low=1),
                                validate.choice(data, "method", PAYOUT_METHODS),
                                validate.text(data, "note", 200, required=False))
    body = _statement_payload(ctx, services.statement(ctx, membership_id, month))
    return Result({"payout_id": payout_id, "statement": body}, status=201, target_type="payout", target_id=payout_id)


# --- Money reports (F5) ----------------------------------------------------------------------------------------


@api("GET", perm="finances.view")
def receivables_view(request, ctx, data):
    size = paging.limit(request)
    rows, total = selectors.receivables(ctx, size, paging.time_cursor(request))
    result = paging.page(rows, size, lambda r: paging.encode_time(r["oldest"], r["patient_id"]))
    today = business_day(ctx.clinic)
    result["items"] = [{
        "patient": {"id": r["patient_id"], "name": r["name"], "phone": r["phone"], "file_no": r["file_no"]},
        "owed": r["owed"], "invoices": r["invoices"], "oldest": local_iso(ctx.clinic, r["oldest"]),
        "age_days": (today - business_day(ctx.clinic, r["oldest"])).days,
        "last_visit": r["last_day"].isoformat() if r["last_day"] else None} for r in result["items"]]
    result["total"] = total
    return result


@api("GET", perm="finances.view")
def revenue_view(request, ctx, data):
    """?from=&to=&by=day|month|doctor|section|method (default: this month by day)."""
    today = business_day(ctx.clinic)
    by = request.GET.get("by") or "day"
    if by not in selectors.REVENUE_BY:
        raise ApiError("invalid", "by must be one of: " + ", ".join(selectors.REVENUE_BY), fields={"by": "choice"})
    first = validate.iso_day(request.GET["from"], "from") if request.GET.get("from") else today.replace(day=1)
    last = validate.iso_day(request.GET["to"], "to") if request.GET.get("to") else today
    selectors.check_range(first, last, by)
    return selectors.revenue(ctx, first, last, by)


@api("GET", perm="finances.view")
def profit_view(request, ctx, data):
    """?month=YYYY-MM (default this month)."""
    return selectors.profit(ctx, _month(request, business_day(ctx.clinic)))
