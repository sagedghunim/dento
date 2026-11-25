"""Suppliers, purchase orders and what we owe them (design H1). Inside the request's shard transaction.

Lock order (design H1): request key -> [shift, only when paid from the drawer] -> items (one statement, sorted by id)
-> order (conditional status update) -> lines -> vendor entries -> expenses -> clinic-days.

Low stock prepares an order on its own (refill): when an item crosses its minimum downward it goes on its supplier's
draft. The desk reviews the draft, sends it (a WhatsApp link) and receives what came. Money is shown and taken only
with money.view; without it the desk receives quantities alone, at the item's current cost.
"""

import datetime as dt
import re

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from audit.services import record_change
from billing import visibility
from core import sql, whatsapp
from core.days import business_day
from core.errors import ApiError
from summaries.services import Deltas

from . import services as stock

SUPPLIER_COLUMNS = "id, name, phone, notes, kind, is_active, created_at, visit_days, visit_time"
SUPPLIER_TEXT = {"name": 120, "phone": 20, "notes": 300}
PAID = ("drawer", "outside", "account")
OPEN = ("draft", "sent")


def _not_found(what):
    messages = {"supplier": _("This supplier wasn't found."), "order": _("This order wasn't found."),
                "entry": _("This entry wasn't found.")}
    return ApiError("not_found", messages[what], 404)


def phone_digits(value):
    """Digits only; an Egyptian mobile (01x, 11 digits) gets the country code (201x) so wa.me opens it."""
    digits = "".join(c for c in (value or "") if c.isdigit())
    if len(digits) == 11 and digits.startswith("01"):
        digits = "2" + digits
    return digits[:20]


# --- Suppliers --------------------------------------------------------------------------------------------------


def _supplier_row(values):
    """visit_days as a list of weekdays (Monday = 0) and visit_time as "HH:MM" or ""."""
    row = dict(zip(stock._cols(SUPPLIER_COLUMNS), values))
    row["visit_days"] = days_list(row["visit_days"])
    row["visit_time"] = row["visit_time"].strftime("%H:%M") if row["visit_time"] else ""
    return row


def days_mask(days):
    return sum(1 << d for d in set(days))


def days_list(mask):
    return [d for d in range(7) if mask & (1 << d)]


def _visit_params(values):
    """The visit columns as the database takes them, from the API's list and "HH:MM"."""
    out = {}
    if "visit_days" in values:
        out["visit_days"] = days_mask(values["visit_days"])
    if "visit_time" in values:
        out["visit_time"] = values["visit_time"] or None
    return out


def owed(ctx, supplier_ids):
    """{supplier_id: bills - payments}, from the open-entries index; below zero is credit."""
    if not supplier_ids:
        return {}
    return {r[0]: r[1] for r in sql.rows(ctx, """
        SELECT supplier_id, COALESCE(sum(CASE WHEN kind = 'bill' THEN amount ELSE -amount END), 0)::bigint
          FROM "inventory_vendor_entry"
         WHERE clinic_id = %s AND supplier_id = ANY(%s::bigint[]) AND voided_at IS NULL GROUP BY supplier_id""",
                                         [ctx.clinic_id, list(supplier_ids)])}


def suppliers(ctx, kind="supplier", include_inactive=False):
    """The place's suppliers by name, with what we owe each (money.view only)."""
    where = "" if include_inactive else " AND is_active"
    rows = [_supplier_row(r) for r in sql.rows(ctx, f"""
        SELECT {SUPPLIER_COLUMNS} FROM "inventory_supplier" WHERE clinic_id = %s AND kind = %s{where} ORDER BY name, id""",
                                              [ctx.clinic_id, kind])]
    balances = owed(ctx, [r["id"] for r in rows]) if visibility.sees(ctx) else {}
    for row in rows:
        row["owed"] = balances.get(row["id"], 0) if visibility.sees(ctx) else None
    return rows


def get_supplier(ctx, supplier_id):
    found = sql.row(ctx, f'SELECT {SUPPLIER_COLUMNS} FROM "inventory_supplier" WHERE clinic_id = %s AND id = %s',
                    [ctx.clinic_id, supplier_id])
    if found is None:
        raise _not_found("supplier")
    row = _supplier_row(found)
    row["owed"] = owed(ctx, [supplier_id]).get(supplier_id, 0) if visibility.sees(ctx) else None
    return row


