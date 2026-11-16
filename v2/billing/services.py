"""Invoices and payments (design 5.3). Every function runs inside the request's shard transaction.

Lock order (design 5.0): the appointment, then the invoice, then [void, remove-line: its patient
packages (7a)], then the payment row, then the invoice counter, then the open cash shift (8a: FOR
SHARE, or opened by a cash entry when none is open), then doctor-days, then money-days, then
clinic-days. Recording a procedure takes the package before the invoice (7a); its appointment lock
keeps that from cycling. Invoice endpoints read the invoice without a lock first only to learn its
appointment, which never changes.

Discount types (billing.discounts) add no lock to this order: applying one holds the appointment and the invoice
and reads the type without a lock (the invoice keeps its own copy of the name and the percent); managing them takes
the clinic row (6) and then the type's row, and nothing else.
"""

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from audit.services import record_change
from clinics.services import take_numbers
from core import sql
from core.days import business_day
from core.errors import ApiError
from summaries.services import Deltas

APPOINTMENT_LOCK_SQL = """
    SELECT a.id, a.status, a.day, a.doctor_id, a.patient_id, a.service_id, a.fee, a.is_follow_up, s.name, a.section_id
      FROM "scheduling_appointment" a
      JOIN "clinics_service" s ON s.clinic_id = a.clinic_id AND s.id = a.service_id
     WHERE a.clinic_id = %s AND a.id = %s FOR NO KEY UPDATE OF a"""
INVOICE_COLUMNS = ("id, number, status, patient_id, appointment_id, subtotal, discount, total, paid, discount_type_id, "
                   "discount_name, discount_percent")
INVOICE_FIELDS = tuple(name.strip() for name in INVOICE_COLUMNS.split(","))


def _invoice(values):
    return dict(zip(INVOICE_FIELDS, values))


def percent_of(subtotal, percent):
    """A percent discount in whole piastres, half a piastre rounding up. The invoice table checks the same sum
    (invoice_discount_percent_valid) and move_subtotal computes it in SQL."""
    return (subtotal * percent + 50) // 100


def move_subtotal(ctx, invoice_id, amount):
    """Under the invoice's lock: add `amount` (negative for a removed line) to the subtotal. An invoice that carries
    a percent discount type gets its discount computed again in the same statement; any other discount stays.
    Returns the invoice as it now is."""
    discount = ("CASE WHEN discount_percent IS NULL THEN discount "
                "ELSE ((subtotal + %(amount)s) * discount_percent + 50) / 100 END")
    return _invoice(sql.row(ctx, f"""
        UPDATE "billing_invoice"
           SET subtotal = subtotal + %(amount)s, discount = {discount}, total = subtotal + %(amount)s - {discount}
         WHERE clinic_id = %(clinic)s AND id = %(invoice)s RETURNING {INVOICE_COLUMNS}""",
                            {"amount": amount, "clinic": ctx.clinic_id, "invoice": invoice_id}))


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
    appointment = dict(zip(("id", "status", "day", "doctor_id", "patient_id", "service_id", "fee", "is_follow_up",
                            "service_name", "section_id"), found))
    from scheduling.rules import check_scope

    check_scope(ctx, appointment["section_id"], appointment["doctor_id"])  # design G1: another section's visit is 404
    return appointment


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


