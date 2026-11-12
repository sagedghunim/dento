"""Reading appointments: the shared row payload, Today (design 5.11), a patient's visits, slots and quotes.

`today_state` and `today_data` serve both the JSON endpoint and the HTML fragment
(`/today/queue`): read the state (the version) first, build the ETag, and build the data
only when the ETag doesn't match.
"""

import datetime as dt
import hashlib

from django.utils import timezone

from core import paging, sql
from core.i18n import pick
from core.days import business_day, local_iso, next_rollover
from summaries import selectors as summaries

from . import rules

TODAY_LIMIT = 1000
OPEN_STATUSES = ("booked", "waiting", "with_assistant", "ready", "in_room", "done")
STATUS_KEYS = ("booked", "waiting", "with_assistant", "ready", "in_room", "done", "cancelled", "no_show")

# The columns every appointment payload is built from (one query with joins inside the clinic).
ROW_SQL = """
    SELECT a.id, a.day, a.queue_no, a.status, a.starts_at, a.ends_at, a.is_extra, a.source, a.fee, a.is_follow_up,
           a.is_new, a.auto_closed, a.note, a.arrived_at, a.with_assistant_at, a.ready_at, a.called_at, a.finished_at, a.cancelled_at, a.cancel_reason,
           a.doctor_id, a.section_id, a.patient_id, p.full_name AS patient_name, p.phone AS patient_phone,
           p.file_no AS patient_file_no, a.service_id, s.name AS service_name, s.name_en AS service_name_en,
           i.id AS invoice_id, i.number AS invoice_number, i.total AS invoice_total, i.paid AS invoice_paid
      FROM "scheduling_appointment" a
      JOIN "patients_patient" p ON p.clinic_id = a.clinic_id AND p.id = a.patient_id
      JOIN "clinics_service" s ON s.clinic_id = a.clinic_id AND s.id = a.service_id
      LEFT JOIN "billing_invoice" i ON i.clinic_id = a.clinic_id AND i.appointment_id = a.id AND i.status = 'open'
"""


def _iso(clinic, instant):
    return local_iso(clinic, instant) if instant else None


def timings(r):
    """Seconds spent (design E3): waiting (arrival to call-in), with the doctor (call-in to done) and in all.

    Only spans that have ended; a screen counts a running one from the times it has.
    """
    def span(start, end):
        return int((end - start).total_seconds()) if start and end else None

    return {"waiting_seconds": span(r["arrived_at"], r["called_at"]),
            "doctor_seconds": span(r["called_at"], r["finished_at"]),
            "total_seconds": span(r["arrived_at"], r["finished_at"])}


def payload(clinic, r):
    """One appointment as every endpoint answers it. `r` holds ROW_SQL's columns."""
    invoice = None
    if r["invoice_id"]:
        invoice = {"id": r["invoice_id"], "number": r["invoice_number"], "total": r["invoice_total"],
                   "paid": r["invoice_paid"]}
        due = r["invoice_total"] - r["invoice_paid"]
    else:
        due = r["fee"] if r["status"] in OPEN_STATUSES else 0
    return {
        "id": r["id"], "day": r["day"].isoformat(), "queue_no": r["queue_no"], "status": r["status"],
        "starts_at": _iso(clinic, r["starts_at"]), "ends_at": _iso(clinic, r["ends_at"]), "is_extra": r["is_extra"],
        "source": r["source"], "fee": r["fee"], "is_follow_up": r["is_follow_up"], "is_new": r["is_new"],
        "auto_closed": r["auto_closed"], "note": r["note"], "arrived_at": _iso(clinic, r["arrived_at"]),
        "with_assistant_at": _iso(clinic, r.get("with_assistant_at")), "ready_at": _iso(clinic, r.get("ready_at")),
        "called_at": _iso(clinic, r["called_at"]), "finished_at": _iso(clinic, r["finished_at"]),
        "timings": timings(r),
        "cancelled_at": _iso(clinic, r["cancelled_at"]), "cancel_reason": r["cancel_reason"],
        "doctor_id": r["doctor_id"], "section_id": r["section_id"],
        "patient": {"id": r["patient_id"], "name": r["patient_name"], "phone": r["patient_phone"],
                    "file_no": r["patient_file_no"]},
        "service": {"id": r["service_id"], "name": pick(r["service_name"], r.get("service_name_en", ""))},
        "invoice": invoice, "due": due,
    }