def _supplier_savepoint(ctx, statement, params):
    try:
        with transaction.atomic(using=ctx.alias):
            return sql.row(ctx, statement, params)
    except IntegrityError as error:
        name = getattr(getattr(error.__cause__, "diag", None), "constraint_name", None)
        if name == "supplier_active_name_uniq":
            raise ApiError("duplicate", _("This supplier is already in the list."), 409, fields={"name": "duplicate"})
        raise


def create_supplier(ctx, values, kind="supplier", item_ids=None):
    visit = _visit_params(values)
    found = _supplier_savepoint(ctx, f"""
        INSERT INTO "inventory_supplier" (clinic_id, name, phone, notes, kind, is_active, created_at, visit_days,
                                          visit_time)
        VALUES (%s, %s, %s, %s, %s, true, %s, %s, %s) RETURNING {SUPPLIER_COLUMNS}""", [
        ctx.clinic_id, values["name"], phone_digits(values.get("phone")), values.get("notes", ""), kind, timezone.now(),
        visit.get("visit_days", 0), visit.get("visit_time")])
    supplier = _supplier_row(found)
    record_change(ctx, "supplier.add", "supplier", supplier["id"], after={"name": supplier["name"], "kind": kind})
    if item_ids is not None:
        link_items(ctx, supplier["id"], item_ids)
    return get_supplier(ctx, supplier["id"])


def update_supplier(ctx, supplier_id, values, item_ids=None):
    if not values and item_ids is None:
        raise ApiError("invalid", _("Send the changes."), fields={"changes": "required"})
    if "phone" in values:
        values["phone"] = phone_digits(values["phone"])
    before = get_supplier(ctx, supplier_id)
    if values:
        columns = {**{k: v for k, v in values.items() if not k.startswith("visit_")}, **_visit_params(values)}
        assignments = ", ".join(f"{name} = %s" for name in columns)
        _supplier_savepoint(ctx, f'UPDATE "inventory_supplier" SET {assignments} WHERE clinic_id = %s AND id = %s '
                                 "RETURNING id", [*columns.values(), ctx.clinic_id, supplier_id])
        record_change(ctx, "supplier.edit", "supplier", supplier_id, before={k: before[k] for k in values}, after=values)
    if item_ids is not None:
        link_items(ctx, supplier_id, item_ids)
    return get_supplier(ctx, supplier_id)


def supplier_items(ctx, supplier_id):
    """The ids of the active items ordered from this supplier, by name."""
    return [r[0] for r in sql.rows(ctx, 'SELECT id FROM "inventory_item" WHERE clinic_id = %s AND supplier_id = %s '
                                        "AND is_active ORDER BY name, id", [ctx.clinic_id, supplier_id])]


