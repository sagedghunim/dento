"""Reading invoices and payments: the invoice, a receipt's data, a patient's payments.

`receipt(ctx, invoice_id)` is what the print page (`/print/receipts/<invoice>`) renders. It
only reads: the page must record the access event itself, with
`audit.services.record_access(ctx, data["patient"]["id"], AuditAccess.Action.PRINT)`.
"""

from django.utils.translation import gettext as _

from clinics import settings as clinic_settings
from core import paging, sql
from core.days import business_day, local_iso
from core.errors import ApiError

INVOICE_SQL = """
    SELECT i.id, i.number, i.status, i.subtotal, i.discount, i.total, i.paid, i.deposit, i.created_at, i.voided_at,
           i.patient_id, p.full_name, p.phone, p.file_no, i.appointment_id, a.day, a.queue_no, a.doctor_id, a.service_id,
           m.display_name, m.title, i.discount_type_id, i.discount_name, i.discount_percent,
           d.desk_can_apply AS discount_desk
      FROM "billing_invoice" i
      JOIN "patients_patient" p ON p.clinic_id = i.clinic_id AND p.id = i.patient_id
      LEFT JOIN "billing_discount_type" d ON d.clinic_id = i.clinic_id AND d.id = i.discount_type_id
      LEFT JOIN "scheduling_appointment" a ON a.clinic_id = i.clinic_id AND a.id = i.appointment_id
      LEFT JOIN "clinics_membership" m ON m.clinic_id = a.clinic_id AND m.id = a.doctor_id
     WHERE i.clinic_id = %s AND i.id = %s"""


def _not_found():
    return ApiError("not_found", _("This invoice wasn't found."), 404)


def summary(invoice):
    """The short form actions answer with. `due` is never negative: with a booking deposit more may be paid than
    is charged yet, and that is `credit` (the desk refunds it by hand). `deposit` is the deposit still held."""
    open_ = invoice["status"] == "open"
    return {"id": invoice["id"], "number": invoice["number"], "status": invoice["status"],
            "subtotal": invoice["subtotal"], "discount": invoice["discount"], "total": invoice["total"],
            "paid": invoice["paid"], "due": max(0, invoice["total"] - invoice["paid"]) if open_ else 0,
            "credit": max(0, invoice["paid"] - invoice["total"]) if open_ else 0,
            "deposit": invoice.get("deposit", 0)}


def payment_payload(clinic, p):
    return {"id": p["id"], "invoice_id": p["invoice_id"], "kind": p["kind"], "method": p["method"],
            "amount": p["amount"], "refunded": p["refunded"], "refund_of_id": p["refund_of_id"],
            "doctor_id": p["doctor_id"], "received_at": local_iso(clinic, p["received_at"]),
            "day": p["day"].isoformat(), "note": p["note"], "deposit": bool(p.get("is_deposit"))}


