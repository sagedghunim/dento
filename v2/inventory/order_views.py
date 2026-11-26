"""Supplier and purchase-order endpoints (design H1). Views validate, check codes and call one service."""

from core import paging, validate
from core.api import Result, api
from core.errors import ApiError
from core.numbers import parse_int, parse_money

from billing import visibility

from . import orders
from .views import MAX_LINES, MAX_QTY, _lines

CLOSED_PAGE = 50  # received or cancelled orders shown at a time


def _supplier_id(value):
    return validate.path_id(value, orders._not_found("supplier"))


def _order_id(value):
    return validate.path_id(value, orders._not_found("order"))


def _supplier_values(data, partial):
    values = {}
    for name, size in orders.SUPPLIER_TEXT.items():
        if name in data or (not partial and name == "name"):
            values[name] = validate.text(data, name, size, required=name == "name")
    if partial and "is_active" in data:
        values["is_active"] = validate.boolean(data, "is_active")
    if "visit_days" in data:
        days = data["visit_days"]
        if not isinstance(days, list) or any(isinstance(d, bool) or not isinstance(d, int) or not 0 <= d <= 6
                                             for d in days):
            raise ApiError("invalid", "visit_days must be weekdays 0-6", fields={"visit_days": "invalid"})
        values["visit_days"] = sorted(set(days))
    if "visit_time" in data:
        values["visit_time"] = _clock(data["visit_time"])
    if "backup_id" in data:  # design H1 backup: null clears it
        values["backup_id"] = None if data["backup_id"] in (None, "") else validate.ident(data["backup_id"], "backup_id")
    return values


def _clock(value):
    """"HH:MM" (24 hours) or "" for none."""
    if value in (None, ""):
        return ""
    if isinstance(value, str) and len(value) == 5 and value[2] == ":" and value[:2].isdigit() and value[3:].isdigit():
        hour, minute = int(value[:2]), int(value[3:])
        if hour < 24 and minute < 60:
            return value
    raise ApiError("invalid", "visit_time must be HH:MM", fields={"visit_time": "invalid"})


def _item_ids(data):
    """The optional tick boxes: None when not sent (the links stay as they are)."""
    if "item_ids" not in data:
        return None
    ids = data["item_ids"]
    if not isinstance(ids, list) or len(ids) > 2000:
        raise ApiError("invalid", "item_ids must be a list of ids", fields={"item_ids": "invalid"})
    return [validate.ident(i, "item_ids") for i in ids]


def _supplier_answer(ctx, supplier):
    return {"supplier": supplier, "item_ids": orders.supplier_items(ctx, supplier["id"])}


def _replay_supplier(ctx, stored):
    return _supplier_answer(ctx, orders.get_supplier(ctx, stored["target_id"]))


def _replay_order(ctx, stored):
    return {"order": orders.get_order(ctx, stored["target_id"])}


# --- Suppliers --------------------------------------------------------------------------------------------------


@api("GET", perm="inventory.order")
def suppliers_view(request, ctx, data):
    """?all=1 keeps the inactive ones. `owed` is None without money.view."""
    return {"suppliers": orders.suppliers(ctx, include_inactive=request.GET.get("all") == "1")}


@api("POST", perm="inventory.manage", replay=_replay_supplier)
def create_supplier_view(request, ctx, data):
    """POST {name, phone?, notes?, visit_days?: [0-6, Monday = 0], visit_time?: "HH:MM", item_ids?: [...]}."""
    supplier = orders.create_supplier(ctx, _supplier_values(data, partial=False), item_ids=_item_ids(data))
    return Result(_supplier_answer(ctx, supplier), status=201, target_type="supplier", target_id=supplier["id"])


@api("PATCH", perm="inventory.manage", replay=_replay_supplier)
def update_supplier_view(request, ctx, data, supplier_id):
    """PATCH what changed; item_ids, when sent, is the whole list of the supplier's items."""
    supplier_id = _supplier_id(supplier_id)
    supplier = orders.update_supplier(ctx, supplier_id, _supplier_values(data, partial=True), item_ids=_item_ids(data))
    return Result(_supplier_answer(ctx, supplier), target_type="supplier", target_id=supplier_id)


@api("GET", perm=("inventory.order", "money.view"))
def account_view(request, ctx, data, supplier_id):
    supplier, entries = orders.account(ctx, _supplier_id(supplier_id))
    return {"supplier": supplier, "entries": entries}


