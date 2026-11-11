"""Reading appointments: the shared row payload, Today (design 5.11), a patient's visits, slots and quotes.

`today_state` and `today_data` serve both the JSON endpoint and the HTML fragment
(`/today/queue`): read the state (the version) first, build the ETag, and build the data
only when the ETag doesn't match.
"""

import datetime as dt
import hashlib

from django.utils import timezone

from core import paging, sql
from core.days import business_day, local_iso, next_rollover
from summaries import selectors as summaries

from . import rules

TODAY_LIMIT = 1000
OPEN_STATUSES = ("booked", "waiting", "in_room", "done")

# The columns every appointment payload is built from (one query with joins inside the clinic).
ROW_SQL = """
    SELECT a.id, a.day, a.queue_no, a.status, a.starts_at, a.ends_at, a.is_extra, a.source, a.fee, a.is_follow_up,
           a.is_new, a.auto_closed, a.note, a.arrived_at, a.called_at, a.finished_at, a.cancelled_at, a.cancel_reason,
           a.doctor_id, a.section_id, a.patient_id, p.full_name AS patient_name, p.phone AS patient_phone,
           p.file_no AS patient_file_no, a.service_id, s.name AS service_name,
           i.id AS invoice_id, i.number AS invoice_number, i.total AS invoice_total, i.paid AS invoice_paid
      FROM "scheduling_appointment" a
      JOIN "patients_patient" p ON p.clinic_id = a.clinic_id AND p.id = a.patient_id
      JOIN "clinics_service" s ON s.clinic_id = a.clinic_id AND s.id = a.service_id
      LEFT JOIN "billing_invoice" i ON i.clinic_id = a.clinic_id AND i.appointment_id = a.id AND i.status = 'open'
"""


def _iso(clinic, instant):
    return local_iso(clinic, instant) if instant else None


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
        "called_at": _iso(clinic, r["called_at"]), "finished_at": _iso(clinic, r["finished_at"]),
        "cancelled_at": _iso(clinic, r["cancelled_at"]), "cancel_reason": r["cancel_reason"],
        "doctor_id": r["doctor_id"], "section_id": r["section_id"],
        "patient": {"id": r["patient_id"], "name": r["patient_name"], "phone": r["patient_phone"],
                    "file_no": r["patient_file_no"]},
        "service": {"id": r["service_id"], "name": r["service_name"]},
        "invoice": invoice, "due": due,
    }


def appointment(ctx, appointment_id):
    found = sql.dicts(ctx, ROW_SQL + " WHERE a.clinic_id = %s AND a.id = %s", [ctx.clinic_id, appointment_id])
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
    key = ",".join(sorted(ctx.codes)) + "|" + (language or "") + "|" + str(section_id or "")
    digest = hashlib.sha256(key.encode()).hexdigest()[:12]
    return f'"{ctx.clinic_id}.{day.isoformat()}.{version}.{digest}"'


def etag_matches(header, etag):
    if not header:
        return False
    candidates = [part.strip() for part in header.split(",")]
    return etag in candidates or f"W/{etag}" in candidates or "*" in candidates


def today_data(ctx, day, state, section_id=None):
    """Everything Today shows, in four statements whatever the clinic's size.

    The place's sections come with the doctors, in one statement. `section_id` filters the rows in
    memory (design D1: a large center has a few hundred rows a day); an unknown section is 404.
    Money totals and the doctor-day counts stay the whole place's.
    """
    clinic = ctx.clinic
    now = timezone.now()
    doctors, sections = {}, []
    for kind, row_id, name, starts, ends, mode, minutes, cap, session_section, sort, specialty in sql.rows(ctx, """
            SELECT 'doctor' AS row_kind, m.id, m.display_name, s.starts, s.ends, s.mode, s.slot_minutes, s.max_bookings,
                   s.section_id, 0 AS sort, NULL::varchar AS specialty_code
              FROM "clinics_membership" m
              LEFT JOIN "clinics_schedule" s ON s.clinic_id = m.clinic_id AND s.doctor_id = m.id AND s.weekday = %s
             WHERE m.clinic_id = %s AND m.is_active AND m.role IN ('owner', 'doctor')
            UNION ALL
            SELECT 'section', c.id, c.name, NULL, NULL, NULL, NULL, NULL, NULL, c.sort, c.specialty_code
              FROM "clinics_section" c
             WHERE c.clinic_id = %s AND c.is_active
             ORDER BY 1, 10, 2, 4""", [day.weekday(), ctx.clinic_id, ctx.clinic_id]):
        if kind == "section":
            sections.append({"id": row_id, "name": name, "specialty": specialty})
            continue
        doctor = doctors.setdefault(row_id, {"id": row_id, "name": name, "sessions": []})
        if starts is not None:
            doctor["sessions"].append({"starts": starts.strftime("%H:%M"), "ends": ends.strftime("%H:%M"),
                                       "mode": mode, "slot_minutes": minutes, "max_bookings": cap,
                                       "section_id": session_section})
    if section_id is not None:
        if section_id not in {s["id"] for s in sections}:
            raise rules.not_found("section")
        for doctor in doctors.values():
            doctor["sessions"] = [s for s in doctor["sessions"] if s["section_id"] == section_id]
    rows = sql.dicts(ctx, ROW_SQL + " WHERE a.clinic_id = %s AND a.day = %s ORDER BY a.doctor_id, a.queue_no LIMIT %s",
                     [ctx.clinic_id, day, TODAY_LIMIT])
    truncated = len(rows) >= TODAY_LIMIT
    if section_id is not None:
        rows = [r for r in rows if r["section_id"] == section_id]
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
        counts = {status: 0 for status in ("booked", "waiting", "in_room", "done", "cancelled", "no_show")}
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
        "queues": queues, "recent": _recent(appointments), "low_stock": [],  # items arrive in M3
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
        for field, action in (("arrived_at", "arrived"), ("called_at", "called"), ("finished_at", "finished"),
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
    params = [ctx.clinic_id, patient_id]
    where = " WHERE a.clinic_id = %s AND a.patient_id = %s"
    if after:
        where += " AND (a.day, a.id) < (%s, %s)"
        params += [after[0], after[1]]
    rows = sql.dicts(ctx, ROW_SQL + where + " ORDER BY a.day DESC, a.id DESC LIMIT %s", [*params, size + 1])
    items = [payload(ctx.clinic, r) for r in rows]
    return paging.page(items, size, lambda a: paging.encode_day(dt.date.fromisoformat(a["day"]), a["id"]))


def slots(ctx, doctor_id, day, section_id=None):
    """The doctor's slot grid that day (in one section, if given) with what is taken, and the day's limit."""
    if section_id is not None and not sql.row(ctx, 'SELECT 1 FROM "clinics_section" WHERE clinic_id = %s AND id = %s',
                                              [ctx.clinic_id, section_id]):
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
             WHERE clinic_id = %s AND doctor_id = %s AND day >= %s AND status IN ('booked', 'waiting', 'in_room')
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
    """For the recompute: per doctor, active bookings, visits done and the highest turn (no locks)."""
    return {r[0]: {"booked": r[1], "done": r[2], "max_queue_no": r[3]} for r in sql.rows(ctx, """
        SELECT doctor_id, count(*) FILTER (WHERE status <> 'cancelled'), count(*) FILTER (WHERE status = 'done'),
               max(queue_no)
          FROM "scheduling_appointment" WHERE clinic_id = %s AND day = %s GROUP BY doctor_id""", [ctx.clinic_id, day])}