def invoice(ctx, invoice_id):
    """The invoice with its patient, visit, lines and payments (three statements)."""
    from scheduling.rules import patient_sql, visit_sql

    # A doctor: another doctor's patient's invoice, or one for another doctor's visit, is 404.
    mine, params = patient_sql(ctx, "i.clinic_id", "i.patient_id")
    own, own_params = visit_sql(ctx, "a.doctor_id")
    if own:
        own, own_params = " AND (a.id IS NULL OR a.doctor_id = %s)", own_params
    found = sql.dicts(ctx, INVOICE_SQL + mine + own, [ctx.clinic_id, invoice_id, *params, *own_params])
    if not found:
        raise _not_found()
    i = found[0]
    lines = sql.dicts(ctx, 'SELECT id, kind, service_id, item_id, description, qty, unit_price, amount, from_list, '
                           'created_by_id, created_at FROM "billing_invoice_line" '
                           'WHERE clinic_id = %s AND invoice_id = %s ORDER BY id',
                      [ctx.clinic_id, invoice_id])
    # Each line says whether this person may remove it (design 5.3), so the screen offers "Remove" only then.
    from .discounts import may_change
    from .services import may_remove_line

    today = business_day(ctx.clinic)
    for line in lines:
        line["removable"] = i["status"] == "open" and may_remove_line(ctx, line, i["service_id"], today)
        line["created_at"] = local_iso(ctx.clinic, line["created_at"])
    payments = sql.dicts(ctx, "SELECT id, invoice_id, kind, method, amount, refunded, refund_of_id, doctor_id, received_at, "
                              'day, note, is_deposit FROM "billing_payment" WHERE clinic_id = %s AND invoice_id = %s ORDER BY id',
                         [ctx.clinic_id, invoice_id])
    clinic = ctx.clinic
    body = summary(i)
    body.update({
        "created_at": local_iso(clinic, i["created_at"]),
        "voided_at": local_iso(clinic, i["voided_at"]) if i["voided_at"] else None,
        "patient": {"id": i["patient_id"], "name": i["full_name"], "phone": i["phone"], "file_no": i["file_no"]},
        "appointment": {"id": i["appointment_id"], "day": i["day"].isoformat(), "queue_no": i["queue_no"],
                        "doctor": {"id": i["doctor_id"], "name": i["display_name"], "title": i["title"]}}
        if i["appointment_id"] else None,
        "lines": lines,
        "payments": [payment_payload(clinic, p) for p in payments],
        # The discount type the invoice carries, as it was when applied (the receipt prints the name), and whether
        # this person may replace or remove the present discount (the server checks again).
        "discount_type": {"id": i["discount_type_id"], "name": i["discount_name"], "percent": i["discount_percent"]}
        if i["discount_type_id"] else None,
        "discount_changeable": i["status"] == "open" and may_change(ctx, i, i["discount_desk"]),
    })
    return body


def receipt(ctx, invoice_id):
    """What a printed receipt shows (design 7.5): the clinic, the print settings and the invoice."""
    data = invoice(ctx, invoice_id)
    clinic = ctx.clinic
    settings = clinic_settings.effective(clinic.settings)
    data["clinic"] = {"name": clinic.name, "city": clinic.city, "address": clinic.address, "phone": clinic.phone}
    data["print"] = {key: settings[key] for key in ("print_language", "digits", "letterhead", "letterhead_top_mm",
                                                    "letterhead_bottom_mm")}
    return data


def patient_payments(ctx, patient_id, size, after):
    """A patient's payments and refunds, newest first (index (clinic_id, patient_id, received_at DESC, id DESC))."""
    params = [ctx.clinic_id, patient_id]
    where = "WHERE clinic_id = %s AND patient_id = %s"
    if after:
        where += " AND (received_at, id) < (%s, %s)"
        params += [after[0], after[1]]
    rows = sql.dicts(ctx, "SELECT id, invoice_id, kind, method, amount, refunded, refund_of_id, doctor_id, received_at, "
                          f'day, note, is_deposit FROM "billing_payment" {where} ORDER BY received_at DESC, id DESC LIMIT %s',
                     [*params, size + 1])
    more = len(rows) > size
    rows = rows[:size]
    return {"items": [payment_payload(ctx.clinic, r) for r in rows],
            "next": paging.encode_time(rows[-1]["received_at"], rows[-1]["id"]) if more and rows else None}


def payment(ctx, payment_id):
    found = sql.dicts(ctx, "SELECT id, invoice_id, kind, method, amount, refunded, refund_of_id, doctor_id, received_at, "
                           'day, note, is_deposit FROM "billing_payment" WHERE clinic_id = %s AND id = %s',
                      [ctx.clinic_id, payment_id])
    if not found:
        raise ApiError("not_found", _("This payment wasn't found."), 404)
    return found[0]


def invoice_row(ctx, invoice_id):
    found = sql.row(ctx, 'SELECT id, number, status, subtotal, discount, total, paid, appointment_id, deposit '
                         'FROM "billing_invoice" WHERE clinic_id = %s AND id = %s', [ctx.clinic_id, invoice_id])
    if found is None:
        raise _not_found()
    return dict(zip(("id", "number", "status", "subtotal", "discount", "total", "paid", "appointment_id", "deposit"),
                    found))