def link_items(ctx, supplier_id, item_ids):
    """The supplier's items become exactly `item_ids` (Saged, 2026-11-25: tick boxes in the supplier's dialog). A
    ticked item moves here from any other supplier, and stops having this one as its backup; an unticked item that
    was here has no supplier until one is chosen. Only active items of this place; the supplier must be active."""
    wanted = sorted(set(item_ids))
    supplier = sql.row(ctx, 'SELECT is_active, kind FROM "inventory_supplier" WHERE clinic_id = %s AND id = %s '
                       "FOR NO KEY UPDATE", [ctx.clinic_id, supplier_id])
    if supplier is None or supplier[1] != "supplier":
        raise _not_found("supplier")
    if wanted and not supplier[0]:
        raise ApiError("invalid", _("Items can't be linked to a supplier we no longer order from."),
                       fields={"item_ids": "inactive"})
    # Every row both updates touch, locked in id order as stock use locks them (no deadlock); NO KEY so ledger and
    # order lines that point at an item don't wait. The supplier's NO KEY lock still keeps two of these in turn.
    locked = sql.rows(ctx, """
        SELECT id, is_active FROM "inventory_item"
         WHERE clinic_id = %s AND (id = ANY(%s::bigint[]) OR supplier_id = %s) ORDER BY id FOR NO KEY UPDATE""",
                      [ctx.clinic_id, wanted, supplier_id])
    found = {r[0] for r in locked if r[1]}
    if set(wanted) - found:
        raise stock.not_found("item")
    before = supplier_items(ctx, supplier_id)
    if set(before) == set(wanted):
        return
    sql.rows(ctx, """
        UPDATE "inventory_item" SET supplier_id = NULL
         WHERE clinic_id = %s AND supplier_id = %s AND is_active AND NOT (id = ANY(%s::bigint[])) RETURNING id""",
             [ctx.clinic_id, supplier_id, wanted])
    moved = [r[0] for r in sql.rows(ctx, """
        UPDATE "inventory_item"
           SET supplier_id = %s,
               backup_supplier_id = CASE WHEN backup_supplier_id = %s THEN NULL ELSE backup_supplier_id END
         WHERE clinic_id = %s AND id = ANY(%s::bigint[]) AND supplier_id IS DISTINCT FROM %s RETURNING id""",
             [supplier_id, supplier_id, ctx.clinic_id, wanted, supplier_id])]
    if moved:
        # A moved item leaves another supplier's draft (not yet sent), so that draft stops asking them for it; a sent
        # order stays as it is, the supplier already has it.
        sql.run(ctx, """
            DELETE FROM "inventory_order_line" l USING "inventory_order" o
             WHERE l.clinic_id = %s AND l.item_id = ANY(%s::bigint[]) AND o.clinic_id = l.clinic_id
               AND o.id = l.order_id AND o.status = 'draft' AND o.supplier_id <> %s""",
                [ctx.clinic_id, moved, supplier_id])
    refill(ctx, moved)  # a low item newly linked goes on this supplier's draft, as when its own dialog sets it
    record_change(ctx, "supplier.items", "supplier", supplier_id, before={"item_ids": before},
                  after={"item_ids": wanted})


def check_item_suppliers(ctx, supplier_id, backup_id):
    """An item's supplier and backup: this place's, active, kind supplier, and not the same one."""
    wanted = [i for i in (supplier_id, backup_id) if i is not None]
    if supplier_id is not None and supplier_id == backup_id:
        raise ApiError("invalid", _("The backup supplier can't be the same as the supplier."),
                       fields={"backup_supplier_id": "same"})
    if not wanted:
        return
    found = {r[0] for r in sql.rows(ctx, 'SELECT id FROM "inventory_supplier" WHERE clinic_id = %s AND is_active '
                                         "AND kind = 'supplier' AND id = ANY(%s::bigint[])", [ctx.clinic_id, wanted])}
    if set(wanted) - found:
        raise _not_found("supplier")


# --- Refill (flow 1) --------------------------------------------------------------------------------------------


def refill(ctx, item_ids=None):
    """Put low items on their supplier's draft. `item_ids` None: every low item (the "order all that's short" button).

    Only active items at or below their minimum, with an active supplier, and on no draft or sent order. One draft
    per supplier: the INSERT ... ON CONFLICT finds and locks it. qty = max_qty - quantity (or 2 x minimum - quantity
    when max_qty is 0), at least 1. Returns the ids of the drafts it touched.
    """
    if item_ids is not None and not item_ids:
        return []
    where, params = "", [ctx.clinic_id]
    if item_ids is not None:
        where, params = " AND i.id = ANY(%s::bigint[])", [ctx.clinic_id, sorted(item_ids)]
    wanted = sql.rows(ctx, f"""
        SELECT i.supplier_id, i.id, GREATEST(CASE WHEN i.max_qty > 0 THEN i.max_qty ELSE 2 * i.threshold END
                                             - i.quantity, 1)
          FROM "inventory_item" i
          JOIN "inventory_supplier" s ON s.clinic_id = i.clinic_id AND s.id = i.supplier_id AND s.is_active
                                     AND s.kind = 'supplier'
         WHERE i.clinic_id = %s{where} AND i.is_active AND i.quantity <= i.threshold
           AND NOT EXISTS (SELECT 1 FROM "inventory_order_line" l
                             JOIN "inventory_order" o ON o.clinic_id = l.clinic_id AND o.id = l.order_id
                            WHERE l.clinic_id = i.clinic_id AND l.item_id = i.id AND o.status IN ('draft', 'sent'))
         ORDER BY i.supplier_id, i.id""", params)
    by_supplier = {}
    for supplier_id, item_id, qty in wanted:
        by_supplier.setdefault(supplier_id, []).append((item_id, qty))
    touched = []
    now = timezone.now()
    for supplier_id, lines in by_supplier.items():
        order_id = sql.row(ctx, """
            INSERT INTO "inventory_order" (clinic_id, supplier_id, status, note, created_at, created_by_id)
            VALUES (%s, %s, 'draft', '', %s, NULL)
            ON CONFLICT (clinic_id, supplier_id) WHERE status = 'draft'
            DO UPDATE SET note = "inventory_order".note RETURNING id""", [ctx.clinic_id, supplier_id, now])[0]
        _add_lines(ctx, order_id, dict(lines), replace=False)
        touched.append(order_id)
    if touched and ctx.clinic.settings.get("orders_auto_send") and whatsapp.configured():
        from .jobs import queue_auto_send

        for order_id in touched:
            transaction.on_commit(lambda o=order_id: queue_auto_send(ctx.clinic_id, ctx.alias, o), using=ctx.alias)
    return touched