def _replay_entry(ctx, stored):
    return {"entry_id": stored["target_id"], "replayed": True}


@api("POST", perm=("inventory.order", "money.view"), replay=_replay_entry)
def bill_view(request, ctx, data, supplier_id):
    """POST {amount, day, note?}: a bill we owe (a receipt taken without prices, or the lab's invoice)."""
    entry = orders.add_bill(ctx, _supplier_id(supplier_id), parse_money(data.get("amount"), "amount", 1),
                            validate.iso_day(data.get("day")), validate.text(data, "note", 200, required=False))
    return Result({"entry": entry}, status=201, target_type="vendor_entry", target_id=entry["id"])


@api("POST", perm=("inventory.order", "money.view", "expenses.manage"), replay=_replay_entry)
def payment_view(request, ctx, data, supplier_id):
    """POST {amount, day, from_shift, note?}: a payment (an expense too)."""
    entry = orders.add_payment(ctx, _supplier_id(supplier_id), parse_money(data.get("amount"), "amount", 1),
                               validate.iso_day(data.get("day")), validate.boolean(data, "from_shift", False),
                               validate.text(data, "note", 200, required=False))
    return Result({"entry": entry}, status=201, target_type="vendor_entry", target_id=entry["id"])


@api("POST", perm=("inventory.order", "money.view"))
def void_entry_view(request, ctx, data, entry_id):
    """The owner voids a bill or a payment (with its expense)."""
    entry_id = validate.path_id(entry_id, orders._not_found("entry"))
    return Result({"voided": orders.void_entry(ctx, entry_id)}, target_type="vendor_entry", target_id=entry_id)


# --- Orders -----------------------------------------------------------------------------------------------------


@api("GET", perm="inventory.order")
def orders_view(request, ctx, data):
    """?state=open (draft, sent) or closed; closed ones a page at a time (?after= the answer's `next`)."""
    state = request.GET.get("state", "open")
    if state not in ("open", "closed"):
        raise ApiError("invalid", "state must be open or closed", fields={"state": "choice"})
    if state == "open":
        return {"orders": orders.orders(ctx, state), "next": None, "drafts": orders.draft_count(ctx),
                "needs": orders.need_count(ctx)}
    rows = orders.orders(ctx, state, CLOSED_PAGE + 1, paging.time_cursor(request))
    found = paging.page(rows, CLOSED_PAGE, lambda o: paging.encode_time(o["created_at"], o["id"]))
    return {"orders": found["items"], "next": found["next"], "drafts": orders.draft_count(ctx),
            "needs": orders.need_count(ctx)}


@api("POST", perm="inventory.order", replay=_replay_order)
def create_order_view(request, ctx, data):
    """POST {supplier_id, lines: [{item_id, qty}], note?}: an order by hand (into the supplier's draft if one is open)."""
    order = orders.create_order(ctx, validate.ident(data.get("supplier_id"), "supplier_id"), _lines(data),
                                validate.text(data, "note", 300, required=False))
    return Result({"order": order}, status=201, target_type="order", target_id=order["id"])


@api("POST", perm="inventory.order")
def refill_view(request, ctx, data):
    """"Order all that's short": every low item with a supplier goes on its supplier's draft."""
    touched = orders.refill(ctx)
    return {"orders": orders.orders(ctx), "touched": touched}


@api("GET", perm="inventory.order")
def order_view(request, ctx, data, order_id):
    return {"order": orders.get_order(ctx, _order_id(order_id))}


@api("PUT", perm="inventory.order", replay=_replay_order)
def edit_order_view(request, ctx, data, order_id):
    """PUT {lines: [{item_id, qty}], note?}: a draft's lines as left (absolute; an empty list empties it)."""
    order_id = _order_id(order_id)
    lines = {} if data.get("lines") == [] else _lines(data)
    note = validate.text(data, "note", 300, required=False) if "note" in data else None
    return Result({"order": orders.edit_draft(ctx, order_id, lines, note)}, target_type="order", target_id=order_id)


def _replay_send(ctx, stored):
    order, text, link = orders.send(ctx, stored["target_id"])
    return {"order": order, "text": text, "link": link}


