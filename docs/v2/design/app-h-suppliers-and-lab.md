# Appendix H: suppliers, purchase orders and the dental lab

Draft 2, 2026-11-25. Draft 1 went to the full panel; the chief judge asked for REVISE (narrow): this draft applies
its nine changes and five rulings. Next: senior-backend checks draft 2 alone, then H1 is built.

Saged's decisions (memory `supplier-orders-decisions`): one supplier per item, optional backup; low stock prepares an
order, the desk approves, WhatsApp to the supplier; auto-send is an owner switch, off; what we owe suppliers and labs
is built now; lab cost off the doctor's share is an owner switch; doctor or desk sends lab work.

Two steps: H1 suppliers and orders (built first), H2 the lab. One vendor table holds both kinds, so the account
(what we owe, what we paid) is written once.

## H1. Suppliers and orders

### Tables (shard, forced RLS, composite (clinic, id) keys as in 4.7)
- `inventory_supplier`: name 120, phone 20, notes 300, `kind` supplier|lab, is_active, created_at.
  Unique active (clinic, kind, name). CHECK kind in (supplier, lab); phone digits only (`^[0-9]*$`); the service
  stores Egyptian numbers with the country code (01x → 201x) for wa.me.
- `inventory_item` + `supplier_id` null, `backup_supplier_id` null, `max_qty` int db_default 0 (0 = up to twice the
  minimum). CHECK supplier ≠ backup, max_qty ≥ 0. Live table: nullable columns and db_default are metadata only;
  the composite FKs and CHECKs are added `NOT VALID`, validated in a second migration; index (clinic, supplier_id)
  built `CONCURRENTLY` (atomic=False, lock_timeout). `max_qty` not indexed (stock updates stay HOT).
  The service checks both are kind supplier and active.
- `inventory_order`: supplier, `status` draft|sent|done|cancelled, created_at, created_by_id (null = the system),
  sent_at, sent_by_id, closed_at, note 300. CHECKs: status valid; sent_at set when sent or done; closed_at set
  exactly when done or cancelled. Partial unique (clinic, supplier) WHERE status = 'draft'. Index
  (clinic, status, created_at DESC, id DESC) for the list and the side-bar count.
- `inventory_order_line`: order, item, qty > 0, received ≥ 0, unit_price ≥ 0 (last receipt). Unique
  (clinic, order, item); index (clinic, item) for "already on an open order".
- `inventory_vendor_entry`: supplier, kind bill|payment, amount > 0, day, order_id null, expense_id null, note 200,
  created_by_id, created_at, voided_at, voided_by_id. CHECKs: kind valid; voided_at and voided_by_id both set or
  both null. Index (clinic, supplier) INCLUDE (kind, amount) WHERE voided_at IS NULL.
  What we owe = Σ bills − Σ payments, one indexed sum per supplier; below zero shows as credit. (No balance column
  for now: few rows per supplier; revisit if slow.)

### Lock order
request key → [shift, only when paid from the drawer] → items (one statement, sorted by id) → order (conditional
status update) → lines → vendor entries → expenses → clinic-days. Refill, send, receive and cancel all follow it.

### Flows
1. **Refill.** Only on a downward crossing (`before > threshold AND after <= threshold`) in take, finish_visit,
   adjust, waste. For each such item with an active supplier and not already on a draft or sent order:
   `INSERT … ON CONFLICT (clinic_id, supplier_id) WHERE status = 'draft' DO UPDATE SET note = order.note
   RETURNING id` (locks the draft), then insert lines `ON CONFLICT DO NOTHING`. qty = max_qty − quantity, or
   2 × threshold − quantity when max_qty is 0; at least 1. Refill also runs for the touched items after a cancel, a
   close with lines short, and a supplier newly set on an item. Not after a line removed from a draft (it would
   come straight back): that item waits for the button or its next crossing. One button "اطلب كل الناقص" runs it
   for every low item. No scheduled sweep.
2. **The desk sees it.** Inventory parts: Items | Orders | Suppliers (Orders first for the desk). The side bar shows
   the count of drafts as text ("طلبات الشراء (3)") to anyone with `inventory.order`, from the draft index, read with
   the page (no new poll). A draft opens as a page: qty per line, remove, add item (pick list), note. Anyone with
   `inventory.order` can start an order by hand.