def collect(ctx, appointment, amount, method, deltas, note="", invoice_number=None, today=None, shift_id=None):
    """Take a payment for a locked appointment and record its summary changes in `deltas`.

    `today` is the request's business day when the caller already worked it out, so one request
    never straddles two days. `shift_id` is the open cash shift when the caller already took it (booking takes
    it before its doctor-day, design 5.0 8a). Returns (payment dict, invoice dict).
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
    section_id = appointment.get("section_id")
    if section_id is None and "section_id" not in appointment:
        section_id = _section_of(ctx, appointment["id"])
    payment = _insert_payment(ctx, invoice, appointment["patient_id"], appointment["doctor_id"], "payment", method,
                              amount, today, note, section_id=section_id, shift_id=shift_id)
    deltas.doctor_day(today, appointment["doctor_id"], collected=amount)
    deltas.money_day(today, section_id, method, collected=amount)
    deltas.clinic_day(today, collected=amount)
    deltas.clinic_day(appointment["day"])  # the visit's day shows what is owed
    return payment, invoice


def _over_balance():
    return ApiError("over_balance", _("The amount is more than what is owed."), 409, fields={"amount": "over_balance"})


def _section_of(ctx, appointment_id):
    """The visit's section, for a caller whose appointment row didn't carry it."""
    if appointment_id is None:
        return None
    found = sql.row(ctx, 'SELECT section_id FROM "scheduling_appointment" WHERE clinic_id = %s AND id = %s',
                    [ctx.clinic_id, appointment_id])
    return found[0] if found else None


def _insert_payment(ctx, invoice, patient_id, doctor_id, kind, method, amount, day, note, refund_of=None,
                    section_id=None, shift_id=None):
    """Every payment and refund row comes through here, so the register rule (F1) holds in one place:
    cash always joins an open shift, opening one when none is open (finance.services.shift_for); other
    methods join one when it is open.

    For other methods the open shift is locked FOR SHARE inside the INSERT itself (lock order 8a). A close
    holds the shift FOR NO KEY UPDATE: a cash entry waits for it, re-checks `status = 'open'`, and opens a
    new shift, so a closed shift never gains a payment after its count.
    """
    now = timezone.now()
    if method == "cash":
        from finance.services import shift_for

        shift_id = shift_id or shift_for(ctx, "cash")
        payment_id = sql.row(ctx, """
            INSERT INTO "billing_payment" (clinic_id, invoice_id, patient_id, doctor_id, kind, method, amount,
                                           refunded, refund_of_id, received_at, day, received_by_id, note, shift_id,
                                           section_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 0, %s, %s, %s, %s, %s, %s, %s) RETURNING id""", [
            ctx.clinic_id, invoice["id"], patient_id, doctor_id, kind, method, amount, refund_of, now, day,
            ctx.membership_id, note, shift_id, section_id])[0]
        return {"id": payment_id, "invoice_id": invoice["id"], "patient_id": patient_id, "doctor_id": doctor_id,
                "kind": kind, "method": method, "amount": amount, "refunded": 0, "refund_of_id": refund_of,
                "received_at": now, "day": day, "note": note, "shift_id": shift_id, "section_id": section_id}
    payment_id, shift_id = sql.row(ctx, """
        INSERT INTO "billing_payment" (clinic_id, invoice_id, patient_id, doctor_id, kind, method, amount, refunded,
                                       refund_of_id, received_at, day, received_by_id, note, shift_id, section_id)
        SELECT %s, %s, %s, %s, %s, %s, %s, 0, %s, %s, %s, %s, %s, s.id, %s
          FROM (SELECT 1) one LEFT JOIN (SELECT id FROM "finance_shift" WHERE clinic_id = %s AND status = 'open'
                                          FOR SHARE) s ON true
        RETURNING id, shift_id""", [
        ctx.clinic_id, invoice["id"], patient_id, doctor_id, kind, method, amount, refund_of, now, day,
        ctx.membership_id, note, section_id, ctx.clinic_id])
    return {"id": payment_id, "invoice_id": invoice["id"], "patient_id": patient_id, "doctor_id": doctor_id,
            "kind": kind, "method": method, "amount": amount, "refunded": 0, "refund_of_id": refund_of,
            "received_at": now, "day": day, "note": note, "shift_id": shift_id, "section_id": section_id}


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
    void_packages(ctx, invoice["id"])  # F7: a package with no session used goes with its invoice
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
    move_subtotal(ctx, invoice["id"], amount)
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
    """Only while paid stays within the new total, and only for who may_remove_line allows. Audited.

    A percent discount type is computed again on what is left. Any other discount must fit what is left first.
    """
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
    left = invoice["subtotal"] - amount
    percent = invoice["discount_percent"]
    # The discount is named first: with it larger than what is left, "refund first" would send the desk the wrong way.
    if percent is None and invoice["discount"] > left:
        raise ApiError("discount_exceeds", _("Lower the discount before removing this line."), 409)
    discount = invoice["discount"] if percent is None else percent_of(left, percent)
    if invoice["paid"] > left - discount:
        raise _paid_exceeds()
    if line[1] == "package":
        void_packages(ctx, invoice["id"], line_id)  # F7: refused once a session was used
    elif line[1] == "procedure" and appointment:
        _return_package_session(ctx, appointment["id"], line_id)
    move_subtotal(ctx, invoice["id"], -amount)
    sql.run(ctx, 'DELETE FROM "billing_invoice_line" WHERE clinic_id = %s AND id = %s', [ctx.clinic_id, line_id])
    record_change(ctx, "invoice.line_remove", "invoice", invoice["id"],
                  before={"line": line[0], "kind": line[1], "service_id": line[2], "description": line[3],
                          "qty": line[4], "unit_price": line[5], "amount": amount, "from_list": line[7]},
                  after={"discount": discount} if percent is not None else None)
    return Deltas().clinic_day(_visit_day(ctx, invoice, appointment)).write_clinics(ctx)


def _return_package_session(ctx, appointment_id, line_id):
    """Under the invoice's lock (invoice, then package, as void does): a removed procedure line that used a prepaid
    session gives it back, and the record keeps no link to the line or the package. Audited."""
    procedure = sql.row(ctx, 'SELECT id, patient_package_id FROM "clinical_visit_procedure" WHERE clinic_id = %s '
                             "AND appointment_id = %s AND invoice_line_id = %s AND patient_package_id IS NOT NULL",
                        [ctx.clinic_id, appointment_id, line_id])
    if procedure is None:
        return
    returned = sql.run(ctx, 'UPDATE "billing_patient_package" SET used = used - 1 WHERE clinic_id = %s AND id = %s '
                            "AND used > 0", [ctx.clinic_id, procedure[1]])
    sql.run(ctx, 'UPDATE "clinical_visit_procedure" SET patient_package_id = NULL, invoice_line_id = NULL '
                 "WHERE clinic_id = %s AND id = %s", [ctx.clinic_id, procedure[0]])
    record_change(ctx, "package.session_return", "patient_package", procedure[1],
                  visit_procedure=procedure[0], line=line_id, returned=bool(returned))


def _paid_exceeds():
    return ApiError("paid_exceeds", _("More has been paid than the new total. Refund first."), 409)


def set_discount(ctx, invoice_id, discount, expected):
    """An absolute, free-typed discount with the value it replaces (design 5.3). Audited.

    It takes the place of a discount type: the invoice forgets the type, its name and its percent.
    """
    invoice, appointment = _lock_invoice(ctx, invoice_id)
    _require_open(invoice)
    if invoice["discount"] == discount and invoice["discount_type_id"] is None:
        return invoice, None  # already so: a harmless repeat
    if invoice["discount"] != expected:
        raise ApiError("stale", _("The discount changed meanwhile. Reload and try again."), 409,
                       fields={"discount": invoice["discount"]})
    if discount > invoice["subtotal"]:
        raise ApiError("discount_too_large", _("The discount can't be more than the invoice."), 409,
                       fields={"discount": "range"})
    if invoice["paid"] > invoice["subtotal"] - discount:
        raise _paid_exceeds()
    updated = write_discount(ctx, invoice["id"], discount)
    before = {"discount": invoice["discount"]}
    if invoice["discount_type_id"] is not None:
        before["type"] = invoice["discount_type_id"]
    record_change(ctx, "invoice.discount", "invoice", invoice["id"], before=before, after={"discount": discount})
    version = Deltas().clinic_day(_visit_day(ctx, invoice, appointment)).write_clinics(ctx)
    return updated, version


def write_discount(ctx, invoice_id, discount, type_id=None, name="", percent=None):
    """Under the invoice's lock: the discount and the type it came from (none for a free-typed amount)."""
    return _invoice(sql.row(ctx, f'UPDATE "billing_invoice" SET discount = %s, total = subtotal - %s, '
                                 "discount_type_id = %s, discount_name = %s, discount_percent = %s "
                                 f"WHERE clinic_id = %s AND id = %s RETURNING {INVOICE_COLUMNS}",
                            [discount, discount, type_id, name, percent, ctx.clinic_id, invoice_id]))


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
                           "RETURNING patient_id, doctor_id, method, amount, refunded, section_id",
                      [amount, ctx.clinic_id, payment_id, amount])
    if updated is None:
        raise ApiError("over_refund", _("The refund is more than what is left of this payment."), 409,
                       fields={"amount": "over_refund"})
    patient_id, doctor_id, original_method, original_amount, refunded, section_id = updated
    today = business_day(ctx.clinic)
    method = method or original_method
    row = _insert_payment(ctx, invoice, patient_id, doctor_id, "refund", method, amount, today, note,
                          refund_of=payment_id, section_id=section_id)
    invoice = _invoice(sql.row(ctx, f'UPDATE "billing_invoice" SET paid = paid - %s WHERE clinic_id = %s AND id = %s '
                                    f"RETURNING {INVOICE_COLUMNS}", [amount, ctx.clinic_id, invoice["id"]]))
    record_change(ctx, "payment.refund", "payment", payment_id, before={"refunded": refunded - amount},
                  after={"refunded": refunded}, amount=amount, refund_id=row["id"])
    deltas = Deltas()
    if doctor_id is not None:
        deltas.doctor_day(today, doctor_id, refunded=amount)
    deltas.money_day(today, section_id, method, refunded=amount)
    deltas.clinic_day(today, refunded=amount)
    visit_day = _visit_day(ctx, invoice, appointment)
    deltas.clinic_day(visit_day)
    deltas.write_doctors(ctx)
    versions = deltas.write_clinics(ctx)
    return row, invoice, versions[visit_day]