def appointment(ctx, appointment_id):
    """One visit; another doctor's is 404 to a doctor (Saged, 2026-11-13)."""
    own, own_params = rules.visit_sql(ctx, "a.doctor_id")
    found = sql.dicts(ctx, ROW_SQL + " WHERE a.clinic_id = %s AND a.id = %s" + own,
                      [ctx.clinic_id, appointment_id, *own_params])
    if not found:
        raise rules.not_found("appointment")
    return payload(ctx.clinic, found[0])


def answer(ctx, appointment_id, version=None):
    """The body of every appointment action: the row and the version of its day."""
    body = appointment(ctx, appointment_id)
    if version is None:
        version = summaries.clinic_day(ctx, dt.date.fromisoformat(body["day"]))["version"]
    return {"appointment": body, "version": version}


# --- Today -------------------------------------------------------------------------------------------------


def today_day(ctx, raw=None):
    if raw:
        from core.validate import iso_day

        return iso_day(raw)
    return business_day(ctx.clinic)


def today_state(ctx, day):
    """The day's version and totals: always read before the data (design 5.11)."""
    return summaries.clinic_day(ctx, day)


def today_section(raw):
    """The optional ?section= filter: a section id, or None for every section. Checked against the place in today_data."""
    if raw in (None, ""):
        return None
    from core.validate import ident

    return ident(raw, "section")


def today_etag(ctx, day, version, language, section_id=None):
    """"<clinic>.<day>.<version>.<hash of the person's codes, language and section filter>"."""
    # A limited assistant's sections and whose visits alone the member sees change the body too (design G1;
    # Saged, 2026-11-13: a doctor's own, and an owner's own unless they run the clinic alone).
    member = ctx.membership
    scope = (f"{member.id}:{member.role}:{sorted(member.sections)}:{rules.queue_doctor(ctx)}"
             if member is not None else "")
    key = ",".join(sorted(ctx.codes)) + "|" + (language or "") + "|" + str(section_id or "") + "|" + scope
    digest = hashlib.sha256(key.encode()).hexdigest()[:12]
    return f'"{ctx.clinic_id}.{day.isoformat()}.{version}.{digest}"'


def etag_matches(header, etag):
    if not header:
        return False
    candidates = [part.strip() for part in header.split(",")]
    return etag in candidates or f"W/{etag}" in candidates or "*" in candidates


LOW_STOCK_LIMIT = 20  # as inventory.services.low_stock