def _add_lines(ctx, order_id, lines, replace):
    """Insert lines; an item already on the order keeps its qty (refill) or takes the new one (`replace`)."""
    if not lines:
        return
    values = ", ".join(["(%s, %s, %s, %s, 0, 0)"] * len(lines))
    conflict = "DO UPDATE SET qty = EXCLUDED.qty" if replace else "DO NOTHING"
    sql.run(ctx, f"""
        INSERT INTO "inventory_order_line" (clinic_id, order_id, item_id, qty, received, unit_price) VALUES {values}
        ON CONFLICT (clinic_id, order_id, item_id) {conflict}""",
            [p for item_id in sorted(lines) for p in (ctx.clinic_id, order_id, item_id, lines[item_id])])


# --- Orders -----------------------------------------------------------------------------------------------------

ORDER_COLUMNS = "o.id, o.supplier_id, s.name, s.phone, o.status, o.note, o.created_at, o.created_by_id, o.sent_at, o.closed_at"


def _order_row(values):
    return dict(zip(("id", "supplier_id", "supplier_name", "supplier_phone", "status", "note", "created_at",
                     "created_by_id", "sent_at", "closed_at"), values))


def draft_count(ctx):
    """The side bar's count (from the status index)."""
    return sql.row(ctx, 'SELECT count(*) FROM "inventory_order" WHERE clinic_id = %s AND status = %s',
                   [ctx.clinic_id, "draft"])[0]


def orders(ctx, state="open", limit=100, after=None):
    """Open orders (draft, then sent) or closed ones (done, cancelled), newest first, with their line count.

    Closed ones come a page at a time: `after` is the (created_at, id) of the last one shown."""
    statuses = list(OPEN) if state == "open" else ["done", "cancelled"]
    params = [ctx.clinic_id, statuses]
    where = ""
    if after is not None and state != "open":
        where = "AND (o.created_at, o.id) < (%s, %s)"
        params += list(after)
    order = "o.status = 'sent', " if state == "open" else ""
    rows = sql.rows(ctx, f"""
        SELECT {ORDER_COLUMNS}, (SELECT count(*) FROM "inventory_order_line" l
                                  WHERE l.clinic_id = o.clinic_id AND l.order_id = o.id)
          FROM "inventory_order" o JOIN "inventory_supplier" s ON s.clinic_id = o.clinic_id AND s.id = o.supplier_id
         WHERE o.clinic_id = %s AND o.status = ANY(%s) {where}
         ORDER BY {order}o.created_at DESC, o.id DESC LIMIT %s""", [*params, limit])
    found = []
    for r in rows:
        row = _order_row(r[:-1])
        row["lines"] = r[-1]
        found.append(row)
    return found


