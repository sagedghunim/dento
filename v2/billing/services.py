"""Invoices and payments (design 5.3). Every function runs inside the request's shard transaction.

Lock order (design 5.0): the appointment, then the invoice, then the payment row, then the
invoice counter, then doctor-days, then clinic-days. Invoice endpoints read the invoice
without a lock first only to learn its appointment, which never changes.
"""

from django.utils import timezone
from django.utils.translation import gettext as _

from audit.services import record_change
from clinics.services import take_numbers
from core import sql
from core.days import business_day
from core.errors import ApiError
from summaries.services import Deltas

APPOINTMENT_LOCK_SQL = """
    SELECT a.id, a.status, a.day, a.doctor_id, a.patient_id, a.service_id, a.fee, a.is_follow_up, s.name
      FROM "scheduling_appointment" a
      JOIN "clinics_service" s ON s.clinic_id = a.clinic_id AND s.id = a.service_id
     WHERE a.clinic_id = %s AND a.id = %s FOR NO KEY UPDATE OF a"""
INVOICE_COLUMNS = "id, number, status, patient_id, appointment_id, subtotal, discount, total, paid"


def _invoice(values):
    return dict(zip(("id", "number", "status", "patient_id", "appointment_id", "subtotal", "discount", "total", "paid"),
                    values))


def not_found(what):
    messages = {
        "appointment": _("This appointment wasn't found."),
        "invoice": _("This invoice wasn't found."),
        "payment": _("This payment wasn't found."),
        "line": _("This line wasn't found."),
        "service": _("This service wasn't found."),
    }
    return ApiError("not_found", messages[what], 404)


def lock_appointment(ctx, appointment_id):
    found = sql.row(ctx, APPOINTMENT_LOCK_SQL, [ctx.clinic_id, appointment_id])
    if found is None:
        raise not_found("appointment")
    return dict(zip(("id", "status", "day", "doctor_id", "patient_id", "service_id", "fee", "is_follow_up",
                     "service_name"), found))


def open_invoice(ctx, appointment_id, lock=True):
    found = sql.row(ctx, f'SELECT {INVOICE_COLUMNS} FROM "billing_invoice" WHERE clinic_id = %s AND appointment_id = %s '
                         "AND status = 'open'" + (" FOR NO KEY UPDATE" if lock else ""), [ctx.clinic_id, appointment_id])
    return _invoice(found) if found else None


def line_description(appointment):
    return appointment["service_name"][:200]


def create_invoice(ctx, appointment, number, paid=0):
    """The visit's invoice with one line for its service at the appointment's fee (one statement)."""
    fee = appointment["fee"]
    now = timezone.now()
    found = sql.row(ctx, f"""
        WITH inv AS (
            INSERT INTO "billing_invoice" (clinic_id, number, patient_id, appointment_id, status, subtotal, discount,
                                           total, paid, created_at, created_by_id)
            VALUES (%s, %s, %s, %s, 'open', %s, 0, %s, %s, %s, %s) RETURNING {INVOICE_COLUMNS}),
        line AS (
            INSERT INTO "billing_invoice_line" (clinic_id, invoice_id, kind, service_id, description, qty, unit_price,
                                                amount, from_list, created_by_id, created_at)
            SELECT %s, inv.id, 'service', %s, %s, 1, %s, %s, true, %s, %s FROM inv)
        SELECT {INVOICE_COLUMNS} FROM inv""", [
        ctx.clinic_id, number, appointment["patient_id"], appointment["id"], fee, fee, paid, now, ctx.membership_id,
        ctx.clinic_id, appointment["service_id"], line_description(appointment), fee, fee, ctx.membership_id, now])
    return _invoice(found)


def ensure_invoice(ctx, appointment, number=None, paid=0):
    """The visit's open invoice, created if missing. Call it under the appointment's lock.

    Returns (invoice, created). `number` is an invoice number already taken (booking takes
    it before the summary rows, design 5.1); `paid` is used only when the invoice is new.
    """
    invoice = open_invoice(ctx, appointment["id"]) if number is None else None
    if invoice is not None:
        return invoice, False
    if number is None:
        number = take_numbers(ctx, "invoice")[0]
    return create_invoice(ctx, appointment, number, paid), True


