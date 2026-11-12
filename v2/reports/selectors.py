"""Patient insights (F6) and performance reports (F8). Reads only, no locks.

Everything reads the summaries (`summaries_patient`, `summaries_doctor_day`) or a partial index that holds only
the rows a list is about (open plan items, packages with sessions left, invoices with something owed), never a
scan of visits.
"""

import datetime as dt

from core import sql

ACTIVE_DAYS = 180  # "active": seen in the last 6 months
NEVER_BACK_DAYS = 30  # one visit, and nothing for a month since
QUIET_DAYS = 90  # used to return (2+ visits), nothing for 3 months...
LOST_DAYS = 365  # ...but less than a year: after that they are simply gone
LISTS = ("never_came_back", "going_quiet", "unfinished_treatment", "unused_sessions", "outstanding_balance")
EXPORT_LIMIT = 5000


def insights(ctx, today):
    """The F6 counts in two statements: one over the place's patient-stats rows, one over the month's money."""
    month = today.replace(day=1)
    row = sql.row(ctx, """
        SELECT count(*) FILTER (WHERE last_day >= %s),
               count(*) FILTER (WHERE first_day >= %s),
               count(*) FILTER (WHERE visits >= 2 AND last_day < %s AND last_day >= %s),
               count(*) FILTER (WHERE reactivated_day >= %s),
               count(*) FILTER (WHERE visits >= 2), count(*),
               count(*) FILTER (WHERE last_day >= %s)
          FROM "summaries_patient" WHERE clinic_id = %s""", [
        today - dt.timedelta(days=ACTIVE_DAYS), month, today - dt.timedelta(days=QUIET_DAYS),
        today - dt.timedelta(days=LOST_DAYS), month, month, ctx.clinic_id])
    active, new, at_risk, reactivated, repeat, total, seen_this_month = row
    net = sql.row(ctx, 'SELECT coalesce(sum(collected) - sum(refunded), 0) FROM "summaries_clinic_day" '
                       "WHERE clinic_id = %s AND day BETWEEN %s AND %s", [ctx.clinic_id, month, today])[0]
    return {"active": active, "new_this_month": new, "at_risk": at_risk, "reactivated_this_month": reactivated,
            "repeat_rate_bp": (repeat * 10000 // total) if total else 0, "patients": total,
            "seen_this_month": seen_this_month, "revenue_this_month": int(net),
            "revenue_per_patient": int(net) // seen_this_month if seen_this_month else 0,
            "rules": {"active_days": ACTIVE_DAYS, "quiet_days": QUIET_DAYS, "never_back_days": NEVER_BACK_DAYS}}


def _ids(ctx, name, today, tag_id, after, size):
    """[(patient_id, extra)] for one list, by patient id after `after`, at most `size` rows."""
    tag_sql = ' AND EXISTS (SELECT 1 FROM "patients_patient_tag" pt WHERE pt.clinic_id = %s AND pt.tag_id = %s ' \
              "AND pt.patient_id = {col})"
    params_tag = [ctx.clinic_id, tag_id] if tag_id is not None else []
    if name in ("never_came_back", "going_quiet"):
        if name == "never_came_back":
            where, params = "visits = 1 AND last_day < %s", [today - dt.timedelta(days=NEVER_BACK_DAYS)]
        else:
            where, params = "visits >= 2 AND last_day < %s AND last_day >= %s", [
                today - dt.timedelta(days=QUIET_DAYS), today - dt.timedelta(days=LOST_DAYS)]
        return sql.rows(ctx, f"""
            SELECT patient_id, NULL FROM "summaries_patient" s WHERE clinic_id = %s AND {where} AND patient_id > %s
            {tag_sql.format(col="s.patient_id") if tag_id is not None else ""}
             ORDER BY patient_id LIMIT %s""", [ctx.clinic_id, *params, after, *params_tag, size])
    if name == "unfinished_treatment":
        return sql.rows(ctx, f"""
            SELECT p.patient_id, count(*) FROM "clinical_plan_item" i
              JOIN "clinical_plan" p ON p.clinic_id = i.clinic_id AND p.id = i.plan_id
             WHERE i.clinic_id = %s AND i.status IN ('planned', 'in_progress') AND p.patient_id > %s
             {tag_sql.format(col="p.patient_id") if tag_id is not None else ""}
             GROUP BY p.patient_id ORDER BY p.patient_id LIMIT %s""", [ctx.clinic_id, after, *params_tag, size])
    if name == "unused_sessions":
        return sql.rows(ctx, f"""
            SELECT k.patient_id, sum(k.sessions - k.used)::bigint FROM "billing_patient_package" k
             WHERE k.clinic_id = %s AND k.used < k.sessions AND k.voided_at IS NULL AND k.patient_id > %s
             {tag_sql.format(col="k.patient_id") if tag_id is not None else ""}
             GROUP BY k.patient_id ORDER BY k.patient_id LIMIT %s""", [ctx.clinic_id, after, *params_tag, size])
    return sql.rows(ctx, f"""
        SELECT i.patient_id, sum(i.total - i.paid)::bigint FROM "billing_invoice" i
         WHERE i.clinic_id = %s AND i.status = 'open' AND i.paid < i.total AND i.patient_id > %s
         {tag_sql.format(col="i.patient_id") if tag_id is not None else ""}
         GROUP BY i.patient_id ORDER BY i.patient_id LIMIT %s""", [ctx.clinic_id, after, *params_tag, size])


EXTRA = {"unfinished_treatment": "open_items", "unused_sessions": "sessions_left", "outstanding_balance": "owed"}


def patient_list(ctx, name, today, tag_id=None, after=0, size=20):
    """One F6 list page: [{patient, last_visit, visits, <extra>}], by patient id. Two statements."""
    found = _ids(ctx, name, today, tag_id, after or 0, size)
    if not found:
        return []
    details = {r[0]: r[1:] for r in sql.rows(ctx, """
        SELECT p.id, p.full_name, p.phone, p.file_no, s.last_day, s.visits FROM "patients_patient" p
          LEFT JOIN "summaries_patient" s ON s.clinic_id = p.clinic_id AND s.patient_id = p.id
         WHERE p.clinic_id = %s AND p.id = ANY(%s::bigint[])""", [ctx.clinic_id, [r[0] for r in found]])}
    items = []
    for patient_id, extra in found:
        full_name, phone, file_no, last_day, visits = details.get(patient_id, (None,) * 5)
        item = {"patient": {"id": patient_id, "name": full_name, "phone": phone, "file_no": file_no},
                "last_visit": last_day.isoformat() if last_day else None, "visits": visits or 0}
        if name in EXTRA:
            item[EXTRA[name]] = int(extra)
        items.append(item)
    return items


def performance(ctx, first, last, by):
    """Clinic performance (F8) by day or month, from the doctor-days: visits, no-shows, average wait and doctor
    time (seconds, over the visits with all three stamps), procedures and money. One statement."""
    key = "day" if by == "day" else "date_trunc('month', day)::date"
    rows = sql.rows(ctx, f"""
        SELECT {key}, sum(done), sum(no_shows), sum(procedures), sum(timed), sum(wait_secs), sum(room_secs),
               sum(collected), sum(refunded)
          FROM "summaries_doctor_day" WHERE clinic_id = %s AND day BETWEEN %s AND %s GROUP BY 1 ORDER BY 1""",
                    [ctx.clinic_id, first, last])
    return [_performance_row(r[0].isoformat()[:7] if by == "month" else r[0].isoformat(), r[1:]) for r in rows]


def _performance_row(key, values):
    done, no_shows, procedures, timed, wait, room, collected, refunded = (int(v or 0) for v in values)
    return {"key": key, "visits": done, "no_shows": no_shows, "procedures": procedures, "timed": timed,
            "avg_wait_secs": wait // timed if timed else None, "avg_doctor_secs": room // timed if timed else None,
            "collected": collected, "refunded": refunded, "revenue": collected - refunded}


def staff_performance(ctx, first, last):
    """Per member (F8): visits, procedures, no-shows, revenue and average doctor time. One statement."""
    rows = sql.rows(ctx, """
        SELECT d.doctor_id, m.display_name, m.title, sum(d.done), sum(d.no_shows), sum(d.procedures), sum(d.timed),
               sum(d.wait_secs), sum(d.room_secs), sum(d.collected), sum(d.refunded)
          FROM "summaries_doctor_day" d JOIN "clinics_membership" m ON m.clinic_id = d.clinic_id AND m.id = d.doctor_id
         WHERE d.clinic_id = %s AND d.day BETWEEN %s AND %s GROUP BY d.doctor_id, m.display_name, m.title
         ORDER BY sum(d.collected) - sum(d.refunded) DESC, d.doctor_id""", [ctx.clinic_id, first, last])
    items = []
    for doctor_id, name, title, done, no_shows, procedures, timed, wait, room, collected, refunded in rows:
        row = _performance_row(None, (done, no_shows, procedures, timed, wait, room, collected, refunded))
        row.pop("key")
        items.append({"membership_id": doctor_id, "name": name, "title": title, **row})
    return items
