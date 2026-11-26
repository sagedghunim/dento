"""The owner's alerts (Saged, 2026-11-19): the sensitive things the team did, read from the change log.

A procedure taken off after its visit was finished, a removed bill line, a discount, a refund, a register closed short or over,
a price change, a voided expense and a stock count that changed. The change log (audit_change) already holds each
one with who and when, so nothing new is written when they happen: this reads it on its index (clinic, at DESC).

The owner only. Opening the page marks them seen (Membership.alerts_seen_at); the side bar counts the newer ones.
The owner turns kinds of alerts off and on (Membership.alerts_off, Saged 2026-11-24): an off kind is neither listed
nor counted.
"""

import datetime as dt
import json

from django.db import OperationalError, transaction
from django.shortcuts import render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET

from audit.services import record_change
from core import sql
from core.api import Result, api, page
from core.days import local_iso
from core.errors import ApiError

from .format import money_plain
from .http import clinic_page

# The kinds the owner can turn off, by group, in the settings' order (Saged, 2026-11-25: grouped, so it reads
# simply): (key, the change-log actions, the label's msgid, the group). "late" is any change to a file after its
# visit had finished (web.file_history).
KINDS = (
    ("register", ("shift.close",), "The register was short or over", "drawer"),
    ("auto_close", ("shift.auto_close",), "The register closed by itself without a count", "drawer"),
    ("cash_in", ("cash.in",), "Money put into the drawer", "drawer"),
    ("cash", ("cash.out",), "Money taken out of the drawer", "drawer"),  # with the drawer expenses' permission
    ("refund", ("payment.refund",), "Refunded money to a patient", "drawer"),
    ("expense", ("expense.void",), "Voided an expense", "drawer"),
    ("line", ("invoice.line_remove",), "Removed a line from a bill", "bills"),
    ("procedure", ("visit_procedure.remove",), "Removed a procedure after the visit ended", "bills"),
    ("discount", ("invoice.discount",), "Gave a discount", "bills"),
    ("price", ("procedure.price",), "Changed a price", "bills"),
    ("late", (), "A file changed after its visit ended", "files"),
    ("stock", ("item.count",), "A stock count changed the quantity", "stock"),
    ("short", ("item.short",), "A visit used more than the stock showed", "stock"),
)
GROUPS = ("drawer", "bills", "files", "stock")
KEYS = tuple(k[0] for k in KINDS)
ACTIONS = tuple(a for k in KINDS for a in k[1])
DAYS = 60
LIMIT = 150
# A register closed with the count matching is not news, nor a bill line the doctor's removed procedure took with it,
# nor a procedure taken off while its visit was still on: that is a fix (Saged, 2026-11-25). Only one taken off
# after the visit was finished with it (the doctor went back in) is an alert.
_WHERE = ("clinic_id = %s AND (action = ANY(%s){late}) AND at >= %s "
          "AND NOT (action = 'shift.close' AND coalesce((details->'after'->>'difference')::bigint, 0) = 0) "
          "AND NOT (action = 'invoice.line_remove' AND details->>'from_procedure' = 'true') "
          "AND NOT (action = 'visit_procedure.remove' AND coalesce(details->>'after_finish', '') <> 'true')")


def _method_labels():
    from .finance_pages import _method_labels as labels

    return {**labels(), "credit": _("From the patient's credit")}


