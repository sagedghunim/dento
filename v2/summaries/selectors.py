"""Reading the day summaries."""

from core import sql

TOTALS = ("version", "done", "collected", "refunded", "expenses")


def clinic_day(ctx, day):
    """The day's version and totals in one statement. Today reads it before its data (design 5.11)."""
    found = sql.row(ctx, 'SELECT version, done, collected, refunded, expenses FROM "summaries_clinic_day" '
                         "WHERE clinic_id = %s AND day = %s", [ctx.clinic_id, day])
    return dict(zip(TOTALS, found or (0, 0, 0, 0, 0)))


def doctor_days(ctx, day):
    return sql.dicts(ctx, "SELECT doctor_id, last_queue_no, booked, done, collected, refunded "
                          'FROM "summaries_doctor_day" WHERE clinic_id = %s AND day = %s ORDER BY doctor_id',
                     [ctx.clinic_id, day])
