"""The cash register, expenses and staff pay (design F1, F3, F4). Inside the request's shard transaction.

Lock order (design 5.0): request key -> [appointment -> invoice -> payment rows] -> the open shift (8a: FOR SHARE to
add to it, FOR NO KEY UPDATE to close it) -> inventory items -> expenses -> clinic-days. Closing locks the shift
before it sums what the shift holds, so a cash payment either commits before the sum (and is counted) or waits
and then finds the register closed (409 register_closed).
"""

import datetime as dt

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from audit.services import record_change
from core import sql
from core.days import business_day
from core.errors import ApiError
from summaries.services import Deltas

SHIFT_COLUMNS = ("id, status, opening_float, opened_at, opened_by_id, closed_at, closed_by_id, expected, counted, "
                 "difference, note")
CATEGORY_COLUMNS = "id, name, code, is_system, is_active, sort"
EXPENSE_COLUMNS = "id, category_id, amount, day, note, shift_id, purchase, created_at, created_by_id, voided_at"
RULE_COLUMNS = "membership_id, percent_bp, per_visit, per_procedure, monthly_salary, version"


def _cols(columns):
    return [c.strip() for c in columns.split(",")]


def not_found(what):
    messages = {
        "shift": _("This shift wasn't found."),
        "category": _("This category wasn't found."),
        "expense": _("This expense wasn't found."),
        "member": _("This member wasn't found."),
    }
    return ApiError("not_found", messages[what], 404)


def register_closed():
    return ApiError("register_closed", _("Open the cash register first."), 409)


def _savepoint(ctx, statement, params, duplicates):
    """Run one statement in a savepoint; a named unique violation becomes its ApiError."""
    try:
        with transaction.atomic(using=ctx.alias):
            return sql.row(ctx, statement, params)
    except IntegrityError as error:
        name = getattr(getattr(error.__cause__, "diag", None), "constraint_name", None)
        if name in duplicates:
            refusal = duplicates[name]
            raise refusal() if callable(refusal) else refusal
        raise


# --- The register (F1) -----------------------------------------------------------------------------------------


def shift_for(ctx, method):
    """The open shift a payment, refund, expense or payout joins, locked FOR SHARE (lock order 8a).

    Cash needs one (409 register_closed) and the cash.collect code; other methods join it when one is open.
    """
    if method == "cash":
        ctx.require("cash.collect")
    found = sql.row(ctx, "SELECT id FROM \"finance_shift\" WHERE clinic_id = %s AND status = 'open' FOR SHARE",
                    [ctx.clinic_id])
    if found is None and method == "cash":
        raise register_closed()
    return found[0] if found else None


def _shift(values):
    return dict(zip(_cols(SHIFT_COLUMNS), values))


def open_shift(ctx, opening_float, note=""):
    """One open shift per place: a second open is 409 shift_open with the open one's id."""
    found = _savepoint(ctx, f"""
        INSERT INTO "finance_shift" (clinic_id, status, opening_float, opened_at, opened_by_id, note)
        VALUES (%s, 'open', %s, %s, %s, %s) RETURNING {SHIFT_COLUMNS}""",
                       [ctx.clinic_id, opening_float, timezone.now(), ctx.membership_id, note],
                       {"shift_one_open": lambda: shift_open_error(ctx)})
    return _shift(found)


def shift_open_error(ctx):
    current = sql.row(ctx, "SELECT id FROM \"finance_shift\" WHERE clinic_id = %s AND status = 'open'", [ctx.clinic_id])
    return ApiError("shift_open", _("A shift is already open."), 409, extra={"shift_id": current[0] if current else None})