def _fold_discounts(rows, payments):
    """A refund of money paid over the bill, or one that lowered the bill, is told with the discount that went with
    it in one alert (Saged, 2026-11-26). One give-back refunds several payments (one row each): they show as one,
    the newest, with their amounts added. A give-back takes only a discount made after the bill's last payment
    (one made before was no money paid over); a lowered bill takes the discount its own refund wrote.
    {"into": {refund row id: discount row}, "hidden": {row ids told elsewhere}, "sum": {refund row id: amount}}."""
    into, hidden, total = {}, set(), {}
    window = dt.timedelta(seconds=5)
    for r in rows:  # newest first
        d = r["details"] or {}
        paid = payments.get(r["target_id"])
        if r["action"] != "payment.refund" or r["id"] in hidden or paid is None:
            continue
        if d.get("given_back"):
            total[r["id"]] = d.get("amount") or 0
            for x in rows:  # the other parts of the same give-back
                xp = payments.get(x["target_id"]) if x["action"] == "payment.refund" else None
                if (x["id"] != r["id"] and xp and xp[2] == paid[2] and (x["details"] or {}).get("given_back")
                        and abs(x["at"] - r["at"]) <= window and x["id"] not in hidden):
                    hidden.add(x["id"])
                    total[r["id"]] += (x["details"] or {}).get("amount") or 0
        elif not d.get("lowered"):
            continue
        for x in rows:
            xd = x["details"] or {}
            if x["action"] != "invoice.discount" or x["target_id"] != paid[2] or x["id"] in hidden:
                continue
            if d.get("lowered"):
                if xd.get("reason") == "refund" and abs(x["at"] - r["at"]) <= window:
                    into[r["id"]] = x
                    hidden.add(x["id"])
                    break
            elif xd.get("reason") != "refund" and x["at"] <= r["at"] and (paid[8] is None or x["at"] >= paid[8]):
                into[r["id"]] = x
                hidden.add(x["id"])
                break
    return {"into": into, "hidden": hidden, "sum": total}


def _labels():
    # Written out so the string catalogue finds them (core.i18n reads the gettext calls).
    return {"procedure": _("Removed a procedure after the visit ended"), "line": _("Removed a line from a bill"),
            "discount": _("Gave a discount"), "refund": _("Refunded money to a patient"),
            "register": _("The register was short or over"), "cash_in": _("Money put into the drawer"), "cash": _("Money taken out of the drawer"),
            "auto_close": _("The register closed by itself without a count"),
            "price": _("Changed a price"), "expense": _("Voided an expense"),
            "stock": _("A stock count changed the quantity"), "short": _("A visit used more than the stock showed"),
            "late": _("A file changed after its visit ended")}


def _group_labels():
    return {"drawer": _("The drawer"), "bills": _("Bills and prices"), "files": _("Patients and files"),
            "stock": _("The stock")}


def off_kinds(ctx):
    return set(ctx.membership.alerts_off or ()) & set(KEYS)


def _where(ctx, group=None):
    """(sql, actions) for the kinds still on, of one group when given."""
    off = off_kinds(ctx)
    if group:
        off = off | {k[0] for k in KINDS if k[3] != group}
    actions = [a for key, kind_actions, _l, _g in KINDS if key not in off for a in kind_actions]
    late = "" if "late" in off else " OR details->>'late' = 'true'"
    return _WHERE.format(late=late), actions


def _since():
    return timezone.now() - dt.timedelta(days=DAYS)


def sees(ctx):
    return ctx.membership is not None and ctx.membership.is_owner


def unseen(ctx):
    """How many alerts the owner hasn't opened yet (one count on the index), or 0 for anyone else."""
    if not sees(ctx):
        return 0
    seen = ctx.membership.alerts_seen_at
    since = max(seen, _since()) if seen else _since()
    where, actions = _where(ctx)
    found = sql.row(ctx, f'SELECT count(*) FROM (SELECT 1 FROM "audit_change" WHERE {where} LIMIT 100) a',
                    [ctx.clinic_id, actions, since])
    # Money waiting for the owner's OK counts until it is decided (Saged, 2026-11-25).
    waiting = sql.row(ctx, 'SELECT count(*) FROM (SELECT 1 FROM "finance_cash_request" WHERE clinic_id = %s '
                           "AND status = 'pending' LIMIT 100) r", [ctx.clinic_id])
    return found[0] + waiting[0]