def get_order(ctx, order_id):
    from core.i18n import pick

    found = sql.row(ctx, f"""
        SELECT {ORDER_COLUMNS} FROM "inventory_order" o
          JOIN "inventory_supplier" s ON s.clinic_id = o.clinic_id AND s.id = o.supplier_id
         WHERE o.clinic_id = %s AND o.id = %s""", [ctx.clinic_id, order_id])
    if found is None:
        raise _not_found("order")
    order = _order_row(found)
    order["lines"] = visibility.rows(ctx, [
        {"item_id": r[0], "name": pick(r[1], r[2]), "unit": r[3], "qty": r[4], "received": r[5], "unit_price": r[6],
         "quantity": r[7], "threshold": r[8], "unit_cost": r[9]} for r in sql.rows(ctx, """
        SELECT l.item_id, i.name, i.name_en, i.unit, l.qty, l.received, l.unit_price, i.quantity, i.threshold, i.unit_cost
          FROM "inventory_order_line" l JOIN "inventory_item" i ON i.clinic_id = l.clinic_id AND i.id = l.item_id
         WHERE l.clinic_id = %s AND l.order_id = %s ORDER BY i.name, l.item_id""", [ctx.clinic_id, order_id])],
        "unit_price", "unit_cost")
    return order


def _check_items(ctx, item_ids):
    found = {r[0] for r in sql.rows(ctx, 'SELECT id FROM "inventory_item" WHERE clinic_id = %s AND is_active '
                                         "AND id = ANY(%s::bigint[])", [ctx.clinic_id, list(item_ids)])}
    if set(item_ids) - found:
        raise stock.not_found("item")


def create_order(ctx, supplier_id, lines, note=""):
    """An order started by hand. The supplier's draft, when one is open, takes these lines instead (one per supplier)."""
    check_item_suppliers(ctx, supplier_id, None)
    _check_items(ctx, lines)
    order_id = sql.row(ctx, """
        INSERT INTO "inventory_order" (clinic_id, supplier_id, status, note, created_at, created_by_id)
        VALUES (%s, %s, 'draft', %s, %s, %s)
        ON CONFLICT (clinic_id, supplier_id) WHERE status = 'draft'
        DO UPDATE SET note = CASE WHEN EXCLUDED.note <> '' THEN EXCLUDED.note ELSE "inventory_order".note END
        RETURNING id""", [ctx.clinic_id, supplier_id, note, timezone.now(), ctx.membership_id])[0]
    _add_lines(ctx, order_id, lines, replace=True)
    return get_order(ctx, order_id)


def _lock_order(ctx, order_id, statuses):
    found = sql.row(ctx, 'SELECT status FROM "inventory_order" WHERE clinic_id = %s AND id = %s FOR NO KEY UPDATE',
                    [ctx.clinic_id, order_id])
    if found is None:
        raise _not_found("order")
    if found[0] not in statuses:
        raise _wrong_status(found[0])
    return found[0]


def _wrong_status(status):
    messages = {"draft": _("This order hasn't been sent yet."), "sent": _("This order was already sent."),
                "done": _("This order was already received."), "cancelled": _("This order was cancelled.")}
    return ApiError("order_status", messages[status], 409, extra={"status": status})


def edit_draft(ctx, order_id, lines, note=None):
    """A draft's lines as the desk left them (absolute): quantities changed, items removed or added, the note.

    A removed item is not put back by refill (it would come straight back): it waits for the button or its next
    crossing.
    """
    _lock_order(ctx, order_id, ("draft",))
    if lines:
        _check_items(ctx, lines)
    sql.run(ctx, 'DELETE FROM "inventory_order_line" WHERE clinic_id = %s AND order_id = %s '
                 "AND NOT (item_id = ANY(%s::bigint[]))", [ctx.clinic_id, order_id, list(lines)])
    _add_lines(ctx, order_id, lines, replace=True)
    if note is not None:
        sql.run(ctx, 'UPDATE "inventory_order" SET note = %s WHERE clinic_id = %s AND id = %s',
                [note, ctx.clinic_id, order_id])
    return get_order(ctx, order_id)


# The words the owner writes in the order message (Saged, 2026-11-25), each in English and Arabic.
MESSAGE_WORDS = {"supplier": ("{supplier}", "{المورد}"), "clinic": ("{clinic}", "{العيادة}"),
                 "items": ("{items}", "{الأصناف}")}