@api("POST", perm="inventory.order", replay=_replay_send)
def send_view(request, ctx, data, order_id):
    """Draft -> sent: the WhatsApp text and its wa.me link. A repeat on a sent order answers the same."""
    order_id = _order_id(order_id)
    order, text, link = orders.send(ctx, order_id)
    return Result({"order": order, "text": text, "link": link}, target_type="order", target_id=order_id)


@api("POST", perm="inventory.order")
def cancel_view(request, ctx, data, order_id):
    order_id = _order_id(order_id)
    return Result({"order": orders.cancel(ctx, order_id)}, target_type="order", target_id=order_id)


def _receive_lines(ctx, data):
    """[{item_id, qty, unit_price?}] -> {item_id: (qty, price)}. With money.view a received line needs its price;
    without it the server refuses prices (they'd be guesses)."""
    raw = data.get("lines")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_LINES:
        raise ApiError("invalid", f"lines must be a list of 1 to {MAX_LINES} items", fields={"lines": "list"})
    money = visibility.sees(ctx)
    lines = {}
    for entry in raw:
        if not isinstance(entry, dict):
            raise ApiError("invalid", "each line needs item_id and qty", fields={"lines": "item"})
        item_id = validate.ident(entry.get("item_id"), "item_id")
        if item_id in lines:
            raise ApiError("invalid", "an item appears twice", fields={"lines": "duplicate"})
        qty = parse_int(entry.get("qty"), "qty", 0, MAX_QTY)
        price = None
        if entry.get("unit_price") not in (None, ""):
            if not money:
                raise ApiError("forbidden", "unit_price needs money.view", 403)
            price = parse_money(entry.get("unit_price"), "unit_price")
        elif money and qty > 0:
            raise ApiError("invalid", "unit_price is required", fields={"unit_price": "required"},
                           extra={"item_id": item_id})
        lines[item_id] = (qty, price)
    if not any(q for q, _p in lines.values()) and not data.get("close"):
        raise ApiError("invalid", "nothing was received", fields={"lines": "empty"})
    return lines


@api("POST", perm="inventory.order", replay=_replay_order)
def receive_view(request, ctx, data, order_id):
    """POST {lines: [{item_id, qty, unit_price?}], paid?: drawer|outside|account, close?} (design H1 flow 5)."""
    order_id = _order_id(order_id)
    lines = _receive_lines(ctx, data)
    paid = data.get("paid")
    if paid is not None:
        if not visibility.sees(ctx):
            raise ApiError("forbidden", "paid needs money.view", 403)
        paid = validate.choice(data, "paid", orders.PAID)
        if paid == "drawer":
            ctx.require("cash.collect")
    order, expense, moved, late = orders.receive(ctx, order_id, lines, paid, validate.boolean(data, "close", False))
    return Result({"order": order, "expense_id": expense["id"] if expense else None, "moved": moved, "late": late},
                  status=201, target_type="order", target_id=order_id)


# --- The backup and the "needs a supplier" list (design H1 backup) ---------------------------------------------


@api("POST", perm="inventory.order")
def missing_view(request, ctx, data, order_id):
    """POST {item_id}: "مش عنده" on a sent order's line. `moved` says where the rest went (empty on a repeat)."""
    order_id = _order_id(order_id)
    order, moved = orders.not_available(ctx, order_id, validate.ident(data.get("item_id"), "item_id"))
    return Result({"order": order, "moved": moved}, target_type="order", target_id=order_id)


@api("GET", perm="inventory.order")
def needs_view(request, ctx, data):
    return {"items": orders.needs(ctx)}


def _need_item(value):
    return validate.path_id(value, orders.stock.not_found("item"))


@api("POST", perm="inventory.order")
def pick_view(request, ctx, data, item_id):
    """POST {supplier_id, qty}: the item goes on that supplier's draft. A repeat answers the order it is on (200)."""
    item_id = _need_item(item_id)
    order, created = orders.pick_supplier(ctx, item_id, validate.ident(data.get("supplier_id"), "supplier_id"),
                                          parse_int(data.get("qty"), "qty", 1, MAX_QTY))
    return Result({"order": order}, status=201 if created else 200, target_type="order", target_id=order["id"])


@api("POST", perm="inventory.order")
def dismiss_view(request, ctx, data, item_id):
    item_id = _need_item(item_id)
    orders.dismiss_need(ctx, item_id)
    return Result({"items": orders.needs(ctx)}, target_type="item", target_id=item_id)