def day_sums(ctx, day):
    """For the recompute: per doctor (None for payments without one), money in and refunded that day (no locks).
    Credit moved between bills is no money in or out."""
    return {r[0]: {"collected": r[1], "refunded": r[2]} for r in sql.rows(ctx, """
        SELECT doctor_id, coalesce(sum(amount) FILTER (WHERE kind = 'payment'), 0),
               coalesce(sum(amount) FILTER (WHERE kind = 'refund'), 0)
          FROM "billing_payment" WHERE clinic_id = %s AND day = %s AND method <> 'credit'
         GROUP BY doctor_id""", [ctx.clinic_id, day])}


def day_money(ctx, day):
    """For the recompute: money in and refunded that day by (section, method), section 0 for none (no locks)."""
    return {(r[0], r[1]): {"collected": r[2], "refunded": r[3]} for r in sql.rows(ctx, """
        SELECT coalesce(section_id, 0), method, coalesce(sum(amount) FILTER (WHERE kind = 'payment'), 0),
               coalesce(sum(amount) FILTER (WHERE kind = 'refund'), 0)
          FROM "billing_payment" WHERE clinic_id = %s AND day = %s AND method <> 'credit'
         GROUP BY 1, 2""", [ctx.clinic_id, day])}


BALANCE_LIMIT = 50  # invoices listed; the total covers them all


def credits(ctx, patient_ids):
    """What the clinic holds for each of these patients (Saged, 2026-11-24): {patient_id: piastres}, holders only.
    A bill's credit is what is paid over its total, beyond a booking deposit still held (billing.services.credit_of).
    """
    if not patient_ids:
        return {}
    return dict(sql.rows(ctx, """
        SELECT patient_id, sum(paid - greatest(total, deposit))::bigint FROM "billing_invoice"
         WHERE clinic_id = %s AND patient_id = ANY(%s::bigint[]) AND status = 'open' AND paid > greatest(total, deposit)
         GROUP BY patient_id""", [ctx.clinic_id, sorted(set(patient_ids))]))


def patient_credits(ctx, patient_id):
    """What the clinic holds for a patient (Saged, 2026-11-22): open bills paid over their total, each given back
    from the drawer with POST /invoices/{id}/give-back. Oldest first; a handful at most."""
    return sql.dicts(ctx, """
        SELECT i.id, i.number, i.paid - greatest(i.total, i.deposit) AS credit FROM "billing_invoice" i
         WHERE i.clinic_id = %s AND i.patient_id = %s AND i.status = 'open' AND i.paid > greatest(i.total, i.deposit)
         ORDER BY i.created_at, i.id LIMIT 20""", [ctx.clinic_id, patient_id])


def invoice_credit(ctx, invoice_id):
    """One open bill paid over its total, with its patient's name, or None (the drawer's give-back question)."""
    found = sql.dicts(ctx, """
        SELECT i.id, i.number, i.paid - greatest(i.total, i.deposit) AS credit, p.id AS patient_id, p.full_name AS patient
          FROM "billing_invoice" i
          JOIN "patients_patient" p ON p.clinic_id = i.clinic_id AND p.id = i.patient_id
         WHERE i.clinic_id = %s AND i.id = %s AND i.status = 'open'
           AND i.paid > greatest(i.total, i.deposit)""", [ctx.clinic_id, invoice_id])
    return found[0] if found else None