def default_message(arabic=True):
    """The built-in order text, written with the words, as the owner's box shows it."""
    if arabic:
        return "السلام عليكم، طلب من {العيادة}:\n{الأصناف}\nشكراً"
    return "Hello, an order from {clinic}:\n{items}\nThank you"


def message(ctx, order):
    """The WhatsApp text from the clinic's template (or the built-in one): the clinic, the supplier, the items with
    quantities and units, and the order's note under the items. No prices, no patient data. A template without the
    items word still gets the items, at the end: an order never goes out without them."""
    settings = ctx.clinic.settings or {}
    template = settings.get("order_message") or default_message(settings.get("print_language", "ar") == "ar")
    lines = [f"- {line['name']}: {line['qty']} {line['unit']}" for line in order["lines"]]
    if order["note"]:
        lines.append(order["note"])
    if not any(word in template for word in MESSAGE_WORDS["items"]):
        template = template.rstrip() + "\n{items}"
    values = {"supplier": order["supplier_name"], "clinic": ctx.clinic.name, "items": "\n".join(lines)}
    # One pass over the template, so a name that holds a word is never filled again.
    pattern = re.compile("|".join(re.escape(w) for words in MESSAGE_WORDS.values() for w in words))
    key_of = {w: key for key, words in MESSAGE_WORDS.items() for w in words}
    return pattern.sub(lambda m: values[key_of[m.group(0)]], template)


def send(ctx, order_id):
    """Draft -> sent (conditional). Answers the message and the wa.me link; a repeat on a sent order answers the same."""
    changed = sql.row(ctx, """
        UPDATE "inventory_order" o SET status = 'sent', sent_at = %s, sent_by_id = %s
         WHERE clinic_id = %s AND id = %s AND status = 'draft'
           AND EXISTS (SELECT 1 FROM "inventory_order_line" l WHERE l.clinic_id = o.clinic_id AND l.order_id = o.id)
        RETURNING id""", [timezone.now(), ctx.membership_id, ctx.clinic_id, order_id])
    order = get_order(ctx, order_id)
    if changed is None:
        if order["status"] == "draft":
            raise ApiError("invalid", _("Add an item to the order first."), fields={"lines": "required"})
        if order["status"] in ("done", "cancelled"):
            raise _wrong_status(order["status"])
    else:
        record_change(ctx, "order.send", "order", order_id, after={"supplier_id": order["supplier_id"],
                                                                     "lines": len(order["lines"])})
    text = message(ctx, order)
    return order, text, whatsapp.link(order["supplier_phone"], text)


def cancel(ctx, order_id):
    """Draft or sent -> cancelled. No stock or money changes. A sent order's items go back to refill; a draft's
    don't (they would come straight back on a new draft)."""
    status = _lock_order(ctx, order_id, ("draft", "sent", "cancelled"))
    if status == "cancelled":
        return get_order(ctx, order_id)  # a harmless repeat
    sql.run(ctx, "UPDATE \"inventory_order\" SET status = 'cancelled', closed_at = %s "
                 "WHERE clinic_id = %s AND id = %s AND status IN ('draft', 'sent')",
            [timezone.now(), ctx.clinic_id, order_id])
    record_change(ctx, "order.cancel", "order", order_id, before={"status": status})
    if status == "sent":
        refill(ctx, [r[0] for r in sql.rows(ctx, 'SELECT item_id FROM "inventory_order_line" WHERE clinic_id = %s '
                                                  "AND order_id = %s", [ctx.clinic_id, order_id])])
    return get_order(ctx, order_id)