def shift_totals(ctx, shift_id):
    """What the shift holds: payments and refunds by method, cash in and out, expenses and payouts from the drawer.

    Five indexed reads of this shift's rows only (a day's worth at most).
    """
    methods = {}
    for method, kind, total in sql.rows(ctx, 'SELECT method, kind, sum(amount)::bigint FROM "billing_payment" '
                                             "WHERE clinic_id = %s AND shift_id = %s GROUP BY method, kind",
                                        [ctx.clinic_id, shift_id]):
        row = methods.setdefault(method, {"collected": 0, "refunded": 0})
        row["collected" if kind == "payment" else "refunded"] += total
    for row in methods.values():
        row["net"] = row["collected"] - row["refunded"]
    movements = sql.dicts(ctx, 'SELECT id, kind, amount, reason, at, by_id FROM "finance_cash_movement" '
                               "WHERE clinic_id = %s AND shift_id = %s ORDER BY id", [ctx.clinic_id, shift_id])
    expenses = sql.row(ctx, 'SELECT coalesce(sum(amount), 0)::bigint FROM "finance_expense" WHERE clinic_id = %s '
                            "AND shift_id = %s AND voided_at IS NULL", [ctx.clinic_id, shift_id])[0]
    payouts = sql.row(ctx, 'SELECT coalesce(sum(amount), 0)::bigint FROM "finance_payout" WHERE clinic_id = %s '
                           "AND shift_id = %s AND method = 'cash'", [ctx.clinic_id, shift_id])[0]
    cash_in = sum(m["amount"] for m in movements if m["kind"] == "in")
    cash_out = sum(m["amount"] for m in movements if m["kind"] == "out")
    return {"methods": methods, "movements": movements, "cash_in": cash_in, "cash_out": cash_out,
            "expenses": expenses, "payouts": payouts}


def expected_cash(opening_float, totals):
    cash = totals["methods"].get("cash", {"net": 0})["net"]
    return opening_float + cash + totals["cash_in"] - totals["cash_out"] - totals["expenses"] - totals["payouts"]


def close_shift(ctx, shift_id, counted, note=""):
    """Lock the shift, then sum it, then close it with the count and the difference. A repeat is 409 shift_closed."""
    found = sql.row(ctx, f'SELECT {SHIFT_COLUMNS} FROM "finance_shift" WHERE clinic_id = %s AND id = %s '
                         "FOR NO KEY UPDATE", [ctx.clinic_id, shift_id])
    if found is None:
        raise not_found("shift")
    shift = _shift(found)
    if shift["status"] != "open":
        raise ApiError("shift_closed", _("This shift is already closed."), 409)
    totals = shift_totals(ctx, shift_id)
    expected = expected_cash(shift["opening_float"], totals)
    closed = _shift(sql.row(ctx, f"""
        UPDATE "finance_shift" SET status = 'closed', closed_at = %s, closed_by_id = %s, expected = %s, counted = %s,
               difference = %s, note = CASE WHEN %s = '' THEN note ELSE %s END
         WHERE clinic_id = %s AND id = %s AND status = 'open' RETURNING {SHIFT_COLUMNS}""", [
        timezone.now(), ctx.membership_id, expected, counted, counted - expected, note, note, ctx.clinic_id, shift_id]))
    record_change(ctx, "shift.close", "shift", shift_id, after={"expected": expected, "counted": counted,
                                                               "difference": counted - expected})
    return closed, totals


def cash_movement(ctx, kind, amount, reason):
    """Cash in or out of the open shift's drawer, with a reason (F1). Audited."""
    shift_id = shift_for(ctx, "cash")
    movement_id = sql.row(ctx, """
        INSERT INTO "finance_cash_movement" (clinic_id, shift_id, kind, amount, reason, at, by_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""", [
        ctx.clinic_id, shift_id, kind, amount, reason, timezone.now(), ctx.membership_id])[0]
    record_change(ctx, "cash." + kind, "shift", shift_id, amount=amount, reason=reason, movement_id=movement_id)
    return shift_id, movement_id


def get_shift(ctx, shift_id):
    found = sql.row(ctx, f'SELECT {SHIFT_COLUMNS} FROM "finance_shift" WHERE clinic_id = %s AND id = %s',
                    [ctx.clinic_id, shift_id])
    if found is None:
        raise not_found("shift")
    return _shift(found)