3. **Send.** `POST /orders/{id}/send`: lock the order `WHERE status = 'draft'`, set sent. Answer: the Arabic message
   (clinic name, items, qty, unit; no prices, no patient data) and the wa.me link. A repeat on a sent order answers
   200 with the same text. Page: opens a blank window in the click, before the await, then sets its address; if the
   window is blocked, a big "افتح واتساب" link. Sent orders keep "ابعت تاني" (same link, no change).
   `core/whatsapp.send(phone, template, params)` is the one hook for Confirmed EG (env `WHATSAPP_PROVIDER`,
   `WHATSAPP_TOKEN`, `WHATSAPP_SENDER`); unset → None and the page uses the link.
4. **Auto-send** (setting `orders_auto_send`, owner only, off; does nothing while the hook is unset): after refill
   commits, a background job claims the order first with the conditional draft → sent update (only one claim wins),
   then calls the hook outside any request. On failure the order goes back to draft (conditional sent → draft) and
   is audited (`order.send_failed`), so the desk sends it by hand.
5. **Receive.** `POST /orders/{id}/receive {lines: [{item_id, qty, unit_price?}], paid?: drawer|outside|account,
   close?}`, with the idempotency key. Refuses done or cancelled (409). One batch: items locked sorted, then the order row locked
   (`FOR NO KEY UPDATE … AND status = 'sent'`, else 409), each line `received = received + q` (more than ordered is allowed: suppliers send extras; the
   page warns), purchase movement + average cost per line.
   - With `money.view`: unit_price per line and `paid` required (no default; the total shown first). drawer → one
     expense from the open register (option off with a reason when none is open); outside → one expense not from
     the drawer; account → one `bill` entry, no expense yet.
   - Without `money.view`: quantities only; stock valued at the item's current unit_cost; no expense, no bill; the
     server rejects unit_price and paid. Someone with money.view adds the bill later from the supplier's account
     ("add bill").
   - The order is done when every line is fully received or `close` is sent; lines short then go back to refill.
6. **Cancel.** draft or sent → cancelled (conditional update), no stock or money change, audited; its items go back
   to refill.
7. **Account.** `POST /suppliers/{id}/bills {amount, day, note}` and `POST /suppliers/{id}/payments {amount, day,
   from_shift, note}`, both with the idempotency key. A payment = an expense (category materials, or lab for a lab,
   `purchase` true so the expenses screen can't void it) + a payment entry. Paying more than owed is allowed (credit).
   Void (owner only, own service): the entry and its expense together, both conditional on voided_at IS NULL,
   audited. A wrong receipt: the owner voids its bill or expense and fixes stock with a count (automatic stock reversal
   deferred).

### Permissions and money
- New code `inventory.order` ("Prepare, send and receive purchase orders"), group stock, in the assistant template
  (not the doctor's): the doctor role never sees Orders or Suppliers. Suppliers list and edit: `inventory.manage`.
  Prices, the account, bills and payments: `money.view`; payments also `expenses.manage`. Responses strip
  unit_price and amounts owed for anyone without money.view.
- Audit: supplier.add/edit, order.send, order.receive, order.cancel, vendor.bill, vendor.pay, vendor.void.
- Buttons are disabled while their request runs; a timeout says "مش متأكد إنه اتحفظ" and reloads the order.

### Deferred
Automatic stock reversal of a receipt, a balance column, a scheduled refill sweep, phone checks beyond digits, showing
where a draft came from.

## H2. The dental lab (next, after H1)
- A lab is an `inventory_supplier` with kind lab. `lab_price`: lab, work (pick list: zircon, PFM, E-max, full denture,
  partial, night guard, temporary…), price, days (usual turnaround).
- `lab_case`: patient, doctor (membership), appointment null, lab, work, teeth (from the chart), shade, units, price
  (from the lab's list, editable), status open|done|cancelled, created_by.
- `lab_step`: case, step (impression, metal try-in, bisque, final, wax rim, teeth try-in, delivery, redo), sent_at,
  sent_by, due_on, back_at, result ok|redo, redo_by lab|clinic, note. Index (clinic, due_on) WHERE back_at IS NULL.
- A finished case writes one `bill` (vendor_entry gets `lab_case_id`, partial unique on bills); a lab redo costs
  nothing. Owner switch `lab_cost_from_doctor`: the doctor's share is worked out after the lab cost (F4 pay rules).
- Alerts (desk and owner): due tomorrow and not back; patient booked before the work is due; back with no booking.
- Lab slip: print or WhatsApp (no prices for the doctor role). Needs its own panel review before building.
