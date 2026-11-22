"""Installments (Saged, 2026-11-19): what a patient owes, paid in parts on set days.

A plan takes what the patient owes now (their open bills) and splits it: a down payment today, then equal parts
every week, two weeks or month, or parts the desk types one by one. Nothing is moved on the bills: payments are
taken on the bills as always, and those on the plan's bills from the moment it was made cover its parts in order.
So a part is paid, partly paid, due today, late or still ahead, worked out when it is read.

A new visit after the plan has its own bill and is paid on its own. One running plan per patient.
"""

import calendar
import datetime as dt

from django.utils import timezone
from django.utils.translation import gettext as _

from audit.services import record_change
from core import sql
from core.days import business_day
from core.errors import ApiError

EVERY = ("week", "2weeks", "month")
MAX_PARTS = 60


def _add_months(day, months):
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    return dt.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _step(day, every, n):
    if every == "week":
        return day + dt.timedelta(days=7 * n)
    if every == "2weeks":
        return day + dt.timedelta(days=14 * n)
    return _add_months(day, n)


def _owed(ctx, patient_id):
    """What the patient owes on their open bills now."""
    return int(sql.row(ctx, 'SELECT coalesce(sum(total - paid), 0) FROM "billing_invoice" WHERE clinic_id = %s '
                            "AND patient_id = %s AND status = 'open' AND paid < total", [ctx.clinic_id, patient_id])[0])