def collect(ctx, appointment, amount, method, deltas, note="", invoice_number=None, today=None):
    """Take a payment for a locked appointment and record its summary changes in `deltas`.

    `today` is the request's business day when the caller already worked it out, so one request
    never straddles two days. Returns (payment dict, invoice dict).
    """
    if amount <= 0:
        raise ApiError("invalid", _("Enter an amount."), fields={"amount": "range"})
    invoice = None if invoice_number is not None else open_invoice(ctx, appointment["id"])
    if invoice is None:
        if amount > appointment["fee"]:
            raise _over_balance()
        if invoice_number is None:
            invoice_number = take_numbers(ctx, "invoice")[0]
        invoice = create_invoice(ctx, appointment, invoice_number, paid=amount)
    else:
        updated = sql.row(ctx, f'UPDATE "billing_invoice" SET paid = paid + %s WHERE clinic_id = %s AND id = %s '
                               f"AND status = 'open' AND paid + %s <= total RETURNING {INVOICE_COLUMNS}",
                          [amount, ctx.clinic_id, invoice["id"], amount])
        if updated is None:
            raise _over_balance()
        invoice = _invoice(updated)
    today = today or business_day(ctx.clinic)
    payment = _insert_payment(ctx, invoice, appointment["patient_id"], appointment["doctor_id"], "payment", method,
                              amount, today, note)
    deltas.doctor_day(today, appointment["doctor_id"], collected=amount)
    deltas.clinic_day(today, collected=amount)
    deltas.clinic_day(appointment["day"])  # the visit's day shows what is owed
    return payment, invoice


def _over_balance():
    return ApiError("over_balance", _("The amount is more than what is owed."), 409, fields={"amount": "over_balance"})


def _insert_payment(ctx, invoice, patient_id, doctor_id, kind, method, amount, day, note, refund_of=None):
    now = timezone.now()
    payment_id = sql.row(ctx, """
        INSERT INTO "billing_payment" (clinic_id, invoice_id, patient_id, doctor_id, kind, method, amount, refunded,
                                       refund_of_id, received_at, day, received_by_id, note)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 0, %s, %s, %s, %s, %s) RETURNING id""", [
        ctx.clinic_id, invoice["id"], patient_id, doctor_id, kind, method, amount, refund_of, now, day,
        ctx.membership_id, note])[0]
    return {"id": payment_id, "invoice_id": invoice["id"], "patient_id": patient_id, "doctor_id": doctor_id,
            "kind": kind, "method": method, "amount": amount, "refunded": 0, "refund_of_id": refund_of,
            "received_at": now, "day": day, "note": note}


def pay(ctx, appointment_id, amount, method, note=""):
    """POST appointments/{id}/payments. Returns (payment, invoice, version of the visit's day)."""
    appointment = lock_appointment(ctx, appointment_id)
    if appointment["status"] == "cancelled":
        raise _cancelled()
    deltas = Deltas()
    payment, invoice = collect(ctx, appointment, amount, method, deltas, note)
    deltas.write_doctors(ctx)
    versions = deltas.write_clinics(ctx)
    return payment, invoice, versions[appointment["day"]]


def _cancelled():
    return ApiError("cancelled", _("This visit was cancelled."), 409)


def invoice_for_visit(ctx, appointment_id):
    """POST appointments/{id}/invoice: ensure the visit's invoice, so lines and discounts can address it."""
    appointment = lock_appointment(ctx, appointment_id)
    if appointment["status"] == "cancelled":
        raise _cancelled()
    invoice, created = ensure_invoice(ctx, appointment)
    version = None
    if created:
        version = Deltas().clinic_day(appointment["day"]).write_clinics(ctx)[appointment["day"]]
    return invoice, created, version


def cancel_invoice(ctx, appointment_id):
    """For a cancel, under the appointment's lock: refuse while money is paid; void an unpaid open invoice."""
    invoice = open_invoice(ctx, appointment_id)
    if invoice is None:
        return None
    if invoice["paid"] > 0:
        raise ApiError("refund_first", _("Refund the payments before cancelling this visit."), 409)
    _void(ctx, invoice, reason="cancel")
    return invoice


def _void(ctx, invoice, reason):
    sql.run(ctx, "UPDATE \"billing_invoice\" SET status = 'void', voided_at = %s, voided_by_id = %s "
                 "WHERE clinic_id = %s AND id = %s AND status = 'open' AND paid = 0",
            [timezone.now(), ctx.membership_id, ctx.clinic_id, invoice["id"]])
    record_change(ctx, "invoice.void", "invoice", invoice["id"], before={"status": "open", "total": invoice["total"]},
                  after={"status": "void"}, reason=reason)


def _lock_invoice(ctx, invoice_id):
    """Lock the invoice's appointment, then the invoice. Returns (invoice, appointment or None)."""
    found = sql.row(ctx, 'SELECT appointment_id FROM "billing_invoice" WHERE clinic_id = %s AND id = %s',
                    [ctx.clinic_id, invoice_id])
    if found is None:
        raise not_found("invoice")
    appointment = lock_appointment(ctx, found[0]) if found[0] else None
    invoice = _invoice(sql.row(ctx, f'SELECT {INVOICE_COLUMNS} FROM "billing_invoice" WHERE clinic_id = %s AND id = %s '
                                    "FOR NO KEY UPDATE", [ctx.clinic_id, invoice_id]))
    return invoice, appointment