def today_data(ctx, day, state, section_id=None):
    """Everything Today shows, in four statements whatever the clinic's size.

    The place's sections and its low-stock items (F2, for those with inventory.use) come with the doctors, in
    one statement. `section_id` filters the rows in
    memory (design D1: a large center has a few hundred rows a day); an unknown section is 404.
    Money totals and the doctor-day counts stay the whole place's.
    """
    clinic = ctx.clinic
    now = timezone.now()
    doctors, sections, low_stock = {}, [], []
    for kind, row_id, name, starts, ends, mode, minutes, cap, session_section, sort, specialty, name_en in sql.rows(ctx, """
            SELECT 'doctor' AS row_kind, m.id, m.display_name, s.starts, s.ends, s.mode, s.slot_minutes, s.max_bookings,
                   s.section_id, 0 AS sort, NULL::varchar AS specialty_code, m.display_name_en
              FROM "clinics_membership" m
              LEFT JOIN "clinics_schedule" s ON s.clinic_id = m.clinic_id AND s.doctor_id = m.id AND s.weekday = %s
             WHERE m.clinic_id = %s AND m.is_active AND m.role IN ('owner', 'doctor')
            UNION ALL
            SELECT 'section', c.id, c.name, NULL, NULL, NULL, NULL, NULL, NULL, c.sort, c.specialty_code, c.name_en
              FROM "clinics_section" c
             WHERE c.clinic_id = %s AND c.is_active
            UNION ALL
            (SELECT 'low', i.id, i.name, NULL, NULL, i.unit, i.quantity, i.threshold, NULL, i.quantity - i.threshold, NULL,
                    i.name_en
               FROM "inventory_item" i
              WHERE i.clinic_id = %s AND %s AND i.is_active AND i.quantity <= i.threshold
              ORDER BY i.quantity - i.threshold, i.name LIMIT %s)
             ORDER BY 1, 10, 2, 4""", [day.weekday(), ctx.clinic_id, ctx.clinic_id, ctx.clinic_id,
                                       ctx.can("inventory.use"), LOW_STOCK_LIMIT]):
        name = pick(name, name_en)
        if kind == "low":
            low_stock.append({"id": row_id, "name": name, "unit": mode, "quantity": minutes, "threshold": cap})
            continue
        if kind == "section":
            sections.append({"id": row_id, "name": name, "specialty": specialty})
            continue
        doctor = doctors.setdefault(row_id, {"id": row_id, "name": name, "sessions": []})
        if starts is not None:
            doctor["sessions"].append({"starts": starts.strftime("%H:%M"), "ends": ends.strftime("%H:%M"),
                                       "mode": mode, "slot_minutes": minutes, "max_bookings": cap,
                                       "section_id": session_section})
    scope = rules.section_scope(ctx)
    if scope is not None:
        # A limited assistant sees only their sections' doctors' sessions and visits (design G1).
        sections = [s for s in sections if s["id"] in scope]
        for doctor in doctors.values():
            doctor["sessions"] = [s for s in doctor["sessions"] if s["section_id"] in scope]
    if section_id is not None:
        if section_id not in {s["id"] for s in sections}:
            raise rules.not_found("section")
        for doctor in doctors.values():
            doctor["sessions"] = [s for s in doctor["sessions"] if s["section_id"] == section_id]
    own_queue = rules.queue_doctor(ctx)
    if own_queue is not None:
        # A doctor, or an owner who treats in a shared place, sees only their own queue (Saged, 2026-11-13).
        doctors = {own_queue: doctors[own_queue]} if own_queue in doctors else {}
        own, own_params = " AND a.doctor_id = %s", [own_queue]  # index appointment_turn_uniq (clinic, day, doctor, ...)
    else:
        own, own_params = "", []
    rows = sql.dicts(ctx, ROW_SQL + " WHERE a.clinic_id = %s AND a.day = %s" + own
                     + " ORDER BY a.doctor_id, a.queue_no LIMIT %s", [ctx.clinic_id, day, *own_params, TODAY_LIMIT])
    truncated = len(rows) >= TODAY_LIMIT
    if section_id is not None:
        rows = [r for r in rows if r["section_id"] == section_id]
    elif scope is not None:
        rows = [r for r in rows if r["section_id"] in scope]
    # By slot time, then turn; never by status (design 7.4).
    rows.sort(key=lambda r: (r["doctor_id"], r["starts_at"] is None, r["starts_at"] or now, r["queue_no"]))
    appointments = [payload(clinic, r) for r in rows]
    day_rows = {r["doctor_id"]: r for r in summaries.doctor_days(ctx, day)}
    show_money = ctx.can("finances.view")

    queues = []
    for doctor_id in sorted({*(d for d, info in doctors.items() if info["sessions"]),
                             *(a["doctor_id"] for a in appointments)}):
        info = doctors.get(doctor_id, {"id": doctor_id, "name": "", "sessions": []})
        mine = [a for a in appointments if a["doctor_id"] == doctor_id]
        counts = {status: 0 for status in STATUS_KEYS}
        for a in mine:
            counts[a["status"]] += 1
        summary = day_rows.get(doctor_id, {})
        queue = {"doctor": info, "appointments": mine, "counts": counts,
                 "booked": summary.get("booked", 0), "last_queue_no": summary.get("last_queue_no", 0)}
        if show_money:
            queue["collected"] = summary.get("collected", 0)
            queue["refunded"] = summary.get("refunded", 0)
        queues.append(queue)

    body = {
        "day": day.isoformat(), "version": state["version"], "server_time": local_iso(clinic, now),
        "business_day": business_day(clinic, now).isoformat(), "next_rollover": local_iso(clinic, next_rollover(clinic, now)),
        "kind": clinic.kind, "sections": sections, "section_id": section_id,
        "queues": queues, "recent": _recent(appointments), "low_stock": low_stock,
        # A doctor's screens open on their own queue; own_only: no other doctor's visits are in it.
        "my_doctor_id": ctx.membership_id if ctx.membership.role in ("owner", "doctor") else None,
        "own_only": own_queue is not None,
        "truncated": truncated,
    }
    if show_money:
        body["totals"] = {"done": state["done"], "collected": state["collected"], "refunded": state["refunded"],
                          "net": state["collected"] - state["refunded"]}
    return body