def current_shift(ctx):
    found = sql.row(ctx, f"SELECT {SHIFT_COLUMNS} FROM \"finance_shift\" WHERE clinic_id = %s AND status = 'open'",
                    [ctx.clinic_id])
    return _shift(found) if found else None


def shifts(ctx, size, after):
    """Newest first, keyset on (opened_at, id)."""
    where, params = "", []
    if after is not None:
        where, params = " AND (opened_at, id) < (%s, %s)", list(after)
    return [_shift(r) for r in sql.rows(ctx, f'SELECT {SHIFT_COLUMNS} FROM "finance_shift" WHERE clinic_id = %s{where} '
                                             "ORDER BY opened_at DESC, id DESC LIMIT %s",
                                        [ctx.clinic_id, *params, size + 1])]


# --- Expenses (F3) ---------------------------------------------------------------------------------------------

DEFAULT_CATEGORIES = [  # (code, Arabic, English)
    ("rent", "إيجار", "Rent"),
    ("salaries", "مرتبات", "Salaries"),
    ("materials", "خامات ومستلزمات", "Materials"),
    ("lab", "معمل", "Lab"),
    ("utilities", "كهرباء ومياه وإنترنت", "Utilities"),
    ("other", "أخرى", "Other"),
]


def seed_categories(clinic_id, language="ar"):
    """The default categories a place doesn't have yet (by code). Never changes a category the place edited."""
    from .models import Category

    have = set(Category.objects.filter(clinic_id=clinic_id).values_list("code", flat=True))
    Category.objects.bulk_create([
        Category(clinic_id=clinic_id, code=code, name=arabic if language == "ar" else english, is_system=True, sort=sort)
        for sort, (code, arabic, english) in enumerate(DEFAULT_CATEGORIES) if code not in have])


def ensure_categories(ctx):
    """The default categories, for a place made before M3 that has none yet (one statement)."""
    language = ctx.clinic.settings.get("print_language", "ar")
    rows = [(code, arabic if language == "ar" else english, sort) for sort, (code, arabic, english)
            in enumerate(DEFAULT_CATEGORIES)]
    sql.run(ctx, f"""
        INSERT INTO "finance_category" (clinic_id, name, code, is_system, is_active, sort)
        VALUES {", ".join(["(%s, %s, %s, true, true, %s)"] * len(rows))} ON CONFLICT DO NOTHING""",
            [p for code, name, sort in rows for p in (ctx.clinic_id, name, code, sort)])


def _duplicate_category():
    return ApiError("duplicate", _("This category is already in the list."), 409, fields={"name": "duplicate"})


def categories(ctx, include_inactive=False):
    where = "" if include_inactive else " AND is_active"
    return sql.dicts(ctx, f'SELECT {CATEGORY_COLUMNS} FROM "finance_category" WHERE clinic_id = %s{where} '
                          "ORDER BY sort, id", [ctx.clinic_id])


def create_category(ctx, name):
    found = _savepoint(ctx, f'INSERT INTO "finance_category" (clinic_id, name, code, is_system, is_active, sort) '
                            f"VALUES (%s, %s, '', false, true, 1000) RETURNING {CATEGORY_COLUMNS}",
                       [ctx.clinic_id, name], {"category_active_name_uniq": _duplicate_category()})
    return dict(zip(_cols(CATEGORY_COLUMNS), found))


def update_category(ctx, category_id, values):
    if not values:
        raise ApiError("invalid", _("Send the changes."), fields={"changes": "required"})
    assignments = ", ".join(f"{name} = %s" for name in values)
    found = _savepoint(ctx, f'UPDATE "finance_category" SET {assignments} WHERE clinic_id = %s AND id = %s '
                            f"RETURNING {CATEGORY_COLUMNS}", [*values.values(), ctx.clinic_id, category_id],
                       {"category_active_name_uniq": _duplicate_category()})
    if found is None:
        raise not_found("category")
    return dict(zip(_cols(CATEGORY_COLUMNS), found))


