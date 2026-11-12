"""The end-of-day sweep and the summary recompute (design 5.10), as functions, commands and Procrastinate tasks.

Both run with a system Ctx (actor "system", no membership), one transaction per clinic (sweep)
or per clinic-day (recompute), on the clinic's shard with row-level security set.

Which places to visit comes from the directory's copy of each place's time zone and day end
(draft 2 check, I6): only places whose business day ended at least 30 minutes ago and that the job
hasn't finished yet. The sweep repairs that copy whenever it differs from the shard. The periodic
tasks below run one job per shard every 10 minutes; the worker is `manage.py procrastinate worker`.
"""

import datetime as dt
import logging

from django.conf import settings
from django.utils import timezone
from procrastinate.contrib.django import app

from billing import selectors as billing_read
from billing import services as billing
from clinics.models import Clinic
from clinics.services import take_numbers
from core import sql
from core.context import Ctx
from core.days import business_day
from core.routing import clinic_transaction
from summaries.services import Deltas, record_visits

from . import selectors

log = logging.getLogger(__name__)

SWEEP_DELAY = dt.timedelta(minutes=30)
SWEEP_CATCH_UP_DAYS = 7  # a sweep also closes the previous week, in case a run was missed
RECOMPUTE_BATCH = 100
CLINIC_BATCH = 500  # places read from the directory at a time (design 5.10)
SWEEP_CRON = "*/10 * * * *"
RECOMPUTE_CRON = "5-59/10 * * * *"  # five minutes after each sweep, which it waits for


def system_ctx(clinic, alias):
    return Ctx(clinic=clinic, membership=None, user_id=None, actor="system", codes=frozenset(), alias=alias)


def _clinic(clinic_id):
    clinic = Clinic.objects.filter(id=clinic_id).first()
    if clinic is None or not clinic.owned:
        return None
    return clinic


def last_closed_day(clinic, now):
    """The business day that ended at least 30 minutes ago."""
    return business_day(clinic, now - SWEEP_DELAY) - dt.timedelta(days=1)


def sweep_clinic(clinic_id, alias, now=None):
    """Close the open visits of the business days that have ended (design 5.10). Returns counts.

    booked → no_show; waiting → no_show with arrived_at kept and auto_closed ("left without
    being seen", pending Saged); in_room → done with auto_closed, its invoice ensured. The
    auto_closed flag is what the owner sees as "flagged".
    """
    return _sweep(clinic_id, alias, now or timezone.now())[0]


def _sweep(clinic_id, alias, now):
    """sweep_clinic's work. Returns (counts, the last business day closed, the shard's clinic row or None)."""
    with clinic_transaction(clinic_id, alias):
        clinic = _clinic(clinic_id)
        if clinic is None:
            return {}, None, None
        ctx = system_ctx(clinic, alias)
        last = last_closed_day(clinic, now)
        first = last - dt.timedelta(days=SWEEP_CATCH_UP_DAYS - 1)
        rows = sql.dicts(ctx, """
            SELECT a.id, a.status, a.day, a.doctor_id, a.patient_id, a.service_id, a.fee, s.name AS service_name
              FROM "scheduling_appointment" a
              JOIN "clinics_service" s ON s.clinic_id = a.clinic_id AND s.id = a.service_id
             WHERE a.clinic_id = %s AND a.day BETWEEN %s AND %s AND a.status IN ('booked', 'waiting', 'with_assistant', 'ready', 'in_room')
             ORDER BY a.id FOR NO KEY UPDATE OF a""", [ctx.clinic_id, first, last])
        if not rows:
            return {}, last, clinic
        sql.run(ctx, """
            UPDATE "scheduling_appointment"
               SET status = CASE status WHEN 'in_room' THEN 'done' ELSE 'no_show' END,
                   auto_closed = (status <> 'booked'),
                   finished_at = CASE status WHEN 'in_room' THEN %s ELSE finished_at END,
                   updated_at = %s, updated_by_id = NULL
             WHERE clinic_id = %s AND id = ANY(%s::bigint[]) AND status IN ('booked', 'waiting', 'with_assistant', 'ready', 'in_room')""",
                [now, now, ctx.clinic_id, [r["id"] for r in rows]])
        in_room = [r for r in rows if r["status"] == "in_room"]
        if in_room:
            invoiced = {r[0] for r in sql.rows(ctx, 'SELECT appointment_id FROM "billing_invoice" WHERE clinic_id = %s '
                                                    "AND appointment_id = ANY(%s::bigint[]) AND status = 'open'",
                                               [ctx.clinic_id, [r["id"] for r in in_room]])}
            missing = [r for r in in_room if r["id"] not in invoiced]
            if missing:
                for appointment, number in zip(missing, take_numbers(ctx, "invoice", len(missing))):
                    billing.create_invoice(ctx, appointment, number)
        deltas = Deltas()
        for r in rows:
            deltas.clinic_day(r["day"])
            if r["status"] != "in_room":
                deltas.doctor_day(r["day"], r["doctor_id"], no_shows=1)  # F8; an auto-closed visit isn't timed
        for r in in_room:
            deltas.doctor_day(r["day"], r["doctor_id"], done=1)
            deltas.clinic_day(r["day"], done=1)
        record_visits(ctx, [(r["patient_id"], r["day"]) for r in in_room])
        deltas.write_doctors(ctx)
        deltas.write_clinics(ctx)
        counts = {"no_show": sum(r["status"] == "booked" for r in rows),
                  "left": sum(r["status"] in ("waiting", "with_assistant", "ready") for r in rows), "auto_done": len(in_room)}
        log.info("sweep clinic=%s days=%s..%s %s", clinic_id, first, last, counts)
        return counts, last, clinic


