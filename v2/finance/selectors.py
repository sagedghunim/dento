"""Money reports (design F5): receivables, revenue and profit. Reads only, no locks.

Revenue and profit read the summaries (doctor-days, money-days, clinic-days), never a scan of payments or
visits, so a report costs the same in a clinic's tenth year as in its first. Receivables read the partial
`invoice_unpaid` index, which holds only invoices with something owed.
"""

import datetime as dt

from django.utils.translation import gettext as _

from core import sql
from core.errors import ApiError

from . import services

REVENUE_BY = ("day", "month", "doctor", "section", "method")
MAX_DAYS = 400  # a year and a bit of days in one report
MAX_MONTHS = 36


def parse_month(value, name="month"):
    """'YYYY-MM' -> the first day of that month."""
    if isinstance(value, str):
        try:
            year, month = value.strip().split("-")
            if len(year) == 4 and len(month) == 2:
                return dt.date(int(year), int(month), 1)
        except ValueError:
            pass
    raise ApiError("invalid", f"{name} must be a month (YYYY-MM)", fields={name: "month"})


def check_range(first, last, by):
    if last < first:
        raise ApiError("invalid", _("The end date is before the start date."), fields={"to": "before_from"})
    days = (last - first).days + 1
    limit = MAX_MONTHS * 31 if by in ("month", "doctor", "section", "method") else MAX_DAYS
    if days > limit:
        raise ApiError("invalid", _("Pick a shorter period."), fields={"to": "range"})


def receivables(ctx, size, after):
    """Each patient who owes, with the amount, the oldest unpaid invoice's date, its age in days and the last
    visit; oldest debt first, keyset on (oldest, patient). Plus the total owed. Two statements."""
    where, params = "", []
    if after is not None:
        where, params = " WHERE (o.oldest, o.patient_id) > (%s, %s)", list(after)
    rows = sql.dicts(ctx, f"""
        WITH o AS (
            SELECT patient_id, sum(total - paid)::bigint AS owed, count(*) AS invoices, min(created_at) AS oldest
              FROM "billing_invoice" WHERE clinic_id = %s AND status = 'open' AND paid < total GROUP BY patient_id)
        SELECT o.patient_id, p.full_name AS name, p.phone, p.file_no, o.owed, o.invoices, o.oldest, s.last_day
          FROM o JOIN "patients_patient" p ON p.clinic_id = %s AND p.id = o.patient_id
          LEFT JOIN "summaries_patient" s ON s.clinic_id = %s AND s.patient_id = o.patient_id{where}
         ORDER BY o.oldest, o.patient_id LIMIT %s""", [ctx.clinic_id, ctx.clinic_id, ctx.clinic_id, *params, size + 1])
    total = sql.row(ctx, "SELECT coalesce(sum(total - paid), 0)::bigint, count(DISTINCT patient_id) FROM \"billing_invoice\" "
                         "WHERE clinic_id = %s AND status = 'open' AND paid < total", [ctx.clinic_id])
    return rows, {"owed": total[0], "patients": total[1]}


def revenue(ctx, first, last, by):
    """Collected, refunded and net between two payment days, grouped `by` day, month, doctor, section or method.

    Day, month and doctor read the doctor-days (money attributed to the visit's doctor, F4); section and method
    read the money-days. Money taken with no doctor (an invoice without a visit) is only in section and method.
    """
    if by in ("day", "month", "doctor"):
        key = {"day": "day", "month": "date_trunc('month', day)::date", "doctor": "doctor_id"}[by]
        rows = sql.rows(ctx, f"""
            SELECT {key}, sum(collected), sum(refunded) FROM "summaries_doctor_day"
             WHERE clinic_id = %s AND day BETWEEN %s AND %s AND (collected > 0 OR refunded > 0)
             GROUP BY 1 ORDER BY 1""", [ctx.clinic_id, first, last])
    else:
        key = "section_id" if by == "section" else "method"
        rows = sql.rows(ctx, f"""
            SELECT {key}, sum(collected), sum(refunded) FROM "summaries_money_day"
             WHERE clinic_id = %s AND day BETWEEN %s AND %s GROUP BY 1 ORDER BY 1""", [ctx.clinic_id, first, last])
    items = []
    for value, collected, refunded in rows:
        if isinstance(value, dt.date):
            value = value.isoformat()[:7] if by == "month" else value.isoformat()
        elif by == "section" and value == 0:
            value = None
        items.append({"key": value, "collected": int(collected), "refunded": int(refunded),
                      "net": int(collected - refunded)})
    return {"by": by, "from": first.isoformat(), "to": last.isoformat(), "items": items,
            "collected": sum(i["collected"] for i in items), "refunded": sum(i["refunded"] for i in items),
            "net": sum(i["net"] for i in items)}


def staff_pay(ctx, first, last):
    """What every member with a pay rule earned in the month, from the doctor-days (F4). Two statements."""
    rules = {r["membership_id"]: r for r in sql.dicts(ctx, f'SELECT {services.RULE_COLUMNS} FROM "finance_pay_rule" '
                                                           "WHERE clinic_id = %s", [ctx.clinic_id])}
    if not rules:
        return {}
    work = {r[0]: r[1:] for r in sql.rows(ctx, """
        SELECT doctor_id, sum(done), sum(procedures), sum(collected) - sum(refunded) FROM "summaries_doctor_day"
         WHERE clinic_id = %s AND day BETWEEN %s AND %s AND doctor_id = ANY(%s::bigint[]) GROUP BY doctor_id""",
                                          [ctx.clinic_id, first, last, list(rules)])}
    pay = {}
    for membership_id, rule in rules.items():
        done, procedures, net = work.get(membership_id, (0, 0, 0))
        pay[membership_id] = services.earned(rule, int(done), int(procedures), int(net))[0]
    return pay


def profit(ctx, month):
    """The month's profit (F5): revenue - refunds - expenses - staff pay. Revenue and expenses from the
    clinic-days; staff pay is what the rules earned (accrued), with the payouts made shown beside it."""
    first, last = services.month_bounds(month)
    collected, refunded, expenses = sql.row(ctx, """
        SELECT coalesce(sum(collected), 0), coalesce(sum(refunded), 0), coalesce(sum(expenses), 0)
          FROM "summaries_clinic_day" WHERE clinic_id = %s AND day BETWEEN %s AND %s""", [ctx.clinic_id, first, last])
    pay = staff_pay(ctx, first, last)
    paid_out = sql.row(ctx, 'SELECT coalesce(sum(amount), 0) FROM "finance_payout" WHERE clinic_id = %s AND month = %s',
                       [ctx.clinic_id, first])[0]
    staff = sum(pay.values())
    return {"month": first.isoformat()[:7], "collected": int(collected), "refunded": int(refunded),
            "revenue": int(collected - refunded), "expenses": int(expenses), "staff_pay": staff,
            "staff_paid_out": int(paid_out), "profit": int(collected - refunded - expenses) - staff,
            "staff": [{"membership_id": m, "earned": v} for m, v in sorted(pay.items())]}