def _active_category(ctx, category_id):
    found = sql.row(ctx, 'SELECT is_active FROM "finance_category" WHERE clinic_id = %s AND id = %s',
                    [ctx.clinic_id, category_id])
    if found is None:
        raise not_found("category")
    if not found[0]:
        raise ApiError("invalid", _("This category isn't used any more."), fields={"category_id": "inactive"})


def category_by_code(ctx, code):
    """The active category with this default code, made again if the place removed it (stock purchases need one)."""
    found = sql.row(ctx, 'SELECT id FROM "finance_category" WHERE clinic_id = %s AND code = %s AND is_active '
                         "ORDER BY id LIMIT 1", [ctx.clinic_id, code])
    if found:
        return found[0]
    names = {c: (ar, en) for c, ar, en in DEFAULT_CATEGORIES}[code]
    name = names[0] if ctx.clinic.settings.get("print_language", "ar") == "ar" else names[1]
    return sql.row(ctx, """
        INSERT INTO "finance_category" (clinic_id, name, code, is_system, is_active, sort) VALUES (%s, %s, %s, true, true, 0)
        ON CONFLICT (clinic_id, name) WHERE is_active DO UPDATE SET code = EXCLUDED.code RETURNING id""",
                   [ctx.clinic_id, name, code])[0]


def add_expense(ctx, category_id, amount, day, note, from_shift, purchase=False, deltas=None):
    """An expense on a day (not after today), optionally paid from the open shift's drawer (lock order 8a, 10).

    With `deltas` the caller writes the clinic-day; otherwise this does. Returns the expense.
    """
    today = business_day(ctx.clinic)
    if day > today:
        raise ApiError("invalid", _("An expense can't be dated after today."), fields={"day": "future"})
    shift_id = shift_for(ctx, "cash") if from_shift else None
    if not purchase:
        _active_category(ctx, category_id)
    found = sql.row(ctx, f"""
        INSERT INTO "finance_expense" (clinic_id, category_id, amount, day, note, shift_id, purchase, created_at,
                                       created_by_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING {EXPENSE_COLUMNS}""", [
        ctx.clinic_id, category_id, amount, day, note, shift_id, purchase, timezone.now(), ctx.membership_id])
    own = deltas is None
    deltas = deltas or Deltas()
    deltas.clinic_day(day, expenses=amount)
    if own:
        deltas.write_clinics(ctx)
    return dict(zip(_cols(EXPENSE_COLUMNS), found))


def void_expense(ctx, expense_id):
    """One conditional update; a repeat is a harmless 200. A purchase's expense goes only with the purchase. Audited."""
    found = sql.row(ctx, 'SELECT purchase, voided_at FROM "finance_expense" WHERE clinic_id = %s AND id = %s',
                    [ctx.clinic_id, expense_id])
    if found is None:
        raise not_found("expense")
    if found[0]:
        raise ApiError("purchase_expense", _("This expense came from a stock purchase. Adjust the stock instead."), 409)
    changed = sql.row(ctx, 'UPDATE "finance_expense" SET voided_at = %s, voided_by_id = %s WHERE clinic_id = %s '
                           "AND id = %s AND voided_at IS NULL AND NOT purchase RETURNING amount, day",
                      [timezone.now(), ctx.membership_id, ctx.clinic_id, expense_id])
    if changed is None:
        return False
    amount, day = changed
    record_change(ctx, "expense.void", "expense", expense_id, before={"amount": amount, "day": day.isoformat()})
    Deltas().clinic_day(day, expenses=-amount).write_clinics(ctx)
    return True


def get_expense(ctx, expense_id):
    found = sql.row(ctx, f'SELECT {EXPENSE_COLUMNS} FROM "finance_expense" WHERE clinic_id = %s AND id = %s',
                    [ctx.clinic_id, expense_id])
    if found is None:
        raise not_found("expense")
    return dict(zip(_cols(EXPENSE_COLUMNS), found))