def targets(job, now, shard=None, clinic_ids=None):
    """The places `job` should visit now, from the directory, in batches by id.

    Without clinic_ids: active places whose business day has ended and that the job hasn't done
    (directory.services.due_clinics). With clinic_ids: those active places, whatever their marks
    (a catch-up run). Each place comes once per call, even if visiting it fails.
    """
    from directory.models import DirectoryClinic
    from directory.services import due_clinics

    if clinic_ids:
        rows = DirectoryClinic.objects.filter(id__in=clinic_ids, status=DirectoryClinic.Status.ACTIVE)
        if shard:
            rows = rows.filter(shard=shard)
        yield from rows.order_by("id").values("id", "shard", "kind", "timezone", "day_ends_at", "name")
        return
    after = 0
    while True:
        batch = due_clinics(job, now, shard=shard, after=after, limit=CLINIC_BATCH)
        yield from batch
        if len(batch) < CLINIC_BATCH:
            return
        after = batch[-1]["id"]


def _reconcile(row, clinic):
    """Repair the directory's copy of the place from the shard, the authority (I6)."""
    from directory.services import record_facts

    facts = {"kind": clinic.kind, "timezone": clinic.timezone, "day_ends_at": clinic.day_ends_at, "name": clinic.name}
    if any(row.get(name) != value for name, value in facts.items()):
        if record_facts(clinic.id, **facts):
            log.warning("directory copy repaired: clinic=%s %s", clinic.id, facts)


def run_sweep(now=None, clinic_ids=None, shard=None):
    """Sweep the places whose business day has just ended, then mark them done in the directory.

    One place failing is logged and doesn't stop the others; it stays due for the next run.
    """
    from directory.services import mark_done

    now = now or timezone.now()
    totals = {}
    for row in targets("sweep", now, shard, clinic_ids):
        try:
            counts, closed, clinic = _sweep(row["id"], row["shard"], now)
        except Exception:
            log.exception("sweep failed: clinic=%s", row["id"])
            totals["failed"] = totals.get("failed", 0) + 1
            continue
        if clinic is not None:
            _reconcile(row, clinic)
            mark_done("sweep", row["id"], closed)
        for key, value in counts.items():
            totals[key] = totals.get(key, 0) + value
    return totals


# --- Recompute -----------------------------------------------------------------------------------------------


def days_to_recompute(clinic_id, alias, today=None):
    with clinic_transaction(clinic_id, alias):
        clinic = _clinic(clinic_id)
        if clinic is None:
            return []
        today = today or business_day(clinic)
        ctx = system_ctx(clinic, alias)
        return [r[0] for r in sql.rows(ctx, 'SELECT day FROM "summaries_clinic_day" WHERE clinic_id = %s '
                                            "AND changed_since_recompute AND day < %s ORDER BY day LIMIT %s",
                                       [clinic_id, today, RECOMPUTE_BATCH])]


REPORT_FIELDS = ("procedures", "no_shows", "timed", "wait_secs", "room_secs")
DOCTOR_CHECKED = ("booked", "done", "collected", "refunded", *REPORT_FIELDS)