def waiting_requests(ctx):
    """Money the desk asked to take out of the drawer, waiting for the owner, with who asked and what for."""
    from finance.services import cash_requests

    rows = cash_requests(ctx, pending_only=True)
    if not rows:
        return []
    names = dict(sql.rows(ctx, 'SELECT id, display_name FROM "clinics_membership" WHERE clinic_id = %s', [ctx.clinic_id]))
    from core.i18n import pick

    categories = {i: pick(n, e) for i, n, e in sql.rows(
        ctx, 'SELECT id, name, name_en FROM "finance_category" WHERE clinic_id = %s AND id = ANY(%s::bigint[])',
        [ctx.clinic_id, [r["category_id"] for r in rows if r["category_id"]]])}
    def what(r):
        if r["kind"] == "expense":
            return _("Pay an expense from the drawer") + " · " + categories.get(r["category_id"], "")
        if r["kind"] == "line":
            return _("Remove a line from a bill")
        if r["kind"] == "price":
            return _("Change a price")
        return {"discount": _("Give a discount"), "refund": _("Refund money to a patient"), "void": _("Void a bill"),
                "exvoid": _("Void an expense"), "patient": _("Delete a patient"), "stock": _("Change the stock count"),
                }.get(r["kind"], _("Take money out of the drawer"))

    about = _requests_about(ctx, rows)
    return [{"id": r["id"], "amount": r["amount"], "who": names.get(r["requested_by_id"], ""),
             "at": local_iso(ctx.clinic, r["requested_at"]), "note": r["note"], "what": what(r),
             "before_amount": (r["data"] or {}).get("before") if r["kind"] == "price" else None,
             **about.get(r["id"], {})} for r in rows]


def _not_refundable(ctx, status, amount, over, deposit):
    """Why a waiting refund can no longer be done, or None (Saged, 2026-11-26: only before going in)."""
    from billing.services import refundable
    from core.errors import ApiError

    try:
        refundable(ctx, {"status": status} if status else None, amount, over, deposit)
    except ApiError as refused:
        return refused.message
    return None


def _requests_about(ctx, rows):
    """Whose file each waiting request is about, its bill, and for a refund the payment it takes from (Saged,
    2026-11-26: every alert shows its file). A refund that can no longer be done says so before the owner taps
    Approve. Three reads at most, only for the requests that touch a bill."""
    pay_ids = [(r["data"] or {}).get("payment_id") for r in rows if r["kind"] == "refund"]
    payments = {p[0]: p[1:] for p in sql.rows(ctx, """
        SELECT id, invoice_id, amount, refunded, method, is_deposit FROM "billing_payment"
         WHERE clinic_id = %s AND id = ANY(%s::bigint[])""", [ctx.clinic_id, [i for i in pay_ids if i]])} if any(pay_ids) else {}

    def invoice_of(r):
        d = r["data"] or {}
        if r["kind"] == "refund":
            return payments.get(d.get("payment_id"), (None,))[0]
        return d.get("invoice_id") if r["kind"] in ("line", "discount", "void") else None

    invoice_ids = [i for i in (invoice_of(r) for r in rows) if i]
    invoices = {i[0]: i[1:] for i in sql.rows(ctx, """
        SELECT i.id, i.number, i.total, i.patient_id, p.full_name, a.status, i.paid
          FROM "billing_invoice" i
          JOIN "patients_patient" p ON p.clinic_id = i.clinic_id AND p.id = i.patient_id
          LEFT JOIN "scheduling_appointment" a ON a.clinic_id = i.clinic_id AND a.id = i.appointment_id
         WHERE i.clinic_id = %s AND i.id = ANY(%s::bigint[])""", [ctx.clinic_id, invoice_ids])} if invoice_ids else {}
    methods = _method_labels()
    out = {}
    for r in rows:
        item = {"lines": []}
        found = invoices.get(invoice_of(r))
        if found is not None:
            number, total, patient_id, patient, visit_status, bill_paid = found
            item.update(patient_id=patient_id, patient=patient, detail=_("Bill %(number)s") % {"number": number})
            item["lines"].append(_("The bill is now %(total)s") % {"total": money_plain(total)})
        paid = payments.get((r["data"] or {}).get("payment_id")) if r["kind"] == "refund" else None
        if paid is not None:  # the warning stands even when the bill or its file can't be found
            _invoice, amount, refunded, method, was_deposit = paid
            item["lines"].append(_("Paid %(amount)s by %(method)s") % {"amount": money_plain(amount),
                                                                         "method": methods.get(method, method)})
            if r["amount"] > amount - refunded:
                item["problem"] = _("This can't be done: only %(left)s is left of this payment. Refuse it.") % {
                    "left": money_plain(amount - refunded)}
            elif found is not None and (why := _not_refundable(ctx, visit_status, r["amount"], bill_paid - total,
                                                                was_deposit)):
                item["problem"] = _("This can't be done: %(why)s Refuse it.") % {"why": why}
        if found is not None or paid is not None:
            out[r["id"]] = item
    return out