def _require_open(invoice):
    if invoice["status"] != "open":
        raise ApiError("invoice_void", _("This invoice was voided."), 409)


def _visit_day(ctx, invoice, appointment):
    return appointment["day"] if appointment else business_day(ctx.clinic)


def add_line(ctx, invoice_id, kind, service_id, description, qty, unit_price):
    """A list price needs billing.collect (the endpoint's code); any other price also needs billing.discount."""
    invoice, appointment = _lock_invoice(ctx, invoice_id)
    _require_open(invoice)
    from_list = False
    if service_id is not None:
        service = sql.row(ctx, 'SELECT id, name, price, is_active FROM "clinics_service" WHERE clinic_id = %s AND id = %s',
                          [ctx.clinic_id, service_id])
        if service is None:
            raise not_found("service")
        if not service[3]:
            raise ApiError("invalid", _("This service isn't offered any more."), fields={"service_id": "inactive"})
        description = description or service[1]
        if unit_price is None:
            unit_price = service[2]
        from_list = unit_price == service[2]
    if unit_price is None:
        raise ApiError("invalid", _("Enter a price."), fields={"unit_price": "required"})
    if not description:
        raise ApiError("invalid", _("Describe the line."), fields={"description": "required"})
    if not from_list:
        ctx.require("billing.discount")
    amount = qty * unit_price
    line_id = sql.row(ctx, """
        INSERT INTO "billing_invoice_line" (clinic_id, invoice_id, kind, service_id, description, qty, unit_price, amount,
                                            from_list, created_by_id, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""", [
        ctx.clinic_id, invoice["id"], kind, service_id, description[:200], qty, unit_price, amount, from_list,
        ctx.membership_id, timezone.now()])[0]
    sql.run(ctx, 'UPDATE "billing_invoice" SET subtotal = subtotal + %s, total = total + %s WHERE clinic_id = %s AND id = %s',
            [amount, amount, ctx.clinic_id, invoice["id"]])
    version = Deltas().clinic_day(_visit_day(ctx, invoice, appointment)).write_clinics(ctx)
    return line_id, version


def is_visit_line(line, visit_service_id):
    """The visit's own service line: the one that charges the visit's service. Removing it is a 100% discount."""
    return line["kind"] == "service" and visit_service_id is not None and line["service_id"] == visit_service_id


def may_remove_line(ctx, line, visit_service_id, today=None):
    """Who may remove a line (chief judge M1, decision 2; design 5.3).

    billing.discount may remove any line. Without it, a person with billing.collect may remove only a line they
    added themselves on the current business day, and never the visit's own service line, so the desk can fix its
    own mis-tap without lowering what the visit costs.
    """
    if ctx.can("billing.discount"):
        return True
    if not ctx.can("billing.collect") or is_visit_line(line, visit_service_id):
        return False
    today = today or business_day(ctx.clinic)
    return line["created_by_id"] == ctx.membership_id and business_day(ctx.clinic, line["created_at"]) == today


def remove_line(ctx, invoice_id, line_id):
    """Only while paid stays within the new total, and only for who may_remove_line allows. Audited."""
    invoice, appointment = _lock_invoice(ctx, invoice_id)
    _require_open(invoice)
    line = sql.row(ctx, 'SELECT id, kind, service_id, description, qty, unit_price, amount, from_list, created_by_id, '
                        'created_at FROM "billing_invoice_line" WHERE clinic_id = %s AND invoice_id = %s AND id = %s',
                   [ctx.clinic_id, invoice["id"], line_id])
    if line is None:
        raise not_found("line")
    if not may_remove_line(ctx, dict(zip(("id", "kind", "service_id", "description", "qty", "unit_price", "amount",
                                          "from_list", "created_by_id", "created_at"), line)),
                           appointment["service_id"] if appointment else None):
        raise ApiError("forbidden", _("You don't have permission to do this."), 403)
    amount = line[6]
    if invoice["paid"] > invoice["total"] - amount:
        raise _paid_exceeds()
    if invoice["discount"] > invoice["subtotal"] - amount:
        raise ApiError("discount_exceeds", _("Lower the discount before removing this line."), 409)
    sql.run(ctx, 'UPDATE "billing_invoice" SET subtotal = subtotal - %s, total = total - %s WHERE clinic_id = %s AND id = %s',
            [amount, amount, ctx.clinic_id, invoice["id"]])
    sql.run(ctx, 'DELETE FROM "billing_invoice_line" WHERE clinic_id = %s AND id = %s', [ctx.clinic_id, line_id])
    record_change(ctx, "invoice.line_remove", "invoice", invoice["id"],
                  before={"line": line[0], "kind": line[1], "service_id": line[2], "description": line[3],
                          "qty": line[4], "unit_price": line[5], "amount": amount, "from_list": line[7]})
    return Deltas().clinic_day(_visit_day(ctx, invoice, appointment)).write_clinics(ctx)