def receive(ctx, order_id, lines, paid=None, close=False):
    """What came from the supplier (flow 5). `lines` is {item_id: (qty, unit_price or None)}; qty 0 skips a line.

    With money.view every line has a price and `paid` says how (when the total isn't 0): the drawer or outside it
    (one expense, purchase = true) or on the supplier's account (one bill). Without it: quantities only, at the
    item's current cost, no expense and no bill. More than ordered is allowed. The order is done when every line is
    fully in or `close` is sent; lines left short go back to refill.
    """
    from finance import services as finance

    money = visibility.sees(ctx)
    if paid == "drawer":
        finance.shift_for(ctx, "cash")  # the shift before the items (lock order)
    item_ids = [i for i, (q, _p) in lines.items() if q > 0]
    locked = stock._lock_items(ctx, item_ids) if item_ids else {}
    _lock_order(ctx, order_id, ("sent",))
    ordered = {r[0]: (r[1], r[2]) for r in sql.rows(ctx, 'SELECT item_id, qty, received FROM "inventory_order_line" '
                                                         "WHERE clinic_id = %s AND order_id = %s FOR NO KEY UPDATE",
                                                    [ctx.clinic_id, order_id])}
    stray = sorted(set(lines) - set(ordered))
    if stray:
        raise ApiError("invalid", _("This item isn't on the order."), fields={"lines": "not_on_order"},
                       extra={"item_ids": stray})
    today = business_day(ctx.clinic)
    total = sum(q * p for q, p in lines.values() if q > 0 and p) if money else 0
    if money and total > 0 and paid not in PAID:
        raise ApiError("invalid", _("Choose how it was paid."), fields={"paid": "required"})
    deltas = Deltas()
    supplier = sql.row(ctx, 'SELECT o.supplier_id, s.name, s.kind FROM "inventory_order" o JOIN "inventory_supplier" s '
                            "ON s.clinic_id = o.clinic_id AND s.id = o.supplier_id WHERE o.clinic_id = %s AND o.id = %s",
                       [ctx.clinic_id, order_id])
    expense = None
    note = f"#{order_id} {supplier[1]}"[:200]
    if total > 0 and paid in ("drawer", "outside"):
        expense = finance.add_expense(ctx, finance.category_by_code(ctx, "materials"), total, today, note,
                                      paid == "drawer", purchase=True, deltas=deltas)
    movements, now = [], timezone.now()
    for item_id in sorted(item_ids):
        qty, price = lines[item_id]
        item = locked[item_id]
        if not money or price is None:
            price = item["unit_cost"]
        new_cost = stock.average_cost(item["quantity"], item["unit_cost"], qty, price)
        balance, threshold = sql.row(ctx, 'UPDATE "inventory_item" SET quantity = quantity + %s, unit_cost = %s '
                                          "WHERE clinic_id = %s AND id = %s RETURNING quantity, threshold",
                                     [qty, new_cost, ctx.clinic_id, item_id])
        if stock._crossed(balance - qty, balance, threshold):
            deltas.clinic_day(today)
        sql.run(ctx, 'UPDATE "inventory_order_line" SET received = received + %s, unit_price = %s '
                     "WHERE clinic_id = %s AND order_id = %s AND item_id = %s",
                [qty, price, ctx.clinic_id, order_id, item_id])
        movements.append(stock._movement(ctx, item_id, "purchase", qty, balance, new_cost,
                                         expense_id=expense["id"] if expense else None, reason=note, at=now))
    stock._insert_movements(ctx, movements)
    if total > 0 and paid == "account":
        _entry(ctx, supplier[0], "bill", total, today, note, order_id=order_id)
    short = [i for i, (q, r) in ordered.items() if r + (lines.get(i, (0, None))[0]) < q]
    done = close or not short
    if done:
        sql.run(ctx, "UPDATE \"inventory_order\" SET status = 'done', closed_at = %s "
                     "WHERE clinic_id = %s AND id = %s AND status = 'sent'", [now, ctx.clinic_id, order_id])
    record_change(ctx, "order.receive", "order", order_id,
                  after={"lines": {str(i): lines[i][0] for i in item_ids}, "paid": paid if total else None,
                         "total": total if money else None, "done": done})
    deltas.write_clinics(ctx)
    if done and short:
        refill(ctx, short)
    return get_order(ctx, order_id), expense


# --- The account (flow 7) ---------------------------------------------------------------------------------------

ENTRY_COLUMNS = "id, kind, amount, day, order_id, expense_id, note, created_by_id, created_at, voided_at"