def items(ctx, group=None):
    """The latest alerts, newest first, each with who, when, what and the patient when there is one."""
    where, actions = _where(ctx, group)
    rows = sql.dicts(ctx, f'SELECT id, action, membership_id, target_id, patient_id, details, at FROM "audit_change" '
                          f"WHERE {where} ORDER BY at DESC, id DESC LIMIT %s",
                     [ctx.clinic_id, actions, _since(), LIMIT])
    if not rows:
        return []
    for r in rows:  # jsonb read as text by the raw cursor
        if isinstance(r["details"], str):
            r["details"] = json.loads(r["details"])
    names = dict(sql.rows(ctx, 'SELECT id, display_name FROM "clinics_membership" WHERE clinic_id = %s', [ctx.clinic_id]))
    invoice_ids = [r["target_id"] for r in rows if r["action"].startswith("invoice.")]
    invoices = {r[0]: r[1:] for r in sql.rows(ctx, """
        SELECT i.id, i.number, i.patient_id, p.full_name, i.total, i.paid - greatest(i.total, i.deposit), i.status
          FROM "billing_invoice" i
          JOIN "patients_patient" p ON p.clinic_id = i.clinic_id AND p.id = i.patient_id
         WHERE i.clinic_id = %s AND i.id = ANY(%s::bigint[])""", [ctx.clinic_id, invoice_ids])} if invoice_ids else {}
    refunds = [r["target_id"] for r in rows if r["action"] == "payment.refund"]
    refunds += [r["details"].get("refund_id") for r in rows if r["action"] == "payment.refund"]
    # The refunded payment and the refund itself: its bill, how it went back and why (Saged, 2026-11-26: every detail).
    payments = {r[0]: r[1:] for r in sql.rows(ctx, """
        SELECT y.id, y.patient_id, p.full_name, y.invoice_id, i.number, i.total, y.method, y.note, y.amount,
               (SELECT max(z.received_at) FROM "billing_payment" z WHERE z.clinic_id = y.clinic_id
                   AND z.invoice_id = y.invoice_id AND z.kind = 'payment')
          FROM "billing_payment" y
          JOIN "patients_patient" p ON p.clinic_id = y.clinic_id AND p.id = y.patient_id
          JOIN "billing_invoice" i ON i.clinic_id = y.clinic_id AND i.id = y.invoice_id
         WHERE y.clinic_id = %s AND y.id = ANY(%s::bigint[])""", [ctx.clinic_id, [i for i in refunds if i]])} if refunds else {}
    folded = _fold_discounts(rows, payments)
    procedure_ids = [r["target_id"] for r in rows if r["action"] == "procedure.price"]
    procedure_ids += [r["details"].get("before", {}).get("procedure_id") for r in rows if r["action"] == "visit_procedure.remove"]
    procedures = dict(sql.rows(ctx, 'SELECT id, name_ar FROM "clinical_procedure" WHERE clinic_id = %s AND id = ANY(%s::bigint[])',
                               [ctx.clinic_id, [i for i in procedure_ids if i]])) if procedure_ids else {}
    patient_ids = [r["details"].get("before", {}).get("patient_id") for r in rows if r["action"] == "visit_procedure.remove"]
    patients = dict(sql.rows(ctx, 'SELECT id, full_name FROM "patients_patient" WHERE clinic_id = %s AND id = ANY(%s::bigint[])',
                             [ctx.clinic_id, [i for i in patient_ids if i]])) if patient_ids else {}
    # The visit that used more than the stock showed: whose file it is (Saged, 2026-11-26).
    visit_ids = [r["details"].get("appointment") for r in rows if r["action"] == "item.short"]
    visits = {v[0]: v[1:] for v in sql.rows(ctx, """
        SELECT a.id, a.patient_id, p.full_name FROM "scheduling_appointment" a
          JOIN "patients_patient" p ON p.clinic_id = a.clinic_id AND p.id = a.patient_id
         WHERE a.clinic_id = %s AND a.id = ANY(%s::bigint[])""", [ctx.clinic_id, [i for i in visit_ids if i]])} if any(visit_ids) else {}
    item_ids = [r["target_id"] for r in rows if r["action"] in ("item.count", "item.short")]
    stock = dict(sql.rows(ctx, 'SELECT id, name FROM "inventory_item" WHERE clinic_id = %s AND id = ANY(%s::bigint[])',
                          [ctx.clinic_id, item_ids])) if item_ids else {}
    expense_ids = [r["target_id"] for r in rows if r["action"] == "expense.void"]
    expenses = {}
    if expense_ids:
        from core.i18n import pick

        for i, name, name_en, note in sql.rows(
                ctx, """SELECT e.id, c.name, c.name_en, e.note FROM "finance_expense" e
                  LEFT JOIN "finance_category" c ON c.clinic_id = e.clinic_id AND c.id = e.category_id
                 WHERE e.clinic_id = %s AND e.id = ANY(%s::bigint[])""", [ctx.clinic_id, expense_ids]):
            expenses[i] = " · ".join(x for x in (pick(name, name_en) if name else "", note or "") if x)
    from .file_history import describe

    late = [r for r in rows if r["action"] not in ACTIONS]
    told = {r["id"]: text for r, text in zip(late, describe(ctx, late))}
    late_ids = list({r["patient_id"] for r in rows if r["patient_id"]})  # also any other alert's own file
    late_names = dict(sql.rows(ctx, 'SELECT id, full_name FROM "patients_patient" WHERE clinic_id = %s AND id = ANY(%s::bigint[])',
                               [ctx.clinic_id, late_ids])) if late_ids else {}
    shift_of = _shifts(ctx, rows[-1]["at"], names)
    seen = ctx.membership.alerts_seen_at
    out = []
    methods = _method_labels()
    for r in rows:
        if r["id"] in folded["hidden"]:
            continue  # told inside the refund it led to
        d, action = r["details"] or {}, r["action"]
        before, after = d.get("before") or {}, d.get("after") or {}
        item = {"id": r["id"], "at": local_iso(ctx.clinic, r["at"]), "who": names.get(r["membership_id"], _("The system")),
                "new": not seen or r["at"] > seen, "amount": None, "patient": None, "patient_id": None, "what": "",
                "detail": "", "level": "warn", "shift": shift_of(r["at"])}
        if r["id"] in told:  # a file changed after its visit ended
            text = told[r["id"]]
            item["what"] = f"{text['what']} · {_('After the visit ended')}"
            item["detail"], item["amount"] = text["detail"], text["amount"]
            item["patient_id"] = r["patient_id"]
            item["patient"] = late_names.get(r["patient_id"])
        elif action == "visit_procedure.remove":
            item["what"] = _("Removed a procedure after the visit ended")
            item["detail"] = procedures.get(before.get("procedure_id"), "") + (f" · {before['tooth']}" if before.get("tooth") else "")
            item["amount"] = before.get("amount")
            item["patient_id"] = before.get("patient_id")
            item["patient"] = patients.get(item["patient_id"])
        elif action.startswith("invoice."):
            number, item["patient_id"], item["patient"], total, over, status = invoices.get(r["target_id"], (None,) * 6)
            bill = _("Bill %(number)s") % {"number": number} if number else ""
            if action == "invoice.line_remove":
                item["what"] = _("Removed a line from a bill")
                item["detail"] = " · ".join(x for x in (bill, before.get("description", "")) if x)
                item["amount"] = before.get("amount")
            else:  # invoice.discount (a voided bill is no alert: Saged, 2026-11-26)
                item["what"] = _("Gave a discount")
                item["detail"] = " · ".join(x for x in (bill, after.get("name", "")) if x)
                item["amount"] = after.get("discount")
                if before.get("discount"):
                    item["before_amount"] = before.get("discount")
                item["level"] = "info"
                if total is not None:
                    item["lines"] = [_("The bill is now %(total)s") % {"total": money_plain(total)}]
                    if over and over > 0 and status == "open":  # paid before the discount: waits in the file
                        item["lines"].append(_("Paid over the bill: %(amount)s, not given back yet") % {"amount": money_plain(over)})
                        item["level"] = "warn"
        elif action == "payment.refund":
            paid = payments.get(r["target_id"])
            back = payments.get(d.get("refund_id"))
            item["amount"] = folded["sum"].get(r["id"], d.get("amount"))
            item["level"] = "bad"
            item["what"] = _("Refunded money to a patient")
            lines = []
            if paid:
                item["patient_id"], item["patient"] = paid[0], paid[1]
                lines.append(_("Bill %(number)s") % {"number": paid[3]} + " · " + _("Paid %(amount)s by %(method)s") % {
                    "amount": money_plain(paid[7]), "method": methods.get(paid[5], paid[5])})
            if back:
                lines.append(_("Given back by %(method)s") % {"method": methods.get(back[5], back[5])}
                             + (f" · {back[6]}" if back[6] else ""))
            discount = folded["into"].get(r["id"])
            if discount is not None:
                dd = discount["details"] or {}
                was, now = (dd.get("before") or {}).get("discount") or 0, (dd.get("after") or {}).get("discount") or 0
                item["what"] = (_("Refunded money to a patient, and the bill went down by it") if d.get("lowered")
                                else _("A discount after paying, and the difference given back"))
                text = _("Discount %(before)s → %(after)s") % {"before": money_plain(was), "after": money_plain(now)}
                name = (dd.get("after") or {}).get("name")
                who = names.get(discount["membership_id"], "")
                lines.insert(0, " · ".join(x for x in (text, name, _("by %(name)s") % {"name": who} if who else "") if x))
                if paid:
                    lines.insert(1, _("The bill is now %(total)s") % {"total": money_plain(paid[4])})
            item["lines"] = lines
        elif action == "shift.close":
            difference = after.get("difference") or 0
            item["what"] = _("The register was short") if difference < 0 else _("The register was over")
            item["amount"] = abs(difference)
            item["level"] = "bad" if difference < 0 else "info"
        elif action in ("cash.in", "cash.out"):
            # Cash the desk put in or took out that is no patient's payment nor an expense (Saged, 2026-11-24).
            item["what"] = _("Added money to the drawer") if action == "cash.in" else _("Took money out of the drawer")
            item["detail"] = d.get("reason", "")
            item["amount"] = d.get("amount")
            item["level"] = "info" if action == "cash.in" else "warn"
        elif action == "shift.auto_close":
            item["what"] = _("The register closed by itself without a count")
            item["detail"] = _("Nobody closed it before the day ended. This is what it should hold.")
            item["amount"] = after.get("expected")
        elif action == "procedure.price":
            item["what"] = _("Changed a price")
            item["detail"] = procedures.get(r["target_id"], "")
            if "price" in before:
                item["amount"] = after.get("price")
                item["before_amount"] = before.get("price")
        elif action == "expense.void":
            item["what"] = _("Voided an expense")
            item["amount"] = before.get("amount")
            item["detail"] = expenses.get(r["target_id"], "")
        elif action == "item.count":
            item["what"] = _("A stock count changed the quantity")
            item["detail"] = f"{stock.get(r['target_id'], '')} · {before.get('quantity')} → {after.get('quantity')}"
        elif action == "item.short":
            # The stock showed less than a visit used (Saged, 2026-11-25): its number was wrong; count it.
            item["what"] = _("A visit used more than the stock showed")
            item["detail"] = _("%(item)s · the stock showed %(had)s, the visit used %(used)s. Count it.") % {
                "item": stock.get(r["target_id"], ""), "had": before.get("quantity"), "used": after.get("used")}
            item["patient_id"], item["patient"] = visits.get(d.get("appointment"), (None, None))
        if item["patient_id"] is None and r["patient_id"]:  # every alert about a patient shows their file
            item["patient_id"], item["patient"] = r["patient_id"], late_names.get(r["patient_id"])
        out.append(item)
    return out


