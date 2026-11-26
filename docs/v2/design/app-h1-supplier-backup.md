# Design: supplier backup and "needs a supplier" list (approved by Saged 2026-11-26)

Context: v2/inventory/orders.py (refill, receive, cancel, link_items), v2/inventory/models.py (Item has supplier + backup_supplier;
Supplier; Order one draft per supplier; OrderLine qty/received/unit_price). Raw SQL via core.sql, shard tables under RLS,
composite (clinic, id) FKs added NOT VALID then validated. Front end: v2/web/static/js/orders.js,
v2/web/templates/web/_order_dialogs.html, v2/inventory/order_views.py. Money: none of this changes money.

## Product rules (Saged)
- A supplier may name one backup supplier (optional, in the supplier dialog).
- Backup chosen for an item: the item's own backup_supplier first, else the order supplier's backup. Must be active,
  kind supplier, and not the order's supplier.
- One step only: an item moves to a backup once. If the backup is also short, it goes to the "needs a supplier" list.
- Quantity moved = exactly what is missing (ordered 10, came 4 -> 6).
- Cancelling a whole order is unchanged (sent order's items go back to refill with their main supplier).

## Data model
1. inventory_supplier.backup_id bigint NULL; composite FK (clinic_id, backup_id) -> supplier(clinic_id, id), added NOT
   VALID + validated in a second migration (same as 0007/0008); CHECK backup_id <> id. No chain rule in data: the
   one-step rule is enforced at move time (below), so A->B and B->A is harmless.
2. inventory_order_line.missing int NOT NULL DEFAULT 0 (db_default 0): what the supplier said they don't have / what
   was short when the order closed. CHECK missing >= 0 AND missing <= qty.
3. inventory_order_line.is_backup bool NOT NULL DEFAULT false: line was placed by a backup move or picked by hand from
   the list. A short is_backup line never moves again: it goes to the list.
4. inventory_item.need_qty int NOT NULL DEFAULT 0 (db_default 0), CHECK >= 0: quantity short with nobody to bring it.
   Adding a constant-default column is metadata-only in PG 18. Stock use updates don't touch it.

## Flows (all in services, inside the request transaction, existing lock order:
## request key -> [shift] -> items (sorted, one statement) -> order -> lines -> ...)
A. not_available(order_id, item_id) "مش عنده": order must be 'sent'. Lock item, then order (FOR NO KEY UPDATE), then
   line. remaining = qty - received - missing; if <= 0 -> harmless repeat. Set missing += remaining, route(item,
   remaining). If every line now has received + missing >= qty -> order 'done' (closed_at).
B. receive(..., close=True or all in): for each line still short, short = qty - received - missing; set missing +=
   short and route it (replaces today's refill(short) on done). Items to route are locked with the received ones
   (same sorted lock statement, before the order lock).
C. route(item, qty, from_supplier, line_is_backup):
   - if line_is_backup -> item.need_qty += qty (list).
   - else target = item.backup_supplier if active/kind supplier/<> from_supplier, else from_supplier.backup with the
     same checks. None -> need_qty += qty.
   - target: draft via the existing INSERT ... ON CONFLICT (one draft per supplier); line upsert:
     ON CONFLICT (clinic, order, item) DO UPDATE SET qty = line.qty + EXCLUDED.qty, is_backup = true.
   - record_change "order.backup" (from order, item, qty, to supplier or list).
D. The list "ناقص ومحتاج مورد": (i) items with need_qty > 0, qty = need_qty; plus (ii) active low items
   (quantity <= threshold) with no active supplier and on no draft/sent order, qty = refill formula. Read query on
   the orders page; shard-local; small.
E. pick_supplier(item_id, supplier_id, qty): lock item, check supplier (active, kind supplier), put qty on its draft
   with is_backup = true, set need_qty = 0. The supplier pick list allows "+ new supplier" in place (existing
   create_supplier). Does NOT change the item's supplier (simple; desk can do that in the item dialog).
F. refill() skips items with need_qty > 0 (they are already on the list) — or not? (reviewers: decide)
G. Supplier deactivated: its backup_id stays; ignored at route time. Supplier dialog: backup pick list excludes itself.

## UI
- Supplier dialog: "المورد الاحتياطي" pick list (optional).
- Sent order dialog: per line a small "مش عنده" button (text, no icon); a line with missing shows "ناقص N".
- Orders page top: "ناقص ومحتاج مورد" card listing item, qty, a supplier pick list (type to filter, + مورد جديد),
  hidden when empty. Count joins the side bar badge? (reviewers: say)
- Permissions: same as orders today (inventory permission); no money shown in any new part.

## Tests (write failing first)
missing/route on close; "مش عنده" routes and closes order when all accounted; item backup beats supplier backup;
inactive/same backup skipped; second short on a backup line -> list; list includes low no-supplier items; pick
supplier puts on draft and clears need; repeat "مش عنده" harmless; qty added when item already on backup draft;
RLS/clinic isolation for new endpoints; concurrency: two "مش عنده" on the same line route once.

## Chief judge decision (2026-11-26): APPROVE with these changes

Settled:
1. Sidebar badge: yes. Counts only items with need_qty > 0 (part i), via partial index (clinic_id) WHERE need_qty > 0.
   Joins the existing orders badge ("orders need you"). Card title also shows the count.
2. Late receipt on a line marked missing: allowed and reported (reply + change log say "already moved to X / list").
3. Any receipt sets need_qty = GREATEST(need_qty - received, 0). Setting the item's supplier (link_items, item dialog)
   clears it. A manual "dismiss" on the list row is allowed.

Accepted points:
- F: refill skips items with need_qty > 0. List part (ii) excludes need_qty > 0.
- receive(close) lock order: read the sent order's item ids unlocked, lock received + line items in one sorted
  statement, then the order. Lock order: items (sorted) -> source order -> lines -> target drafts (by supplier id).
- route() runs as one batch grouped by target supplier in id order; queues auto-send like refill.
- remaining/short = GREATEST(qty - received - missing, 0), read after the line lock. Auto-close counts missing.
- "مش عنده" repeat after the order turned done: harmless (accept done; answer unchanged). Line must be on this order.
- pick_supplier: claim with UPDATE ... SET need_qty = 0 WHERE need_qty > 0 RETURNING; repeat harmless; qty box
  pre-filled with need_qty, editable, 1..10^6; need_qty goes to 0.
- update_supplier validates backup (active, kind supplier, this clinic, not itself) -> 400/404, never 500.
- Cancelling a sent order: its is_backup lines go to need_qty (list), others back to refill. Cancelling a draft: its
  is_backup lines go to the list too. Removing a line in edit_draft: deliberate, no list.
- The is_backup upsert turns an existing normal line on the target draft into backup: accepted, documented, tested.
- Migrations: every new CHECK/FK NOT VALID with lock_timeout, validated in a second atomic=False migration.
  No index on backup_id.
- Frontend: card between toolbar and orders list, hidden when empty, own loader refreshed after every action;
  "مش عنده" uses twoTaps, disabled while running, newKey, settled handling, only on sent lines with remaining > 0;
  toast saying where the qty went (server returns destination); picker() with free text -> create_supplier;
  labels: card "محتاجين مورد", "ناقص ٤", "المورد الاحتياطي (اختياري)" with "بدون".

Deferred: version check on edit_draft; backup chains (more than one step).