def _entry(ctx, supplier_id, kind, amount, day, note, order_id=None, expense_id=None):
    found = sql.row(ctx, f"""
        INSERT INTO "inventory_vendor_entry" (clinic_id, supplier_id, kind, amount, day, order_id, expense_id, note,
                                              created_by_id, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING {ENTRY_COLUMNS}""",
                    [ctx.clinic_id, supplier_id, kind, amount, day, order_id, expense_id, note[:200], ctx.membership_id,
                     timezone.now()])
    entry = dict(zip(stock._cols(ENTRY_COLUMNS), found))
    record_change(ctx, f"vendor.{'bill' if kind == 'bill' else 'pay'}", "vendor_entry", entry["id"],
                  after={"supplier_id": supplier_id, "amount": amount, "day": day.isoformat(), "order_id": order_id})
    return entry


def _check_day(ctx, day):
    today = business_day(ctx.clinic)
    if day > today:
        raise ApiError("invalid", _("The day can't be after today."), fields={"day": "future"})
    if day < today - dt.timedelta(days=366):
        raise ApiError("invalid", _("The day can't be more than a year back."), fields={"day": "past"})


def account(ctx, supplier_id, limit=100):
    """The supplier's bills and payments, newest first, and what we owe."""
    supplier = get_supplier(ctx, supplier_id)
    entries = sql.dicts(ctx, f"""
        SELECT {ENTRY_COLUMNS} FROM "inventory_vendor_entry" WHERE clinic_id = %s AND supplier_id = %s
         ORDER BY day DESC, id DESC LIMIT %s""", [ctx.clinic_id, supplier_id, limit])
    return supplier, entries


def add_bill(ctx, supplier_id, amount, day, note=""):
    get_supplier(ctx, supplier_id)
    _check_day(ctx, day)
    return _entry(ctx, supplier_id, "bill", amount, day, note)


def add_payment(ctx, supplier_id, amount, day, from_shift, note=""):
    """A payment: an expense (materials, or lab for a lab; purchase = true so Expenses can't void it alone) and a
    payment entry. Paying more than owed is allowed (credit)."""
    from finance import services as finance

    if from_shift:
        finance.shift_for(ctx, "cash")
    supplier = get_supplier(ctx, supplier_id)
    _check_day(ctx, day)
    category = finance.category_by_code(ctx, "lab" if supplier["kind"] == "lab" else "materials")
    expense = finance.add_expense(ctx, category, amount, day, (note or supplier["name"])[:200], from_shift,
                                  purchase=True)
    return _entry(ctx, supplier_id, "payment", amount, day, note, expense_id=expense["id"])


def void_entry(ctx, entry_id):
    """Owner only: the entry and its expense together, each conditional on not voided yet. A repeat is harmless."""
    if not (ctx.membership is not None and ctx.membership.is_owner):
        raise ApiError("forbidden", _("Only the account's owner can do this."), 403)
    now = timezone.now()
    found = sql.row(ctx, 'UPDATE "inventory_vendor_entry" SET voided_at = %s, voided_by_id = %s '
                         "WHERE clinic_id = %s AND id = %s AND voided_at IS NULL "
                         "RETURNING supplier_id, kind, amount, day, expense_id",
                    [now, ctx.membership_id, ctx.clinic_id, entry_id])
    if found is None:
        if sql.row(ctx, 'SELECT 1 FROM "inventory_vendor_entry" WHERE clinic_id = %s AND id = %s',
                   [ctx.clinic_id, entry_id]) is None:
            raise _not_found("entry")
        return False
    supplier_id, kind, amount, day, expense_id = found
    if expense_id is not None:
        voided = sql.row(ctx, 'UPDATE "finance_expense" SET voided_at = %s, voided_by_id = %s WHERE clinic_id = %s '
                              "AND id = %s AND voided_at IS NULL RETURNING amount, day",
                         [now, ctx.membership_id, ctx.clinic_id, expense_id])
        if voided:
            Deltas().clinic_day(voided[1], expenses=-voided[0]).write_clinics(ctx)
    record_change(ctx, "vendor.void", "vendor_entry", entry_id,
                  before={"supplier_id": supplier_id, "kind": kind, "amount": amount, "day": day.isoformat(),
                          "expense_id": expense_id})
    return True
