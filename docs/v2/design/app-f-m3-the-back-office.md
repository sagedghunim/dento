## Appendix F: M3, the back office

Saged's scope of 2026-11-12, taken from the TabeebHub screens he sent. Only the data, rules and API are built now; the screens come with his UI plan. Every list is editable by the clinic, with "+", edit and remove. Each action takes its own permission code.

### F1. Cash register (per place)
- **Shifts.** A shift opens with a counted opening float, carries every cash payment and refund taken in it, and closes with a counted amount. Closing records the difference from what the shift expected.
- **One open shift per place.** One person opens it, and anyone with `cash.collect` can use it.
- **Cash never fails for want of a shift** (changed 2026-11-12: a solo doctor never opens a register, and booking with cash was refused). When no shift is open, a cash payment, refund, cash movement, expense from the drawer or payout opens one in the same transaction: the actor is the opener, the opening float is 0, and the shift is marked `auto_opened`. Two first entries share one shift (the one-open-shift unique index, `ON CONFLICT DO NOTHING`, then select). An entry that waited behind a close opens a fresh shift, so a counted shift never gains an entry. Explicit open (with a counted float) and close stay for places that want counted handovers; closing a shift that is already closed is 409 `shift_closed`. `register_closed` is gone. Card, transfer and wallet join the open shift when there is one and never need one.
- **Linked to the shift.** Payments record `shift_id`.
- **Cash movements.** Cash in and cash out, such as petty expenses or a bank deposit, each with a reason.
- **Report.** The shift report shows totals by method and the movements.

### F2. Inventory and materials
- **Items.** Each item has a name, a category, an optional SKU, a unit, the quantity on hand, a minimum-quantity alert, a purchase cost (the average cost per unit), notes and `is_active`. Items belong to a section, or are shared across the place.
- **Movements.** Purchase, use, adjustment and waste. A movement changes stock only through a conditional update; stock never goes below 0 unless the place allows it.
- **Materials.** A procedure carries a list of items and quantities. Recording the procedure deducts those quantities and sets the procedure line's internal cost from the average cost.
- **Low stock.** Items below their minimum appear on Today's `low_stock` list, replacing the M1 stub.

### F3. Expenses
- An expense has a category, an amount, a date, a note, and optionally the shift it was paid from.
- Categories are editable. The defaults are rent, materials, lab, utilities and other. There is no default "salaries" category: staff pay reaches profit through pay rules (F4, F5), so a seeded salaries expense would count it twice (M3 review). A place that pays staff outside pay rules can add its own category.

### F4. Staff pay
- **Pay rules per member.** A rule is a percentage of the revenue they produce, a fixed amount per visit or procedure, a monthly salary, or a mix of these.
- **Monthly statement per member.** The statement lists the visits and procedures that member did, the revenue and refunds attributed to them, what they earned, the payouts already made, and the balance still owed. Revenue is attributed to the doctor of the visit.
- **Payouts.** Payouts are recorded against the statement. Paying out cash requires an open shift.

### F5. Receivables and money reports
- **Receivables.** Each patient with an unpaid balance, how old the balance is, and their last visit.
- **Revenue.** Revenue by day, by month, by doctor, by section, and by payment method.
- **Profit.** Monthly profit is revenue minus refunds minus expenses minus staff pay. The summaries tables from design 4.4 are extended for this.

### F6. Patient insights and tags
- **Counts.** Active patients (seen in the last 6 months), new this month, at risk (they used to return but have gone quiet), reactivated this month, the repeat rate, and revenue per patient.
- **Lists.** Never came back, going quiet, unfinished treatment, unused prepaid sessions (once F7 exists), and outstanding balance. Lists are filterable, paged and exportable to CSV, and each export writes an access event.
- **Tags.** Patient tags are editable per place, such as VIP, insurance or a referral source. A patient can carry several tags, and the lists and search can filter by tag.

### F7. Session packages
- **A package** is a procedure or service, a number of sessions and a price. Selling one to a patient creates an invoice line and a remaining count of sessions.
- **Using a session.** Recording that procedure on a later visit uses up one session at no charge.
- **Unused sessions** show on the patient and in the insights lists. Physiotherapy, dermatology and dental all use this.

### F8. Reports
- **Clinic performance.** Visits, no-shows, average wait and doctor time, and revenue per day and per month.
- **Staff performance.** Visits, revenue and average doctor time per member.
- All of these read from the summaries, never from a scan of visits.
- `reports.view` is not owner-only, so the money in these reports and in the F6 counts (`collected`, `refunded`, `revenue`, `revenue_this_month`, `revenue_per_patient`) goes only to holders of `finances.view`. Others get the counts with `money_hidden: true`, and staff performance is ordered by visits instead of revenue.

### F9. Decisions from the M3 review (2026-11-12)
- **Packages cover procedures only in M3.** F7 says "procedure or service"; a package points at a procedure and a session is used by recording that procedure. Packages for services come later.
- **Removing a procedure line gives its session back.** Under the invoice lock (invoice, then package, as void does), `used = used - 1 … AND used > 0`, the record's package and line links are cleared, and it is audited (`package.session_return`).
- **The recompute repairs expenses.** `clinic_day.expenses` is checked against the day's non-voided `finance_expense` rows (purchases included) and fixed like the other totals.
- **Adding an expense is audited** (`expense.add`), as its void is, because a back-dated expense changes a closed month's profit.
- **The CSV export never cuts silently.** A list longer than 5,000 rows answers 409 `export_too_large` and asks for a tag filter; nothing is exported or logged.
- **Seeded stock may go below zero until counted.** Default items added by `POST inventory/defaults` start at zero with `allow_negative = true`, so an uncounted material never refuses a recording (409 `out_of_stock`). The place turns it off per item after its first count; doing that automatically needs a "counted" marker column, planned later.