def expenses(ctx, first, last, category_id, size, after):
    """Expenses between two days, newest first, keyset on (day, id); with the period's total (not voided)."""
    where, params = "", []
    if category_id is not None:
        where, params = " AND category_id = %s", [category_id]
    page_where, page_params = "", []
    if after is not None:
        page_where, page_params = " AND (day, id) < (%s, %s)", list(after)
    items = sql.dicts(ctx, f'SELECT {EXPENSE_COLUMNS} FROM "finance_expense" WHERE clinic_id = %s AND day BETWEEN %s '
                           f"AND %s{where}{page_where} ORDER BY day DESC, id DESC LIMIT %s",
                      [ctx.clinic_id, first, last, *params, *page_params, size + 1])
    total = sql.row(ctx, f'SELECT coalesce(sum(amount), 0)::bigint FROM "finance_expense" WHERE clinic_id = %s '
                         f"AND day BETWEEN %s AND %s AND voided_at IS NULL{where}", [ctx.clinic_id, first, last, *params])[0]
    return items, total


# --- Staff pay (F4) --------------------------------------------------------------------------------------------


def _member(ctx, membership_id):
    found = sql.row(ctx, 'SELECT id, display_name, title, role, is_active FROM "clinics_membership" '
                         "WHERE clinic_id = %s AND id = %s", [ctx.clinic_id, membership_id])
    if found is None:
        raise not_found("member")
    return dict(zip(("id", "name", "title", "role", "is_active"), found))


def rules(ctx):
    """Every member with their pay rule (zeros when none is set). Two statements."""
    members = sql.dicts(ctx, 'SELECT id, display_name AS name, title, role, is_active FROM "clinics_membership" '
                             "WHERE clinic_id = %s ORDER BY is_active DESC, id", [ctx.clinic_id])
    found = {r["membership_id"]: r for r in sql.dicts(ctx, f'SELECT {RULE_COLUMNS} FROM "finance_pay_rule" '
                                                           "WHERE clinic_id = %s", [ctx.clinic_id])}
    return [{**m, "rule": rule_payload(found.get(m["id"]))} for m in members]


def rule_payload(rule):
    rule = rule or {"percent_bp": 0, "per_visit": 0, "per_procedure": 0, "monthly_salary": 0, "version": 0}
    return {"percent_bp": rule["percent_bp"], "per_visit": rule["per_visit"], "per_procedure": rule["per_procedure"],
            "monthly_salary": rule["monthly_salary"], "version": rule["version"]}


def set_rule(ctx, membership_id, values, expected_version):
    """Absolute values with the version they replace (0: no rule yet). A stale version is 409 stale. Audited."""
    _member(ctx, membership_id)
    names = ("percent_bp", "per_visit", "per_procedure", "monthly_salary")
    now = timezone.now()
    current = sql.row(ctx, f'SELECT {RULE_COLUMNS} FROM "finance_pay_rule" WHERE clinic_id = %s AND membership_id = %s '
                           "FOR NO KEY UPDATE", [ctx.clinic_id, membership_id])
    before = dict(zip(_cols(RULE_COLUMNS), current)) if current else None
    have = before["version"] if before else 0
    if have != expected_version:
        raise ApiError("stale", _("The pay rule changed meanwhile. Reload and try again."), 409,
                       fields={"version": have})
    if before is None:
        found = sql.row(ctx, f"""
            INSERT INTO "finance_pay_rule" (clinic_id, membership_id, percent_bp, per_visit, per_procedure, monthly_salary,
                                            version, updated_at, updated_by_id)
            VALUES (%s, %s, %s, %s, %s, %s, 1, %s, %s) ON CONFLICT (clinic_id, membership_id) DO NOTHING
            RETURNING {RULE_COLUMNS}""", [ctx.clinic_id, membership_id, *(values[n] for n in names), now,
                                          ctx.membership_id])
        if found is None:  # someone else made it first
            raise ApiError("stale", _("The pay rule changed meanwhile. Reload and try again."), 409,
                           fields={"version": 1})
    else:
        found = sql.row(ctx, f"""
            UPDATE "finance_pay_rule" SET percent_bp = %s, per_visit = %s, per_procedure = %s, monthly_salary = %s,
                   version = version + 1, updated_at = %s, updated_by_id = %s
             WHERE clinic_id = %s AND membership_id = %s AND version = %s RETURNING {RULE_COLUMNS}""", [
            *(values[n] for n in names), now, ctx.membership_id, ctx.clinic_id, membership_id, expected_version])
    after = dict(zip(_cols(RULE_COLUMNS), found))
    record_change(ctx, "pay_rule.set", "membership", membership_id,
                  before={n: before[n] for n in names} if before else None, after={n: after[n] for n in names})
    return after