def patient_balance(ctx, patient_id):
    """What a patient owes across visits (design E3): open invoices with something left, oldest first."""
    rows = sql.dicts(ctx, """
        SELECT i.id, i.number, i.appointment_id, a.day, i.total, i.paid, i.created_at
          FROM "billing_invoice" i
          LEFT JOIN "scheduling_appointment" a ON a.clinic_id = i.clinic_id AND a.id = i.appointment_id
         WHERE i.clinic_id = %s AND i.patient_id = %s AND i.status = 'open' AND i.paid < i.total
         ORDER BY i.created_at, i.id""", [ctx.clinic_id, patient_id])
    return {"owed": sum(r["total"] - r["paid"] for r in rows), "count": len(rows),
            "invoices": [{"id": r["id"], "number": r["number"], "appointment_id": r["appointment_id"],
                          "day": r["day"].isoformat() if r["day"] else None, "total": r["total"], "paid": r["paid"],
                          "due": r["total"] - r["paid"]} for r in rows[:BALANCE_LIMIT]]}


def balances(ctx, patient_ids, day=None):
    """What each of these patients owes from before (desk review 3): {patient_id: piastres}, owing patients only.

    One grouped statement on the invoice_unpaid index, whatever the number of patients. With `day`, the
    invoices of that day's visits are left out: their rows show them as this visit's "owes".
    """
    if not patient_ids:
        return {}
    return dict(sql.rows(ctx, """
        SELECT i.patient_id, sum(i.total - i.paid)::bigint
          FROM "billing_invoice" i
         WHERE i.clinic_id = %s AND i.patient_id = ANY(%s::bigint[]) AND i.status = 'open' AND i.paid < i.total
           AND (%s::date IS NULL OR i.appointment_id IS NULL OR NOT EXISTS (
                SELECT 1 FROM "scheduling_appointment" a
                 WHERE a.clinic_id = i.clinic_id AND a.id = i.appointment_id AND a.day = %s::date))
         GROUP BY i.patient_id""", [ctx.clinic_id, sorted(set(patient_ids)), day, day]))


def visit_invoice(ctx, appointment_id):
    """The visit's open invoice with its lines, for the finish dialog: lines, total, paid and remaining."""
    found = sql.row(ctx, 'SELECT id FROM "billing_invoice" WHERE clinic_id = %s AND appointment_id = %s '
                         "AND status = 'open'", [ctx.clinic_id, appointment_id])
    if found is None:
        return None
    body = invoice(ctx, found[0])
    body["remaining"] = body["due"]  # never negative: a deposit over the total is the bill's `credit`
    return body


# --- Session packages (F7) -------------------------------------------------------------------------------------

PATIENT_PACKAGES_LIMIT = 50


def patient_package_payload(clinic, p):
    return {"id": p["id"], "patient_id": p["patient_id"], "package_id": p["package_id"],
            "procedure_id": p["procedure_id"], "name": p["name"], "sessions": p["sessions"], "used": p["used"],
            "left": p["sessions"] - p["used"] if p["voided_at"] is None else 0, "price": p["price"],
            "invoice_id": p["invoice_id"], "appointment_id": p["appointment_id"],
            "sold_at": local_iso(clinic, p["sold_at"]), "voided": p["voided_at"] is not None}


def patient_package(ctx, patient_package_id):
    from .services import PATIENT_PACKAGE_COLUMNS, _patient_package

    found = sql.row(ctx, f'SELECT {PATIENT_PACKAGE_COLUMNS} FROM "billing_patient_package" WHERE clinic_id = %s '
                         "AND id = %s", [ctx.clinic_id, patient_package_id])
    if found is None:
        raise ApiError("not_found", _("This package wasn't found."), 404)
    return patient_package_payload(ctx.clinic, _patient_package(found))


def patient_packages(ctx, patient_id):
    """A patient's packages, those with sessions left first, then newest (one statement, at most 50)."""
    from .services import PATIENT_PACKAGE_COLUMNS, _patient_package

    return [patient_package_payload(ctx.clinic, _patient_package(r)) for r in sql.rows(ctx, f"""
        SELECT {PATIENT_PACKAGE_COLUMNS} FROM "billing_patient_package" WHERE clinic_id = %s AND patient_id = %s
         ORDER BY (used < sessions AND voided_at IS NULL) DESC, sold_at DESC, id DESC LIMIT %s""",
                                                                            [ctx.clinic_id, patient_id,
                                                                             PATIENT_PACKAGES_LIMIT])]