# --- The visit (design E3) ------------------------------------------------------------------------------------


def add_visit_line(ctx, appointment, kind, description, qty, unit_price, from_list, item_id=None):
    """A line on a locked visit's invoice (a recorded procedure). Creates the invoice if it has none.

    Lock order: the appointment (held by the caller) -> the invoice (or the invoice counter for a new one).
    A percent discount type on the invoice is computed again with the new line (move_subtotal).
    Returns (line id, invoice). The caller bumps the visit day's version.
    """
    invoice, _created = ensure_invoice(ctx, appointment)
    _require_open(invoice)
    amount = qty * unit_price
    line_id = sql.row(ctx, """
        INSERT INTO "billing_invoice_line" (clinic_id, invoice_id, kind, service_id, item_id, description, qty, unit_price,
                                            amount, from_list, created_by_id, created_at)
        VALUES (%s, %s, %s, NULL, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""", [
        ctx.clinic_id, invoice["id"], kind, item_id, description[:200], qty, unit_price, amount, from_list,
        ctx.membership_id, timezone.now()])[0]
    return line_id, move_subtotal(ctx, invoice["id"], amount)


def lock_patient_invoices(ctx, patient_id, appointment_id):
    """The visit's own open invoice and the patient's other open invoices with something owed, locked in one
    statement in id order (design 5.0: several rows of one table in key order). Returns them oldest first."""
    rows = sql.rows(ctx, f'SELECT {INVOICE_COLUMNS}, created_at FROM "billing_invoice" WHERE clinic_id = %s '
                         "AND patient_id = %s AND status = 'open' AND (paid < total OR appointment_id = %s) "
                         "ORDER BY id FOR NO KEY UPDATE", [ctx.clinic_id, patient_id, appointment_id])
    found = [dict(_invoice(r[:-1]), created_at=r[-1]) for r in rows]
    return sorted(found, key=lambda i: (i["created_at"], i["id"]))


