"""The end-of-day sweep and the summary recompute (design 5.10). Plain functions; commands call them.

Both run with a system Ctx (actor "system", no membership), one transaction per clinic (sweep)
or per clinic-day (recompute), on the clinic's shard with row-level security set.
"""

import datetime as dt
import logging

from django.utils import timezone

from billing import selectors as billing_read
from billing import services as billing
from clinics.models import Clinic
from clinics.services import take_numbers
from core import sql
from core.context import Ctx
from core.days import business_day
from core.routing import clinic_transaction
from summaries.services import Deltas

from . import selectors

log = logging.getLogger(__name__)

SWEEP_DELAY = dt.timedelta(minutes=30)
SWEEP_CATCH_UP_DAYS = 7  # a sweep also closes the previous week, in case a run was missed
RECOMPUTE_BATCH = 100


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
    now = now or timezone.now()
    with clinic_transaction(clinic_id, alias):
        clinic = _clinic(clinic_id)
        if clinic is None:
            return {}
        ctx = system_ctx(clinic, alias)
        last = last_closed_day(clinic, now)
        first = last - dt.timedelta(days=SWEEP_CATCH_UP_DAYS - 1)
        rows = sql.dicts(ctx, """
            SELECT a.id, a.status, a.day, a.doctor_id, a.patient_id, a.service_id, a.fee, s.name AS service_name
              FROM "scheduling_appointment" a
              JOIN "clinics_service" s ON s.clinic_id = a.clinic_id AND s.id = a.service_id
             WHERE a.clinic_id = %s AND a.day BETWEEN %s AND %s AND a.status IN ('booked', 'waiting', 'in_room')
             ORDER BY a.id FOR NO KEY UPDATE OF a""", [ctx.clinic_id, first, last])
        if not rows:
            return {}
        sql.run(ctx, """
            UPDATE "scheduling_appointment"
               SET status = CASE status WHEN 'in_room' THEN 'done' ELSE 'no_show' END,
                   auto_closed = (status <> 'booked'),
                   finished_at = CASE status WHEN 'in_room' THEN %s ELSE finished_at END,
                   updated_at = %s, updated_by_id = NULL
             WHERE clinic_id = %s AND id = ANY(%s::bigint[]) AND status IN ('booked', 'waiting', 'in_room')""",
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
        for r in in_room:
            deltas.doctor_day(r["day"], r["doctor_id"], done=1)
            deltas.clinic_day(r["day"], done=1)
        deltas.write_doctors(ctx)
        deltas.write_clinics(ctx)
        counts = {"no_show": sum(r["status"] == "booked" for r in rows),
                  "left": sum(r["status"] == "waiting" for r in rows), "auto_done": len(in_room)}
        log.info("sweep clinic=%s days=%s..%s %s", clinic_id, first, last, counts)
        return counts


def directory_clinics():
    """(clinic id, shard) of every active clinic, in batches, from the directory."""
    from directory.models import DirectoryClinic

    return DirectoryClinic.objects.filter(status=DirectoryClinic.Status.ACTIVE).order_by("id").values_list(
        "id", "shard").iterator(chunk_size=500)


def run_sweep(now=None, clinic_ids=None):
    totals = {}
    for clinic_id, alias in directory_clinics():
        if clinic_ids and clinic_id not in clinic_ids:
            continue
        for key, value in sweep_clinic(clinic_id, alias, now).items():
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


def expected_summaries(ctx, day):
    """What the summaries of one clinic-day should hold, from the source rows (the 5.3 formulas)."""
    counts = selectors.day_counts(ctx, day)
    money = billing_read.day_sums(ctx, day)
    doctors = {}
    for doctor_id in {*counts, *(d for d in money if d is not None)}:
        c, m = counts.get(doctor_id, {}), money.get(doctor_id, {})
        doctors[doctor_id] = {"booked": c.get("booked", 0), "done": c.get("done", 0),
                              "collected": m.get("collected", 0), "refunded": m.get("refunded", 0),
                              "max_queue_no": c.get("max_queue_no") or 0}
    clinic = {"done": sum(c["done"] for c in counts.values()),
              "collected": sum(m["collected"] for m in money.values()),
              "refunded": sum(m["refunded"] for m in money.values())}
    return doctors, clinic


def recompute_day(clinic_id, alias, day):
    """One clinic-day in one transaction: lock the summary rows first, then aggregate, then write.

    Returns a list of differences found (and fixed). Expenses are left alone until M3 adds them.
    """
    differences = []
    unlocked = []
    with clinic_transaction(clinic_id, alias):
        clinic = _clinic(clinic_id)
        if clinic is None:
            return differences
        ctx = system_ctx(clinic, alias)
        locked = {r[0]: dict(zip(("last_queue_no", "booked", "done", "collected", "refunded"), r[1:])) for r in sql.rows(
            ctx, 'SELECT doctor_id, last_queue_no, booked, done, collected, refunded FROM "summaries_doctor_day" '
                 "WHERE clinic_id = %s AND day = %s ORDER BY doctor_id FOR UPDATE", [clinic_id, day])}
        current = sql.row(ctx, 'SELECT done, collected, refunded FROM "summaries_clinic_day" '
                               "WHERE clinic_id = %s AND day = %s FOR UPDATE", [clinic_id, day])
        if current is None:
            return differences
        doctors, totals = expected_summaries(ctx, day)
        updates = []
        for doctor_id in sorted(set(locked) | set(doctors)):
            want = doctors.get(doctor_id, {"booked": 0, "done": 0, "collected": 0, "refunded": 0, "max_queue_no": 0})
            have = locked.get(doctor_id)
            if have is None:
                # A row created after the lock statement: never lock it now (a flow may hold it while waiting
                # for this clinic-day). Leave the day flagged; the next run sees it.
                unlocked.append(doctor_id)
                continue
            fixed = {name: want[name] for name in ("booked", "done", "collected", "refunded")}
            fixed["last_queue_no"] = max(have["last_queue_no"], want["max_queue_no"])
            if fixed != have:
                differences.append({"doctor_id": doctor_id, "day": day.isoformat(), "had": have, "fixed": fixed})
                updates.append((doctor_id, fixed))
        if updates:
            values = ", ".join(["(%s::bigint, %s::int, %s::int, %s::int, %s::bigint, %s::bigint)"] * len(updates))
            params = [p for doctor_id, f in updates
                      for p in (doctor_id, f["last_queue_no"], f["booked"], f["done"], f["collected"], f["refunded"])]
            sql.run(ctx, f"""
                UPDATE "summaries_doctor_day" d SET last_queue_no = v.q, booked = v.b, done = v.n, collected = v.c,
                       refunded = v.r
                  FROM (VALUES {values}) AS v(doctor, q, b, n, c, r)
                 WHERE d.clinic_id = %s AND d.day = %s AND d.doctor_id = v.doctor""", [*params, clinic_id, day])
        have_totals = dict(zip(("done", "collected", "refunded"), current))
        if have_totals != totals:
            differences.append({"day": day.isoformat(), "had": have_totals, "fixed": totals})
        sql.run(ctx, 'UPDATE "summaries_clinic_day" SET done = %s, collected = %s, refunded = %s, '
                     "version = version + %s, changed_since_recompute = %s WHERE clinic_id = %s AND day = %s",
                [totals["done"], totals["collected"], totals["refunded"], 1 if differences else 0, bool(unlocked),
                 clinic_id, day])
    if unlocked:
        # Rows with source data but no summary row: create them empty in their own transaction (no other
        # lock held), so the next run, which finds the day still flagged, fills them in.
        with clinic_transaction(clinic_id, alias):
            ctx = system_ctx(_clinic(clinic_id), alias)
            for doctor_id in sorted(unlocked):
                sql.run(ctx, 'INSERT INTO "summaries_doctor_day" (clinic_id, day, doctor_id, last_queue_no, booked, done, '
                             "collected, refunded) VALUES (%s, %s, %s, 0, 0, 0, 0, 0) ON CONFLICT DO NOTHING",
                        [clinic_id, day, doctor_id])
        differences.append({"day": day.isoformat(), "missing_rows": sorted(unlocked)})
    for difference in differences:
        log.warning("recompute fixed a summary: clinic=%s %s", clinic_id, difference)
    return differences


def recompute_clinic(clinic_id, alias, today=None):
    found = []
    for day in days_to_recompute(clinic_id, alias, today):
        found += recompute_day(clinic_id, alias, day)
    return found


def run_recompute(today=None, clinic_ids=None):
    found = []
    for clinic_id, alias in directory_clinics():
        if clinic_ids and clinic_id not in clinic_ids:
            continue
        found += recompute_clinic(clinic_id, alias, today)
    return found
