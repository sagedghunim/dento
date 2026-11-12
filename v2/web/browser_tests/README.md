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

For `e5`, grant the demo doctor `inventory.manage` once (development data only): in `manage.py shell`, `Membership.objects.filter(clinic_id=<clinic id>, user_id=<ahmed's user id>).update(granted=["inventory.manage"])` inside `clinic_transaction(<clinic id>, <shard>)`. The demo owner can't be used: owners need the two-step code.

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