def collect_earlier(ctx, invoices, appointment_id, amount, method, deltas, today):
    """'Also collect the earlier balance' (design E3): pay the patient's older unpaid invoices, oldest first.

    `invoices` come from lock_patient_invoices. The amount may not exceed what they owe. Each payment row
    carries its invoice's visit doctor, and the summaries change as for any payment (design 5.3).
    """
    older = [i for i in invoices if i["appointment_id"] != appointment_id and i["paid"] < i["total"]]
    owed = sum(i["total"] - i["paid"] for i in older)
    if amount > owed:
        raise ApiError("over_balance", _("The amount is more than what is owed."), 409, fields={"earlier.amount": "over_balance"})
    visits = {r[0]: (r[1], r[2], r[3]) for r in sql.rows(
        ctx, 'SELECT id, doctor_id, day, section_id FROM "scheduling_appointment" WHERE clinic_id = %s '
             "AND id = ANY(%s::bigint[])", [ctx.clinic_id, [i["appointment_id"] for i in older if i["appointment_id"]]])}
    payments, left = [], amount
    for invoice in older:
        if left <= 0:
            break
        part = min(left, invoice["total"] - invoice["paid"])
        sql.run(ctx, 'UPDATE "billing_invoice" SET paid = paid + %s WHERE clinic_id = %s AND id = %s',
                [part, ctx.clinic_id, invoice["id"]])
        doctor_id, day, section_id = visits.get(invoice["appointment_id"], (None, None, None))
        payments.append(_insert_payment(ctx, invoice, invoice["patient_id"], doctor_id, "payment", method, part, today,
                                        _("Earlier balance"), section_id=section_id))
        if doctor_id is not None:
            deltas.doctor_day(today, doctor_id, collected=part)
        deltas.money_day(today, section_id, method, collected=part)
        deltas.clinic_day(today, collected=part)
        if day is not None:
            deltas.clinic_day(day)  # that visit's day shows what is owed
        left -= part
    return payments