def _shifts(ctx, oldest, names):
    """In whose register shift each alert happened (Saged, 2026-11-22): who opened it and when, who closed it and when.
    One read of the shifts that overlap the alerts (index shift_list), then a lookup per alert."""
    rows = sql.rows(ctx, 'SELECT opened_at, opened_by_id, closed_at, closed_by_id FROM "finance_shift" '
                         "WHERE clinic_id = %s AND (closed_at IS NULL OR closed_at >= %s) ORDER BY opened_at DESC, id DESC "
                         "LIMIT 500", [ctx.clinic_id, oldest])

    def find(at):
        for opened_at, opened_by, closed_at, closed_by in rows:
            if opened_at <= at and (closed_at is None or at <= closed_at):
                return {"opened_by": names.get(opened_by, _("The system")), "opened_at": local_iso(ctx.clinic, opened_at),
                        "closed_by": names.get(closed_by, _("The system")) if closed_at else None,
                        "closed_at": local_iso(ctx.clinic, closed_at) if closed_at else None}
        return None
    return find


@require_GET
@page()
@clinic_page
def alerts_page(request, ctx):
    if not sees(ctx):
        return render(request, "web/forbidden.html", {"ctx": ctx}, status=403)
    group = request.GET.get("group")
    group = group if group in GROUPS else None
    found = items(ctx, group)
    from clinics.models import Membership

    now = timezone.now()
    try:  # marking them seen never stops the page (a long write holding the row: they stay new until next time)
        with transaction.atomic(using=ctx.alias):
            Membership.objects.filter(clinic_id=ctx.clinic_id, id=ctx.membership_id).update(alerts_seen_at=now)
        ctx.membership.alerts_seen_at = now  # the side bar on this page counts none
    except OperationalError:
        pass
    off = off_kinds(ctx)
    labels, names = _labels(), _group_labels()
    from clinics import settings as clinic_settings

    approvals = clinic_settings.effective(ctx.clinic.settings)["approvals"]
    # Next to an alert, its permission where the action can wait for the owner (Saged, 2026-11-25).
    groups = [{"key": g, "label": names[g],
               "kinds": [{"key": k[0], "label": labels[k[0]], "on": k[0] not in off,
                          "gate": k[0] in clinic_settings.APPROVAL_KINDS, "gated": k[0] in approvals}
                         for k in KINDS if k[3] == g]}
              for g in GROUPS]
    return render(request, "web/alerts.html", {"ctx": ctx, "nav_active": "alerts", "items": found, "days": DAYS,
                                               "groups": groups, "group": group, "some_off": bool(off),
                                               "waiting": waiting_requests(ctx), "approvals": approvals})


@api("PUT", key=False)
def alert_settings_view(request, ctx, data):
    """PUT {off: [kind, ...]}: the kinds of alerts the owner doesn't want (absolute: a resend changes nothing)."""
    if not sees(ctx):
        raise ApiError("forbidden", _("Only the account's owner can change this."), 403)
    off = data.get("off")
    if not isinstance(off, list) or not all(isinstance(k, str) and k in KEYS for k in off):
        raise ApiError("invalid", "off must list alert kinds", fields={"off": "invalid"})
    off = sorted(set(off))
    from clinics.models import Membership

    before = sorted(off_kinds(ctx))
    Membership.objects.filter(clinic_id=ctx.clinic_id, id=ctx.membership_id).update(alerts_off=off or None)
    if before != off:
        record_change(ctx, "alerts.settings", "membership", ctx.membership_id, before={"off": before},
                      after={"off": off})
    return Result({"off": off})
