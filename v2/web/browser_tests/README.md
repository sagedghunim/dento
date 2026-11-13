# Browser tests

Headless Edge, driven over the DevTools protocol by plain Node scripts (Node 24 or later; no packages).
They cover what Django's test client can't: the M1 decisions R2, R7 and R8.

| Test | Checks |
|---|---|
| `e1_rows_stay.mjs` | R8: at 390 px with touch, a row under a resting finger stays put when a booking lands above it; a tap within 700 ms of a row's redraw is ignored |
| `e2_no_double_booking.mjs` | R7: a booking whose answer is lost can't be closed away, "Try again" books once, and a page change lists it instead of resending it |
| `e3_finish_collect.mjs` | R2: "Finish and collect" after another screen finished the visit says nothing was collected, and "Collect now" takes the payment once |
| `e4_dental_chart.mjs` | The dental chart, as a doctor (`HAMADA_BT_USER=ahmed`): a double-clicked finding and procedure save once, a dropped connection keeps the save for "Try again", Add to plan marks the tooth in progress, an expired session goes to sign-in. Not in `run.mjs` (it needs a doctor). `HAMADA_BT_SHOTS=<folder>` takes the 1280 and 390 px screenshots |
| `e5_inventory_rx.mjs` | Inventory and Prescription, as someone with `inventory.manage` and `settings.medicines` (`HAMADA_BT_USER=ahmed` after granting him `inventory.manage`, see below): the starter list, a new item, a double-clicked purchase raises the stock once, a dropped connection keeps the purchase for "Try again", use lowers the stock, the item's history, a medicine added once, a ready prescription that picks it, 390 px without sideways scrolling, an expired session goes to sign-in. Not in `run.mjs`. `HAMADA_BT_SHOTS=<folder>` takes the 1280 and 390 px screenshots |
| `e7_finances.mjs` | Finances, as someone with `finances.view`, `finances.pay`, `expenses.manage` and the register codes (`HAMADA_BT_USER=ahmed` after granting him the first three): a double-clicked expense counts once and the month's expenses rise by it, a dropped connection keeps an expense for "Try again", void, a new category offered for expenses, cash in and a double-clicked cash out move the expected cash once each, a shift's report, every tab at 390 px, an expired session goes to sign-in. Not in `run.mjs`. `HAMADA_BT_SHOTS=<folder>` takes the screenshots |
| `e9_desk.mjs` | The front desk review: a ready row offers Call in and isn't marked "didn't come", who needs the desk comes first and finished, paid rows fold, phones call and open WhatsApp, "Didn't come" by hand (double-clicked, undone from the toast, and held while offline), "Book the next visit" opens Book on the patient, Book starts unticked and Walk-in takes the typed name with "collect" ticked, the earlier balance is collected with the finish once, 390 px, an expired session goes to sign-in. In `run.mjs`. `HAMADA_BT_SHOTS=<folder>` takes Today at 1280 and 390 px |
| `e10_visit.mjs` | The doctor's visit screen, as a doctor with a queue today (`HAMADA_BT_USER=ahmed`; the desk is `HAMADA_BT_DESK`, default `sara`): the doctor's Call in opens `/visits/<id>`, the timer runs, notes save once per text through a dropped connection and an expired session, a double-clicked procedure and prescription save once, the chart opens inside, leaving warns, Today comes back to the visit, a double-clicked finish lands on Today with the next patient, 390 px, and the desk's Call in stays on Today. Not in `run.mjs`. `HAMADA_BT_SHOTS=<folder>` takes the 1280 and 390 px screenshots |
| `e8_overview.mjs` | Overview, as someone with `reports.view` and `finances.view` (`HAMADA_BT_USER=ahmed` after granting both): each period, money shown, the team table, Overview in the side bar, 390 px. Not in `run.mjs`. `HAMADA_BT_SHOTS=<folder>` takes the screenshots |

For `e5`, grant the demo doctor `inventory.manage` once (development data only): in `manage.py shell`, `Membership.objects.filter(clinic_id=<clinic id>, user_id=<ahmed's user id>).update(granted=["inventory.manage"])` inside `clinic_transaction(<clinic id>, <shard>)`. The demo owner can't be used: owners need the two-step code.

For `e7` and `e8`, grant him `["finances.view", "finances.pay", "expenses.manage", "reports.view"]` the same way.

## Run

1. Start a dev server with demo data (`seed_demo`) on a day the demo dental clinic works (Saturday to Thursday). It needs two doctors working today.
2. From the repo root:

```
node v2/web/browser_tests/run.mjs
```

Each file also runs alone, for example `node v2/web/browser_tests/e1_rows_stay.mjs`.

Settings, from the environment:

| Variable | Default |
|---|---|
| `HAMADA_BT_BASE` | `http://127.0.0.1:8003` |
| `HAMADA_BT_USER` | `sara` (the front desk) |
| `HAMADA_BT_PASSWORD` | `Hamada-Demo-2026` |
| `HAMADA_BT_CLINIC` | `سمايل` (part of the clinic's name, for someone in several clinics) |
| `HAMADA_BT_EDGE` | `C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe` |

The tests book new patients named `Bt …` on the server they run against. Run them against development data only.