# --- Session packages (F7) ------------------------------------------------------------------------------------
# Lock order: selling is key -> appointment -> invoice (or counter invoice); using a session is part of recording a
# procedure, after the plan item and before the invoice (see clinical.services); voiding takes the packages after
# the invoice. Two flows on one visit are serialised by the appointment lock they both take first.

PACKAGE_COLUMNS = "id, section_id, procedure_id, name, sessions, price, is_active"
PATIENT_PACKAGE_COLUMNS = ("id, patient_id, package_id, procedure_id, name, sessions, used, price, invoice_id, "
                           "invoice_line_id, appointment_id, sold_at, voided_at")


def _package(values):
    return dict(zip([c.strip() for c in PACKAGE_COLUMNS.split(",")], values))


def _patient_package(values):
    return dict(zip([c.strip() for c in PATIENT_PACKAGE_COLUMNS.split(",")], values))


def package_not_found():
    return ApiError("not_found", _("This package wasn't found."), 404)


def _duplicate_package():
    return ApiError("duplicate", _("This package is already in the list."), 409, fields={"name": "duplicate"})


def _package_savepoint(ctx, statement, params):
    try:
        with transaction.atomic(using=ctx.alias):
            return sql.row(ctx, statement, params)
    except IntegrityError as error:
        if getattr(getattr(error.__cause__, "diag", None), "constraint_name", None) == "package_active_name_uniq":
            raise _duplicate_package()
        raise


def packages(ctx, include_inactive=False):
    """The place's packages with their procedure's names (a few dozen at most), one statement."""
    where = "" if include_inactive else " AND k.is_active"
    return sql.dicts(ctx, f"""
        SELECT k.id, k.section_id, k.procedure_id, k.name, k.sessions, k.price, k.is_active, p.name_ar AS procedure_ar,
               p.name_en AS procedure_en
          FROM "billing_package" k JOIN "clinical_procedure" p ON p.clinic_id = k.clinic_id AND p.id = k.procedure_id
         WHERE k.clinic_id = %s{where} ORDER BY k.section_id, k.name, k.id""", [ctx.clinic_id])


def create_package(ctx, procedure_id, name, sessions, price):
    """A package of an active procedure; its section is the procedure's. Audited (it sets a price)."""
    found = sql.row(ctx, 'SELECT section_id, is_active FROM "clinical_procedure" WHERE clinic_id = %s AND id = %s',
                    [ctx.clinic_id, procedure_id])
    if found is None:
        raise ApiError("not_found", _("This procedure wasn't found."), 404)
    if not found[1]:
        raise ApiError("invalid", _("This procedure isn't used any more."), fields={"procedure_id": "inactive"})
    package = _package(_package_savepoint(ctx, f"""
        INSERT INTO "billing_package" (clinic_id, section_id, procedure_id, name, sessions, price, is_active, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, true, %s) RETURNING {PACKAGE_COLUMNS}""", [
        ctx.clinic_id, found[0], procedure_id, name, sessions, price, timezone.now()]))
    record_change(ctx, "package.create", "package", package["id"],
                  after={"name": name, "sessions": sessions, "price": price})
    return package


def update_package(ctx, package_id, values):
    """Absolute values for name, sessions, price, is_active. Packages already sold keep what they were sold with."""
    if not values:
        raise ApiError("invalid", _("Send the changes."), fields={"changes": "required"})
    before = sql.row(ctx, f'SELECT {PACKAGE_COLUMNS} FROM "billing_package" WHERE clinic_id = %s AND id = %s '
                          "FOR NO KEY UPDATE", [ctx.clinic_id, package_id])
    if before is None:
        raise package_not_found()
    before = _package(before)
    assignments = ", ".join(f"{name} = %s" for name in values)
    after = _package(_package_savepoint(
        ctx, f'UPDATE "billing_package" SET {assignments} WHERE clinic_id = %s AND id = %s RETURNING {PACKAGE_COLUMNS}',
        [*values.values(), ctx.clinic_id, package_id]))
    record_change(ctx, "package.update", "package", package_id, before={n: before[n] for n in values},
                  after={n: after[n] for n in values})
    return after