def _paid_exceeds():
    return ApiError("paid_exceeds", _("More has been paid than the new total. Refund first."), 409)


def set_discount(ctx, invoice_id, discount, expected):
    """An absolute discount with the value it replaces (design 5.3). Audited."""
    invoice, appointment = _lock_invoice(ctx, invoice_id)
    _require_open(invoice)
    if invoice["discount"] == discount:
        return invoice, None  # already so: a harmless repeat
    if invoice["discount"] != expected:
        raise ApiError("stale", _("The discount changed meanwhile. Reload and try again."), 409,
                       fields={"discount": invoice["discount"]})
    if discount > invoice["subtotal"]:
        raise ApiError("discount_too_large", _("The discount can't be more than the invoice."), 409,
                       fields={"discount": "range"})
    if invoice["paid"] > invoice["subtotal"] - discount:
        raise _paid_exceeds()
    updated = _invoice(sql.row(ctx, f'UPDATE "billing_invoice" SET discount = %s, total = subtotal - %s '
                                    f"WHERE clinic_id = %s AND id = %s RETURNING {INVOICE_COLUMNS}",
                               [discount, discount, ctx.clinic_id, invoice["id"]]))
    record_change(ctx, "invoice.discount", "invoice", invoice["id"], before={"discount": invoice["discount"]},
                  after={"discount": discount})
    version = Deltas().clinic_day(_visit_day(ctx, invoice, appointment)).write_clinics(ctx)
    return updated, version


def void(ctx, invoice_id):
    """Only while nothing is paid (design 5.3). Audited. A void invoice no longer blocks a new one for the visit."""
    invoice, appointment = _lock_invoice(ctx, invoice_id)
    if invoice["status"] == "void":
        return None  # a harmless repeat
    if invoice["paid"] > 0:
        raise ApiError("void_paid", _("Refund the payments before voiding this invoice."), 409)
    _void(ctx, invoice, reason="")
    return Deltas().clinic_day(_visit_day(ctx, invoice, appointment)).write_clinics(ctx)


def refund(ctx, payment_id, amount, method=None, note=""):
    """Refund part or all of a payment (design 5.3). Audited.

    Returns (refund row, invoice, version of the visit's day).
    """
    original = sql.row(ctx, 'SELECT id, invoice_id, kind FROM "billing_payment" WHERE clinic_id = %s AND id = %s',
                       [ctx.clinic_id, payment_id])
    if original is None:
        raise not_found("payment")
    if original[2] != "payment":
        raise ApiError("invalid", _("Only payments can be refunded."), fields={"payment": "refund"})
    invoice, appointment = _lock_invoice(ctx, original[1])
    updated = sql.row(ctx, 'UPDATE "billing_payment" SET refunded = refunded + %s WHERE clinic_id = %s AND id = %s '
                           "AND kind = 'payment' AND refunded + %s <= amount "
                           "RETURNING patient_id, doctor_id, method, amount, refunded",
                      [amount, ctx.clinic_id, payment_id, amount])
    if updated is None:
        raise ApiError("over_refund", _("The refund is more than what is left of this payment."), 409,
                       fields={"amount": "over_refund"})
    patient_id, doctor_id, original_method, original_amount, refunded = updated
    today = business_day(ctx.clinic)
    row = _insert_payment(ctx, invoice, patient_id, doctor_id, "refund", method or original_method, amount, today, note,
                          refund_of=payment_id)
    invoice = _invoice(sql.row(ctx, f'UPDATE "billing_invoice" SET paid = paid - %s WHERE clinic_id = %s AND id = %s '
                                    f"RETURNING {INVOICE_COLUMNS}", [amount, ctx.clinic_id, invoice["id"]]))
    record_change(ctx, "payment.refund", "payment", payment_id, before={"refunded": refunded - amount},
                  after={"refunded": refunded}, amount=amount, refund_id=row["id"])
    deltas = Deltas()
    if doctor_id is not None:
        deltas.doctor_day(today, doctor_id, refunded=amount)
    deltas.clinic_day(today, refunded=amount)
    visit_day = _visit_day(ctx, invoice, appointment)
    deltas.clinic_day(visit_day)
    deltas.write_doctors(ctx)
    versions = deltas.write_clinics(ctx)
    return row, invoice, versions[visit_day]