def _recent(appointments, size=10):
    """The last changes, worked out from the fetched rows."""
    events = []
    for a in appointments:
        for field, action in (("arrived_at", "arrived"), ("with_assistant_at", "with_assistant"), ("ready_at", "ready"),
                              ("called_at", "called"), ("finished_at", "finished"),
                              ("cancelled_at", "cancelled")):
            if a[field]:
                events.append((a[field], action, a))
    events.sort(key=lambda e: e[0], reverse=True)
    return [{"at": at, "action": action, "appointment_id": a["id"], "queue_no": a["queue_no"],
             "doctor_id": a["doctor_id"], "section_id": a["section_id"], "patient_name": a["patient"]["name"]}
            for at, action, a in events[:size]]


# --- Other reads -------------------------------------------------------------------------------------------


def patient_visits(ctx, patient_id, size, after):
    """A patient's appointments, newest first (index (clinic_id, patient_id, day DESC, id DESC))."""
    own, own_params = rules.visit_sql(ctx, "a.doctor_id")  # a doctor sees their own visits only
    params = [ctx.clinic_id, patient_id, *own_params]
    where = " WHERE a.clinic_id = %s AND a.patient_id = %s" + own
    if after:
        where += " AND (a.day, a.id) < (%s, %s)"
        params += [after[0], after[1]]
    rows = sql.dicts(ctx, ROW_SQL + where + " ORDER BY a.day DESC, a.id DESC LIMIT %s", [*params, size + 1])
    items = [payload(ctx.clinic, r) for r in rows]
    return paging.page(items, size, lambda a: paging.encode_day(dt.date.fromisoformat(a["day"]), a["id"]))


def slots(ctx, doctor_id, day, section_id=None):
    """The doctor's slot grid that day (in one section, if given) with what is taken, and the day's limit."""
    scope = rules.section_scope(ctx)
    if section_id is not None and ((scope is not None and section_id not in scope) or not sql.row(
            ctx, 'SELECT 1 FROM "clinics_section" WHERE clinic_id = %s AND id = %s', [ctx.clinic_id, section_id])):
        raise rules.not_found("section")
    doctor = rules.doctor_day(ctx, doctor_id, day)
    taken = {r[0] for r in sql.rows(ctx, 'SELECT starts_at FROM "scheduling_appointment" '
                                         "WHERE clinic_id = %s AND day = %s AND doctor_id = %s AND holds_slot",
                                    [ctx.clinic_id, day, doctor_id])}
    clinic = ctx.clinic
    return {
        "doctor_id": doctor_id, "day": day.isoformat(), "section_id": section_id,
        "queue": doctor.has_queue(section_id), "max_bookings": doctor.cap,
        "slots": [{"starts_at": local_iso(clinic, start), "ends_at": local_iso(clinic, end), "taken": start in taken}
                  for start, end in rules.grid(clinic, day, doctor, section_id)],
    }