def expected_summaries(ctx, day):
    """What the summaries of one clinic-day should hold, from the source rows (the 5.3 formulas)."""
    counts = selectors.day_counts(ctx, day)
    money = billing_read.day_sums(ctx, day)
    doctors = {}
    for doctor_id in {*counts, *(d for d in money if d is not None)}:
        c, m = counts.get(doctor_id, {}), money.get(doctor_id, {})
        doctors[doctor_id] = {"booked": c.get("booked", 0), "done": c.get("done", 0),
                              "collected": m.get("collected", 0), "refunded": m.get("refunded", 0),
                              "max_queue_no": c.get("max_queue_no") or 0,
                              **{name: c.get(name, 0) for name in REPORT_FIELDS}}
    expenses = sql.row(ctx, 'SELECT coalesce(sum(amount), 0)::bigint FROM "finance_expense" WHERE clinic_id = %s '
                            "AND day = %s AND voided_at IS NULL", [ctx.clinic_id, day])[0]
    clinic = {"done": sum(c["done"] for c in counts.values()),
              "collected": sum(m["collected"] for m in money.values()),
              "refunded": sum(m["refunded"] for m in money.values()),
              "expenses": expenses}
    return doctors, clinic


def recompute_day(clinic_id, alias, day):
    """One clinic-day in one transaction: lock the summary rows first, then aggregate, then write.

    Returns a list of differences found (and fixed), the clinic-day's expenses included (the day's non-voided
    finance_expense rows, stock purchases among them).
    """
    differences = []
    unlocked = []
    money_missing = []
    with clinic_transaction(clinic_id, alias):
        clinic = _clinic(clinic_id)
        if clinic is None:
            return differences
        ctx = system_ctx(clinic, alias)
        locked = {r[0]: dict(zip(("last_queue_no", *DOCTOR_CHECKED), r[1:])) for r in sql.rows(
            ctx, f'SELECT doctor_id, last_queue_no, {", ".join(DOCTOR_CHECKED)} FROM "summaries_doctor_day" '
                 "WHERE clinic_id = %s AND day = %s ORDER BY doctor_id FOR UPDATE", [clinic_id, day])}
        money_locked = {(r[0], r[1]): {"collected": r[2], "refunded": r[3]} for r in sql.rows(
            ctx, 'SELECT section_id, method, collected, refunded FROM "summaries_money_day" WHERE clinic_id = %s '
                 "AND day = %s ORDER BY section_id, method FOR UPDATE", [clinic_id, day])}
        current = sql.row(ctx, 'SELECT done, collected, refunded, expenses FROM "summaries_clinic_day" '
                               "WHERE clinic_id = %s AND day = %s FOR UPDATE", [clinic_id, day])
        if current is None:
            return differences
        doctors, totals = expected_summaries(ctx, day)
        updates = []
        for doctor_id in sorted(set(locked) | set(doctors)):
            want = doctors.get(doctor_id, {**dict.fromkeys(DOCTOR_CHECKED, 0), "max_queue_no": 0})
            have = locked.get(doctor_id)
            if have is None:
                # A row created after the lock statement: never lock it now (a flow may hold it while waiting
                # for this clinic-day). Leave the day flagged; the next run sees it.
                unlocked.append(doctor_id)
                continue
            fixed = {name: want[name] for name in DOCTOR_CHECKED}
            fixed["last_queue_no"] = max(have["last_queue_no"], want["max_queue_no"])
            if fixed != have:
                differences.append({"doctor_id": doctor_id, "day": day.isoformat(), "had": have, "fixed": fixed})
                updates.append((doctor_id, fixed))
        if updates:
            names = ("last_queue_no", *DOCTOR_CHECKED)
            values = ", ".join(["(%s::bigint, " + ", ".join(["%s::bigint"] * len(names)) + ")"] * len(updates))
            params = [p for doctor_id, f in updates for p in (doctor_id, *(f[n] for n in names))]
            sets = ", ".join(f"{n} = v.{n}" for n in names)
            sql.run(ctx, f"""
                UPDATE "summaries_doctor_day" d SET {sets}
                  FROM (VALUES {values}) AS v(doctor, {", ".join(names)})
                 WHERE d.clinic_id = %s AND d.day = %s AND d.doctor_id = v.doctor""", [*params, clinic_id, day])
        money_want = billing_read.day_money(ctx, day)
        money_missing = []
        for key in sorted(set(money_locked) | set(money_want)):
            want = money_want.get(key, {"collected": 0, "refunded": 0})
            have = money_locked.get(key)
            if have is None:
                money_missing.append(key)  # created empty after this transaction, like a missing doctor-day
                continue
            if want != have:
                differences.append({"money": list(key), "day": day.isoformat(), "had": have, "fixed": want})
                sql.run(ctx, 'UPDATE "summaries_money_day" SET collected = %s, refunded = %s WHERE clinic_id = %s '
                             "AND day = %s AND section_id = %s AND method = %s",
                        [want["collected"], want["refunded"], clinic_id, day, *key])
        if money_missing:
            unlocked.append(None)
        have_totals = dict(zip(("done", "collected", "refunded", "expenses"), current))
        if have_totals != totals:
            differences.append({"day": day.isoformat(), "had": have_totals, "fixed": totals})
        sql.run(ctx, 'UPDATE "summaries_clinic_day" SET done = %s, collected = %s, refunded = %s, expenses = %s, '
                     "version = version + %s, changed_since_recompute = %s WHERE clinic_id = %s AND day = %s",
                [totals["done"], totals["collected"], totals["refunded"], totals["expenses"], 1 if differences else 0,
                 bool(unlocked),
                 clinic_id, day])
    if unlocked:
        # Rows with source data but no summary row: create them empty in their own transaction (no other
        # lock held), so the next run, which finds the day still flagged, fills them in.
        with clinic_transaction(clinic_id, alias):
            ctx = system_ctx(_clinic(clinic_id), alias)
            for doctor_id in sorted(d for d in unlocked if d is not None):
                sql.run(ctx, 'INSERT INTO "summaries_doctor_day" (clinic_id, day, doctor_id, last_queue_no, booked, done, '
                             "collected, refunded) VALUES (%s, %s, %s, 0, 0, 0, 0, 0) ON CONFLICT DO NOTHING",
                        [clinic_id, day, doctor_id])
            for section_id, method in money_missing:
                sql.run(ctx, 'INSERT INTO "summaries_money_day" (clinic_id, day, section_id, method, collected, refunded) '
                             "VALUES (%s, %s, %s, %s, 0, 0) ON CONFLICT DO NOTHING", [clinic_id, day, section_id, method])
        differences.append({"day": day.isoformat(), "missing_rows": sorted(d for d in unlocked if d is not None),
                            "missing_money": [list(k) for k in money_missing]})
    for difference in differences:
        log.warning("recompute fixed a summary: clinic=%s %s", clinic_id, difference)
    return differences


