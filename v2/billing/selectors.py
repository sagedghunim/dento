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
    SELECT i.id, i.number, i.status, i.subtotal, i.discount, i.total, i.paid, i.created_at, i.voided_at,
           i.patient_id, p.full_name, p.phone, p.file_no, i.appointment_id, a.day, a.queue_no, a.doctor_id, a.service_id,
           m.display_name, m.title
      FROM "billing_invoice" i
      JOIN "patients_patient" p ON p.clinic_id = i.clinic_id AND p.id = i.patient_id
      LEFT JOIN "scheduling_appointment" a ON a.clinic_id = i.clinic_id AND a.id = i.appointment_id
      LEFT JOIN "clinics_membership" m ON m.clinic_id = a.clinic_id AND m.id = a.doctor_id
     WHERE i.clinic_id = %s AND i.id = %s"""


def _not_found():
    return ApiError("not_found", _("This invoice wasn't found."), 404)


def summary(invoice):
    """The short form actions answer with."""
    return {"id": invoice["id"], "number": invoice["number"], "status": invoice["status"],
            "subtotal": invoice["subtotal"], "discount": invoice["discount"], "total": invoice["total"],
            "paid": invoice["paid"], "due": invoice["total"] - invoice["paid"] if invoice["status"] == "open" else 0}


def payment_payload(clinic, p):
    return {"id": p["id"], "invoice_id": p["invoice_id"], "kind": p["kind"], "method": p["method"],
            "amount": p["amount"], "refunded": p["refunded"], "refund_of_id": p["refund_of_id"],
            "doctor_id": p["doctor_id"], "received_at": local_iso(clinic, p["received_at"]),
            "day": p["day"].isoformat(), "note": p["note"]}


def invoice(ctx, invoice_id):
    """The invoice with its patient, visit, lines and payments (three statements)."""
    found = sql.dicts(ctx, INVOICE_SQL, [ctx.clinic_id, invoice_id])
    if not found:
        raise _not_found()
    i = found[0]
    lines = sql.dicts(ctx, 'SELECT id, kind, service_id, item_id, description, qty, unit_price, amount, from_list, '
                           'created_by_id, created_at FROM "billing_invoice_line" '
                           'WHERE clinic_id = %s AND invoice_id = %s ORDER BY id',
                      [ctx.clinic_id, invoice_id])
    # Each line says whether this person may remove it (design 5.3), so the screen offers "Remove" only then.
    from .services import may_remove_line

    today = business_day(ctx.clinic)
    for line in lines:
        line["removable"] = i["status"] == "open" and may_remove_line(ctx, line, i["service_id"], today)
        line["created_at"] = local_iso(ctx.clinic, line["created_at"])
    payments = sql.dicts(ctx, "SELECT id, invoice_id, kind, method, amount, refunded, refund_of_id, doctor_id, received_at, "
                              'day, note FROM "billing_payment" WHERE clinic_id = %s AND invoice_id = %s ORDER BY id',
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
                          f'day, note FROM "billing_payment" {where} ORDER BY received_at DESC, id DESC LIMIT %s',
                     [*params, size + 1])
    more = len(rows) > size
    rows = rows[:size]
    return {"items": [payment_payload(ctx.clinic, r) for r in rows],
            "next": paging.encode_time(rows[-1]["received_at"], rows[-1]["id"]) if more and rows else None}


def payment(ctx, payment_id):
    found = sql.dicts(ctx, "SELECT id, invoice_id, kind, method, amount, refunded, refund_of_id, doctor_id, received_at, "
                           'day, note FROM "billing_payment" WHERE clinic_id = %s AND id = %s', [ctx.clinic_id, payment_id])
    if not found:
        raise ApiError("not_found", _("This payment wasn't found."), 404)
    return found[0]


def invoice_row(ctx, invoice_id):
    found = sql.row(ctx, 'SELECT id, number, status, subtotal, discount, total, paid, appointment_id '
                         'FROM "billing_invoice" WHERE clinic_id = %s AND id = %s', [ctx.clinic_id, invoice_id])
    if found is None:
        raise _not_found()
    return dict(zip(("id", "number", "status", "subtotal", "discount", "total", "paid", "appointment_id"), found))


def day_sums(ctx, day):
    """For the recompute: per doctor (None for payments without one), money in and refunded that day (no locks)."""
    return {r[0]: {"collected": r[1], "refunded": r[2]} for r in sql.rows(ctx, """
        SELECT doctor_id, coalesce(sum(amount) FILTER (WHERE kind = 'payment'), 0),
               coalesce(sum(amount) FILTER (WHERE kind = 'refund'), 0)
          FROM "billing_payment" WHERE clinic_id = %s AND day = %s GROUP BY doctor_id""", [ctx.clinic_id, day])}