def quote(ctx, doctor_id, service_id, day, patient_id=None):
    """The fee and follow-up flag the booking would use (design 5.1)."""
    doctor, service = rules.doctor_day_and_service(ctx, doctor_id, service_id, day)
    eligible = False
    if patient_id is not None:
        found = sql.row(ctx, f'SELECT p.id, {rules.history_sql("p.id")} FROM "patients_patient" p '
                             "WHERE p.clinic_id = %s AND p.id = %s",
                        [*rules.history_params(ctx, doctor.doctor_id, day, rules.follow_up_window(ctx.clinic)),
                         ctx.clinic_id, patient_id])
        if found is None:
            raise rules.not_found("patient")
        eligible = found[1]
    fee, is_follow_up = rules.fee_for(service, eligible)
    return {"fee": fee, "is_follow_up": is_follow_up, "service_price": service.price,
            "follow_up_price": service.follow_up_price, "section_id": service.section_id,
            "doctor_works": bool(doctor.sessions_in(service.section_id))}


def future_bookings(ctx, doctor_id, weekdays, today, page=500):
    """Every open booking of a doctor from today on, on these weekdays (Monday is 0), for the schedule-change rule.

    Read in keyset pages on (day, queue_no), so no booking is ever left unchecked (chief judge M1, R4).
    """
    found, after = [], None
    while True:
        keyset, params = "", [ctx.clinic_id, doctor_id, today, list(weekdays)]
        if after is not None:
            keyset, params = " AND (day, queue_no) > (%s, %s)", [*params, *after]
        rows = sql.dicts(ctx, f"""
            SELECT id, day, queue_no, starts_at, ends_at, section_id FROM "scheduling_appointment"
             WHERE clinic_id = %s AND doctor_id = %s AND day >= %s AND status IN ('booked', 'waiting', 'with_assistant', 'ready', 'in_room')
               AND (EXTRACT(ISODOW FROM day)::int - 1) = ANY(%s){keyset}
             ORDER BY day, queue_no LIMIT %s""", [*params, page])
        found += rows
        if len(rows) < page:
            return found
        after = (rows[-1]["day"], rows[-1]["queue_no"])


def has_bookings(ctx):
    """Whether the place has any appointment at all (the time zone and day end are fixed from then on, design 4.2)."""
    return bool(sql.row(ctx, 'SELECT 1 FROM "scheduling_appointment" WHERE clinic_id = %s LIMIT 1', [ctx.clinic_id]))


def day_counts(ctx, day):
    """For the recompute: per doctor, active bookings, visits done, the highest turn, and the report counters
    (F8): no-shows, the timed visits' wait and doctor seconds, and the procedures recorded on the day's visits."""
    found = {r[0]: {"booked": r[1], "done": r[2], "max_queue_no": r[3], "no_shows": r[4], "timed": r[5],
                    "wait_secs": r[6], "room_secs": r[7], "procedures": 0} for r in sql.rows(ctx, """
        SELECT doctor_id, count(*) FILTER (WHERE status <> 'cancelled'), count(*) FILTER (WHERE status = 'done'),
               max(queue_no), count(*) FILTER (WHERE status = 'no_show'), count(*) FILTER (WHERE t),
               coalesce(sum(floor(GREATEST(0, extract(epoch FROM called_at - arrived_at)))) FILTER (WHERE t), 0)::bigint,
               coalesce(sum(floor(GREATEST(0, extract(epoch FROM finished_at - called_at)))) FILTER (WHERE t), 0)::bigint
          FROM (SELECT *, status = 'done' AND NOT auto_closed AND arrived_at IS NOT NULL AND called_at IS NOT NULL
                          AND finished_at IS NOT NULL AS t
                  FROM "scheduling_appointment" WHERE clinic_id = %s AND day = %s) a
         GROUP BY doctor_id""", [ctx.clinic_id, day])}
    for doctor_id, count in sql.rows(ctx, """
            SELECT a.doctor_id, count(*) FROM "clinical_visit_procedure" v
              JOIN "scheduling_appointment" a ON a.clinic_id = v.clinic_id AND a.id = v.appointment_id
             WHERE v.clinic_id = %s AND a.day = %s GROUP BY a.doctor_id""", [ctx.clinic_id, day]):
        found.setdefault(doctor_id, {"booked": 0, "done": 0, "max_queue_no": 0, "no_shows": 0, "timed": 0,
                                     "wait_secs": 0, "room_secs": 0})["procedures"] = count
    return found