def recompute_clinic(clinic_id, alias, today=None):
    found = []
    for day in days_to_recompute(clinic_id, alias, today):
        found += recompute_day(clinic_id, alias, day)
    return found


def run_recompute(today=None, clinic_ids=None, shard=None, now=None):
    """Recompute the places the sweep has closed and the recompute hasn't, then mark them done.

    `today` (a business day) only for tests and catch-up runs; by default each place's own.
    """
    from directory.services import mark_done

    now = now or timezone.now()
    found = []
    for row in targets("recompute", now, shard, clinic_ids):
        try:
            found += recompute_clinic(row["id"], row["shard"], today)
        except Exception:
            log.exception("recompute failed: clinic=%s", row["id"])
            continue
        if row.get("closed"):
            mark_done("recompute", row["id"], row["closed"])
    return found


# --- Procrastinate (design 3.1, 5.10): one periodic job per shard ---------------------------------------------


@app.task(name="scheduling.sweep_shard", pass_context=False)
def sweep_shard(timestamp, alias):
    """Every 10 minutes: close the business days that ended on this shard's places."""
    totals = run_sweep(shard=alias)
    if totals:
        log.info("sweep shard=%s %s", alias, totals)
    return totals


@app.task(name="scheduling.recompute_shard", pass_context=False)
def recompute_shard(timestamp, alias):
    """Every 10 minutes, after the sweep: check the summaries of the days the sweep closed on this shard."""
    found = run_recompute(shard=alias)
    return {"differences": len(found)}


for _alias in settings.SHARDS:
    # `queueing_lock` keeps at most one job per shard waiting, so a slow night never piles up. No `lock`: a worker
    # stopped mid-job (Windows has no graceful stop) would leave it held and block that shard's later runs. Two
    # runs at once are safe anyway: each place's sweep and recompute lock their rows and change only what is open.
    app.periodic(cron=SWEEP_CRON, periodic_id=_alias, queueing_lock=f"sweep:{_alias}",
                 task_kwargs={"alias": _alias})(sweep_shard)
    app.periodic(cron=RECOMPUTE_CRON, periodic_id=_alias, queueing_lock=f"recompute:{_alias}",
                 task_kwargs={"alias": _alias})(recompute_shard)