def sell_package(ctx, appointment_id, package_id):
    """Sell a package on a visit (F7): a `package` line on the visit's invoice at the package price, and the
    patient's package with all its sessions left. Returns (patient package, invoice, version of the visit's day).
    """
    appointment = lock_appointment(ctx, appointment_id)
    if appointment["status"] in ("cancelled", "no_show"):
        raise ApiError("visit_closed", _("This visit was cancelled."), 409, fields={"status": appointment["status"]})
    found = sql.row(ctx, f'SELECT {PACKAGE_COLUMNS} FROM "billing_package" WHERE clinic_id = %s AND id = %s',
                    [ctx.clinic_id, package_id])
    if found is None:
        raise package_not_found()
    package = _package(found)
    if not package["is_active"]:
        raise ApiError("invalid", _("This package isn't sold any more."), fields={"package_id": "inactive"})
    if package["section_id"] != appointment["section_id"]:
        raise ApiError("invalid", _("This package belongs to another section."), fields={"package_id": "section"})
    line_id, invoice = add_visit_line(ctx, appointment, "package", package["name"], 1, package["price"], True)
    sold = _patient_package(sql.row(ctx, f"""
        INSERT INTO "billing_patient_package" (clinic_id, patient_id, package_id, procedure_id, name, sessions, used, price,
                                               invoice_id, invoice_line_id, appointment_id, sold_at, sold_by_id)
        VALUES (%s, %s, %s, %s, %s, %s, 0, %s, %s, %s, %s, %s, %s) RETURNING {PATIENT_PACKAGE_COLUMNS}""", [
        ctx.clinic_id, appointment["patient_id"], package["id"], package["procedure_id"], package["name"],
        package["sessions"], package["price"], invoice["id"], line_id, appointment_id, timezone.now(),
        ctx.membership_id]))
    version = Deltas().clinic_day(appointment["day"]).write_clinics(ctx)[appointment["day"]]
    return sold, invoice, version


USE_SESSION_SQL = f"""
    UPDATE "billing_patient_package" SET used = used + 1
     WHERE clinic_id = %s AND used < sessions AND voided_at IS NULL AND id = (
         SELECT id FROM "billing_patient_package"
          WHERE clinic_id = %s AND patient_id = %s AND procedure_id = %s AND used < sessions AND voided_at IS NULL
          ORDER BY sold_at, id LIMIT 1 FOR NO KEY UPDATE)
    RETURNING {PATIENT_PACKAGE_COLUMNS}"""


def use_session(ctx, patient_id, procedure_id):
    """Use one prepaid session of this procedure (F7): the oldest package with sessions left, or None.

    One conditional UPDATE. When two recordings race for a package's last session, the loser's re-check finds it
    used up and gets no row; it then tries the next package, at most three times.
    """
    for _attempt in range(3):
        found = sql.row(ctx, USE_SESSION_SQL, [ctx.clinic_id, ctx.clinic_id, patient_id, procedure_id])
        if found is not None:
            return _patient_package(found)
        if sql.row(ctx, 'SELECT 1 FROM "billing_patient_package" WHERE clinic_id = %s AND patient_id = %s '
                        "AND procedure_id = %s AND used < sessions AND voided_at IS NULL LIMIT 1",
                   [ctx.clinic_id, patient_id, procedure_id]) is None:
            return None
    return None


def void_packages(ctx, invoice_id, line_id=None):
    """Under the invoice's lock: void the patient packages sold on this invoice (or on this line), refusing with
    409 package_used when any of their sessions was used. Returns how many were voided."""
    where, params = "", []
    if line_id is not None:
        where, params = " AND invoice_line_id = %s", [line_id]
    rows = sql.rows(ctx, f'SELECT id, used FROM "billing_patient_package" WHERE clinic_id = %s AND invoice_id = %s'
                         f"{where} AND voided_at IS NULL ORDER BY id FOR NO KEY UPDATE",
                    [ctx.clinic_id, invoice_id, *params])
    if not rows:
        return 0
    if any(used > 0 for _id, used in rows):
        raise ApiError("package_used", _("Sessions of this package were already used."), 409)
    sql.run(ctx, 'UPDATE "billing_patient_package" SET voided_at = %s WHERE clinic_id = %s AND id = ANY(%s::bigint[]) '
                 "AND used = 0 AND voided_at IS NULL", [timezone.now(), ctx.clinic_id, [r[0] for r in rows]])
    return len(rows)