def schedule(owed, down, every, amount, first_due):
    """[(due_on, amount)]: the down payment today (when any), then `amount` each step from `first_due`, the last
    one smaller. Refused when the parts can't cover what is owed sensibly."""
    if not 0 <= down < owed:
        raise ApiError("invalid", _("The down payment must be less than what is owed."), fields={"down": "range"})
    rest = owed - down
    if amount <= 0:
        raise ApiError("invalid", _("Enter the amount of each installment."), fields={"amount": "required"})
    count = -(-rest // amount)
    if count > MAX_PARTS:
        raise ApiError("invalid", _("That makes too many installments. Raise the amount."), fields={"amount": "too_small"})
    parts = [(first_due, down)] if down else []
    for n in range(count):
        parts.append((_step(first_due, every, n), min(amount, rest - n * amount)))
    return parts


def make(ctx, patient_id, request):
    """POST patients/{id}/installments. `request`: down, every, amount, first_due, or `parts` [{due_on, amount}]
    typed one by one (they must add up to what is owed). Returns the plan as read."""
    found = sql.row(ctx, 'SELECT id FROM "patients_patient" WHERE clinic_id = %s AND id = %s FOR NO KEY UPDATE',
                    [ctx.clinic_id, patient_id])  # one plan at a time for this patient
    if found is None:
        raise ApiError("not_found", _("This patient wasn't found."), 404)
    owed = _owed(ctx, patient_id)
    if owed <= 0:
        raise ApiError("nothing_owed", _("This patient owes nothing to split."), 409)
    if sql.row(ctx, 'SELECT 1 FROM "billing_installment_plan" WHERE clinic_id = %s AND patient_id = %s AND status = %s',
               [ctx.clinic_id, patient_id, "active"]):
        raise ApiError("plan_exists", _("This patient already has installments. Cancel them first."), 409)
    today = business_day(ctx.clinic)
    if request.get("parts"):
        parts = sorted((p["due_on"], p["amount"]) for p in request["parts"])
        if any(amount <= 0 for _day, amount in parts) or len(parts) > MAX_PARTS:
            raise ApiError("invalid", _("Each installment needs an amount."), fields={"parts": "amount"})
        if sum(amount for _day, amount in parts) != owed:
            raise ApiError("invalid", _("The installments must add up to what is owed."), 409,
                           fields={"parts": "sum"}, extra={"owed": owed})
    else:
        first_due = request.get("first_due") or _step(today, request["every"], 1)
        parts = schedule(owed, request.get("down") or 0, request["every"], request["amount"], first_due)
        if request.get("down"):
            parts[0] = (today, parts[0][1])
    if any(day < today for day, _amount in parts):
        raise ApiError("invalid", _("An installment can't be due before today."), fields={"parts": "past"})
    plan_id = sql.row(ctx, """
        INSERT INTO "billing_installment_plan" (clinic_id, patient_id, total, status, note, created_at, created_by_id)
        VALUES (%s, %s, %s, 'active', %s, %s, %s) RETURNING id""", [
        ctx.clinic_id, patient_id, owed, (request.get("note") or "")[:200], timezone.now(), ctx.membership_id])[0]
    values, params = [], []
    for position, (day, amount) in enumerate(parts):
        values.append("(%s, %s, %s, %s, %s, %s)")
        params += [ctx.clinic_id, plan_id, patient_id, position, day, amount]
    sql.run(ctx, 'INSERT INTO "billing_installment" (clinic_id, plan_id, patient_id, position, due_on, amount) '
                 f"VALUES {', '.join(values)}", params)
    record_change(ctx, "installment_plan.create", "installment_plan", plan_id,
                  after={"total": owed, "parts": [[d.isoformat(), a] for d, a in parts]})
    return read(ctx, patient_id)


def split_evenly(rest, count):
    """`rest` over `count` parts, each rounded up to a whole pound, the last smaller (never an empty part)."""
    each = -(-rest // count)
    each = -(-each // 100) * 100
    out = []
    while rest > 0 and len(out) < count:
        out.append(min(each, rest))
        rest -= out[-1]
    return out


def respread(parts, covered, total, today):
    """[(due_on, amount)] after money paid ahead (Saged, 2026-11-22, choice "a"): the parts due by today stay; what
    was paid beyond them is one part today, already paid; what is left is split evenly over the days of the parts
    still to come that weren't paid before. `parts` are the plan's parts with what of each was paid before this
    payment (`_states`); `covered` is everything paid on the plan now. None when nothing is left to split."""
    kept = [(p["due_on"], p["amount"]) for p in parts if p["due_on"] <= today]
    ahead = covered - sum(a for _d, a in kept)
    days = [p["due_on"] for p in parts if p["due_on"] > today and p["paid"] < p["amount"]]
    rest = total - covered
    if ahead <= 0 or rest <= 0 or not days:
        return None
    return [*kept, (today, ahead), *zip(days, split_evenly(rest, len(days)))]


def pay(ctx, patient_id, amount, method):
    """POST patients/{id}/installments/payments (Saged, 2026-11-22): the patient pays any amount toward the plan.
    It is taken on the plan's bills, the oldest first. When it is more than what is due by today, what is left is
    split again evenly over the parts still to come, so each of them gets smaller. Returns (plan, payments)."""
    from summaries.services import Deltas

    from .services import collect_earlier, lock_patient_invoices

    if sql.row(ctx, 'SELECT id FROM "patients_patient" WHERE clinic_id = %s AND id = %s FOR NO KEY UPDATE',
               [ctx.clinic_id, patient_id]) is None:
        raise ApiError("not_found", _("This patient wasn't found."), 404)
    found = sql.dicts(ctx, 'SELECT id, total, created_at FROM "billing_installment_plan" WHERE clinic_id = %s '
                           "AND patient_id = %s AND status = 'active'", [ctx.clinic_id, patient_id])
    if not found:
        raise ApiError("not_found", _("This patient has no installments."), 404)
    plan = found[0]
    today = business_day(ctx.clinic)
    covered = _covered(ctx, [plan["id"]]).get(plan["id"], 0)
    before = _states(_parts(ctx, [plan["id"]]).get(plan["id"], []), covered, today)
    for p in before:
        p["due_on"] = dt.date.fromisoformat(p["due_on"])
    if amount > plan["total"] - covered:
        raise ApiError("over_balance", _("The amount is more than what is left of the installments."), 409,
                       fields={"amount": "over_balance"})
    bills = [i for i in lock_patient_invoices(ctx, patient_id, 0) if i["created_at"] <= plan["created_at"]]
    deltas = Deltas()
    payments = collect_earlier(ctx, bills, 0, amount, method, deltas, today, note=_("Installment"))
    deltas.write_doctors(ctx)
    deltas.write_clinics(ctx)
    due_now = sum(p["amount"] - p["paid"] for p in before if p["due_on"] <= today)
    if amount > due_now:
        parts = respread(before, covered + amount, plan["total"], today)
        if parts is not None:
            sql.run(ctx, 'DELETE FROM "billing_installment" WHERE clinic_id = %s AND plan_id = %s',
                    [ctx.clinic_id, plan["id"]])
            values, params = [], []
            for position, (day, part) in enumerate(parts):
                values.append("(%s, %s, %s, %s, %s, %s)")
                params += [ctx.clinic_id, plan["id"], patient_id, position, day, part]
            sql.run(ctx, 'INSERT INTO "billing_installment" (clinic_id, plan_id, patient_id, position, due_on, amount) '
                         f"VALUES {', '.join(values)}", params)
            record_change(ctx, "installment_plan.respread", "installment_plan", plan["id"],
                          before={"parts": [[p["due_on"].isoformat(), p["amount"]] for p in before]},
                          after={"parts": [[d.isoformat(), a] for d, a in parts]}, patient_id=patient_id)
    return read(ctx, patient_id), payments


def cancel(ctx, patient_id):
    """DELETE patients/{id}/installments: the running plan stops; what is owed stays on the bills."""
    found = sql.row(ctx, 'UPDATE "billing_installment_plan" SET status = %s, ended_at = %s WHERE clinic_id = %s '
                         "AND patient_id = %s AND status = 'active' RETURNING id", [
        "cancelled", timezone.now(), ctx.clinic_id, patient_id])
    if found is None:
        raise ApiError("not_found", _("This patient has no installments."), 404)
    record_change(ctx, "installment_plan.cancel", "installment_plan", found[0])
    return read(ctx, patient_id)


def _covered(ctx, plans):
    """{plan id: what was paid on its bills since it was made}: payments less refunds, one statement."""
    if not plans:
        return {}
    rows = sql.rows(ctx, """
        SELECT pl.id, coalesce(sum(CASE WHEN y.kind = 'refund' THEN -y.amount ELSE y.amount END), 0)
          FROM "billing_installment_plan" pl
          JOIN "billing_invoice" i ON i.clinic_id = pl.clinic_id AND i.patient_id = pl.patient_id
                                  AND i.created_at <= pl.created_at
          JOIN "billing_payment" y ON y.clinic_id = i.clinic_id AND y.invoice_id = i.id AND y.received_at >= pl.created_at
         WHERE pl.clinic_id = %s AND pl.id = ANY(%s::bigint[]) GROUP BY pl.id""", [ctx.clinic_id, list(plans)])
    return {plan_id: int(amount) for plan_id, amount in rows}


def _parts(ctx, plan_ids):
    found = {}
    for r in sql.rows(ctx, 'SELECT plan_id, position, due_on, amount FROM "billing_installment" WHERE clinic_id = %s '
                           "AND plan_id = ANY(%s::bigint[]) ORDER BY plan_id, position", [ctx.clinic_id, list(plan_ids)]):
        found.setdefault(r[0], []).append({"position": r[1], "due_on": r[2], "amount": r[3]})
    return found


def _states(parts, covered, today):
    """Each part with what of it is paid and its state; the money paid goes to the earliest parts first."""
    left, out = covered, []
    for p in parts:
        paid = max(0, min(p["amount"], left))
        left -= paid
        state = ("paid" if paid >= p["amount"] else "late" if p["due_on"] < today
                 else "today" if p["due_on"] == today else "ahead")
        out.append({**p, "due_on": p["due_on"].isoformat(), "paid": paid, "state": state})
    return out


def read(ctx, patient_id):
    """The running plan (or the last one) with its parts and their states, or None."""
    row = sql.dicts(ctx, 'SELECT id, total, status, note, created_at FROM "billing_installment_plan" '
                         "WHERE clinic_id = %s AND patient_id = %s ORDER BY (status = 'active') DESC, id DESC LIMIT 1",
                    [ctx.clinic_id, patient_id])
    if not row:
        return None
    plan = row[0]
    today = business_day(ctx.clinic)
    covered = _covered(ctx, [plan["id"]]).get(plan["id"], 0)
    parts = _states(_parts(ctx, [plan["id"]]).get(plan["id"], []), covered, today)
    late = sum(p["amount"] - p["paid"] for p in parts if p["state"] == "late")
    return {"id": plan["id"], "total": plan["total"], "status": plan["status"], "note": plan["note"],
            "created_at": plan["created_at"].isoformat(), "paid": min(covered, plan["total"]),
            "left": max(0, plan["total"] - covered), "late": late, "parts": parts,
            "finished": covered >= plan["total"]}


def due(ctx, today=None, limit=50):
    """Running plans with a part due today or late and not paid: [{patient, amount due today, late}], for Today."""
    today = today or business_day(ctx.clinic)
    plans = sql.dicts(ctx, """
        SELECT pl.id, pl.patient_id, pl.total, p.full_name, p.file_no FROM "billing_installment_plan" pl
          JOIN "patients_patient" p ON p.clinic_id = pl.clinic_id AND p.id = pl.patient_id
         WHERE pl.clinic_id = %s AND pl.status = 'active'
           AND EXISTS (SELECT 1 FROM "billing_installment" i WHERE i.clinic_id = pl.clinic_id AND i.plan_id = pl.id
                        AND i.due_on <= %s)
         ORDER BY pl.id LIMIT %s""", [ctx.clinic_id, today, limit])
    if not plans:
        return []
    covered = _covered(ctx, [p["id"] for p in plans])
    parts = _parts(ctx, [p["id"] for p in plans])
    out = []
    for p in plans:
        states = _states(parts.get(p["id"], []), covered.get(p["id"], 0), today)
        late = sum(s["amount"] - s["paid"] for s in states if s["state"] == "late")
        now = sum(s["amount"] - s["paid"] for s in states if s["state"] == "today")
        if late or now:
            out.append({"patient_id": p["patient_id"], "name": p["full_name"], "file_no": p["file_no"],
                        "today": now, "late": late})
    return out


def late_by_patient(ctx, patient_ids, today=None):
    """{patient id: late amount} for the patients given (search results, Book's warning)."""
    if not patient_ids:
        return {}
    today = today or business_day(ctx.clinic)
    plans = dict(sql.rows(ctx, 'SELECT id, patient_id FROM "billing_installment_plan" WHERE clinic_id = %s '
                               "AND status = 'active' AND patient_id = ANY(%s::bigint[])", [ctx.clinic_id, list(patient_ids)]))
    if not plans:
        return {}
    covered, parts = _covered(ctx, plans), _parts(ctx, plans)
    out = {}
    for plan_id, patient_id in plans.items():
        late = sum(s["amount"] - s["paid"] for s in _states(parts.get(plan_id, []), covered.get(plan_id, 0), today)
                   if s["state"] == "late")
        if late:
            out[patient_id] = late
    return out