def month_bounds(month):
    last = (month.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
    return month, last


def earned(rule, done, procedures, net):
    """What a rule pays for a month: salary + share of net revenue (half up) + per visit + per procedure."""
    rule = rule or {"percent_bp": 0, "per_visit": 0, "per_procedure": 0, "monthly_salary": 0}
    share = (net * rule["percent_bp"] + 5000) // 10000
    parts = {"salary": rule["monthly_salary"], "share": share, "visits": done * rule["per_visit"],
             "procedures": procedures * rule["per_procedure"]}
    return sum(parts.values()), parts


def statement(ctx, membership_id, month):
    """The month's statement (F4), from the doctor-day summaries: never a scan of visits. Four statements."""
    member = _member(ctx, membership_id)
    first, last = month_bounds(month)
    days = sql.dicts(ctx, 'SELECT day, done, procedures, collected, refunded FROM "summaries_doctor_day" '
                          "WHERE clinic_id = %s AND doctor_id = %s AND day BETWEEN %s AND %s "
                          "AND (done > 0 OR procedures > 0 OR collected > 0 OR refunded > 0) ORDER BY day",
                     [ctx.clinic_id, membership_id, first, last])
    rule = sql.dicts(ctx, f'SELECT {RULE_COLUMNS} FROM "finance_pay_rule" WHERE clinic_id = %s AND membership_id = %s',
                     [ctx.clinic_id, membership_id])
    payouts = sql.dicts(ctx, 'SELECT id, amount, method, shift_id, day, note, created_at FROM "finance_payout" '
                             "WHERE clinic_id = %s AND membership_id = %s AND month = %s ORDER BY id",
                        [ctx.clinic_id, membership_id, first])
    done = sum(d["done"] for d in days)
    procedures = sum(d["procedures"] for d in days)
    collected = sum(d["collected"] for d in days)
    refunded = sum(d["refunded"] for d in days)
    total, parts = earned(rule[0] if rule else None, done, procedures, collected - refunded)
    paid = sum(p["amount"] for p in payouts)
    return {"member": member, "month": first, "rule": rule_payload(rule[0] if rule else None), "days": days,
            "visits": done, "procedures": procedures, "collected": collected, "refunded": refunded,
            "revenue": collected - refunded, "earned": total, "parts": parts, "payouts": payouts, "paid": paid,
            "balance": total - paid}


def payout(ctx, membership_id, month, amount, method, note):
    """A payout against a month's statement. Cash comes out of the open shift (409 register_closed). Audited.

    Payouts aren't capped at the balance: an advance on next month's pay is a payout too.
    """
    _member(ctx, membership_id)
    shift_id = shift_for(ctx, method) if method == "cash" else None
    payout_id = sql.row(ctx, """
        INSERT INTO "finance_payout" (clinic_id, membership_id, month, amount, method, shift_id, day, note, created_at,
                                      created_by_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""", [
        ctx.clinic_id, membership_id, month, amount, method, shift_id, business_day(ctx.clinic), note, timezone.now(),
        ctx.membership_id])[0]
    record_change(ctx, "payout.add", "membership", membership_id, amount=amount, method=method,
                  month=month.isoformat(), payout_id=payout_id)
    return payout_id
