"""Changing the day summaries (design 5.0, 5.3 "counters each action changes").

Every flow writes all its changes to one summary table in one statement, rows in key
order: doctor-days by (day, doctor_id), clinic-days by day. Doctor-days come before
clinic-days in the global lock order, and clinic-days (the hottest rows) are written
last, just before commit. Every clinic-day a flow touches gets version + 1 and
`changed_since_recompute`.
"""

from collections import defaultdict

from django.utils.translation import gettext as _

from core import sql
from core.errors import ApiError

DOCTOR_FIELDS = ("booked", "done", "collected", "refunded", "procedures", "no_shows", "timed", "wait_secs", "room_secs")
CLINIC_FIELDS = ("done", "collected", "refunded", "expenses")
MONEY_FIELDS = ("collected", "refunded")


def _day_full():
    return ApiError("day_full", _("This doctor's day is fully booked."), 409)


class Deltas:
    """One flow's summary changes, so that each table gets one statement."""

    def __init__(self):
        self.doctor = defaultdict(lambda: dict.fromkeys(DOCTOR_FIELDS, 0))
        self.clinic = defaultdict(lambda: dict.fromkeys(CLINIC_FIELDS, 0))
        self.money = defaultdict(lambda: dict.fromkeys(MONEY_FIELDS, 0))  # (day, section_id or 0, method)
        self.turn = None  # (day, doctor_id) that takes the next turn
        self.cap = None  # max_bookings of that doctor-day

    def doctor_day(self, day, doctor_id, **changes):
        """Changes to a doctor-day; with no changes, the row is only locked (slot moves within a doctor-day)."""
        row = self.doctor[(day, doctor_id)]
        for name, value in changes.items():
            row[name] += value
        return self

    def clinic_day(self, day, **changes):
        row = self.clinic[day]
        for name, value in changes.items():
            row[name] += value
        return self

    def money_day(self, day, section_id, method, **changes):
        """Money by payment day, section and method (F5). Written with the clinic-days, just before them."""
        row = self.money[(day, section_id or 0, method)]
        for name, value in changes.items():
            row[name] += value
        return self

    def timing(self, day, doctor_id, arrived_at, called_at, finished_at):
        """A finished visit's wait and doctor time (F8), when it has all three stamps; whole seconds, floored."""
        if arrived_at is None or called_at is None or finished_at is None:
            return self
        wait = max(0, int((called_at - arrived_at).total_seconds()))
        room = max(0, int((finished_at - called_at).total_seconds()))
        return self.doctor_day(day, doctor_id, timed=1, wait_secs=wait, room_secs=room)

    def take_turn(self, day, doctor_id, cap=None):
        """The next turn on this doctor-day and one more active booking (design 5.1 step 4)."""
        self.turn = (day, doctor_id)
        self.cap = cap
        return self.doctor_day(day, doctor_id, booked=1)

    def write_doctors(self, ctx):
        """One upsert for every doctor-day of the flow. Returns the new turn number, or None."""
        if not self.doctor:
            return None
        if self.turn is not None and self.cap is not None and self.cap <= 0:
            raise _day_full()
        keys = sorted(self.doctor)
        changes = {key: {"last_queue_no": 1 if key == self.turn else 0, **self.doctor[key]} for key in keys}
        where, where_params = "", []
        if self.turn is not None and self.cap is not None:
            # Only the row taking the turn is capped. A refused row stays locked, unchanged and unreturned.
            where, where_params = " WHERE (d.day, d.doctor_id) <> (%s, %s) OR d.booked < %s", [*self.turn, self.cap]
        returned = {(day, doctor_id): last for day, doctor_id, last in _upsert(
            ctx, "summaries_doctor_day", "d", ("day", "doctor_id"), ("last_queue_no", *DOCTOR_FIELDS), keys, changes,
            where=where, where_params=where_params, returning="day, doctor_id, last_queue_no")}
        if self.turn is None:
            return None
        if self.turn not in returned:
            raise _day_full()
        return returned[self.turn]

    def write_money(self, ctx):
        """One upsert for the flow's money-day rows, in key order (lock order 12a, design 5.0)."""
        if not self.money:
            return
        keys = sorted(self.money)
        changes = {key: self.money[key] for key in keys}
        self.money.clear()
        _upsert(ctx, "summaries_money_day", "m", ("day", "section_id", "method"), MONEY_FIELDS, keys, changes)

    def write_clinics(self, ctx):
        """The flow's money-day rows, then one upsert for every clinic-day of the flow. Returns {day: new version}."""
        self.write_money(ctx)
        if not self.clinic:
            return {}
        keys = sorted(self.clinic)
        changes = {(day,): {"version": 1, **self.clinic[day]} for day in keys}
        return dict(_upsert(ctx, "summaries_clinic_day", "c", ("day",), ("version", *CLINIC_FIELDS), [(d,) for d in keys],
                            changes, insert_extra={"changed_since_recompute": "true"},
                            set_extra="changed_since_recompute = true", returning="day, version"))

    def write(self, ctx):
        """Both tables, in the global order. Returns (turn or None, {day: version})."""
        turn = self.write_doctors(ctx)
        return turn, self.write_clinics(ctx)


def record_visits(ctx, visits):
    """Finished visits into the patients' stats rows (F6): one upsert, rows in patient order (lock order 11a).

    `visits` is [(patient_id, day)]. A visit more than REACTIVATION_DAYS after the patient's last one marks them
    reactivated on that day. SET reads the old row throughout, so the gap is measured from the old last day.
    """
    if not visits:
        return
    per_patient = {}
    for patient_id, day in visits:
        first, last, count = per_patient.get(patient_id, (day, day, 0))
        per_patient[patient_id] = (min(first, day), max(last, day), count + 1)
    rows, params = [], []
    for patient_id in sorted(per_patient):
        first, last, count = per_patient[patient_id]
        rows.append("(%s, %s, %s::date, %s::date, %s)")
        params += [ctx.clinic_id, patient_id, first, last, count]
    sql.run(ctx, f"""
        INSERT INTO "summaries_patient" AS s (clinic_id, patient_id, first_day, last_day, visits)
        VALUES {", ".join(rows)}
        ON CONFLICT (clinic_id, patient_id) DO UPDATE SET
            visits = s.visits + EXCLUDED.visits,
            first_day = LEAST(s.first_day, EXCLUDED.first_day),
            reactivated_day = CASE WHEN EXCLUDED.last_day - s.last_day > %s THEN EXCLUDED.last_day
                                   ELSE s.reactivated_day END,
            last_day = GREATEST(s.last_day, EXCLUDED.last_day)""", [*params, REACTIVATION_DAYS])


REACTIVATION_DAYS = 180  # a visit after this long away counts as coming back (F6)


def bump(ctx, *days):
    """Version + 1 on these days only (arrive, call, line, discount, void, patient edit). Returns {day: version}."""
    deltas = Deltas()
    for day in days:
        deltas.clinic_day(day)
    return deltas.write_clinics(ctx)


def _upsert(ctx, table, alias, key_columns, fields, keys, changes, *, insert_extra=None, set_extra="", where="",
            where_params=(), returning=""):
    """INSERT ... ON CONFLICT DO UPDATE adding signed deltas, rows in key order, in one statement.

    PostgreSQL checks CHECK constraints on the proposed row before it finds the conflict, so a
    negative delta can't travel in VALUES (the counters are >= 0). Inserted values are the deltas
    clamped at zero; the update adds the true deltas, through EXCLUDED when none is negative and
    through a CASE on the key otherwise. A decrement only ever meets an existing row, made by the
    booking it undoes; the recompute repairs anything else.
    """
    insert_extra = insert_extra or {}
    columns = ["clinic_id", *key_columns, *fields, *insert_extra]
    row_sql = "(" + ", ".join(["%s"] * (1 + len(key_columns) + len(fields)) + list(insert_extra.values())) + ")"
    params = []
    for key in keys:
        params += [ctx.clinic_id, *key, *(max(changes[key][name], 0) for name in fields)]
    sets, set_params = [], []
    for name in fields:
        if all(changes[key][name] >= 0 for key in keys):
            sets.append(f"{name} = {alias}.{name} + EXCLUDED.{name}")
            continue
        branches = []
        for key in keys:
            if changes[key][name]:
                match = " AND ".join(f"{alias}.{column} = %s" for column in key_columns)
                branches.append(f"WHEN {match} THEN %s")
                set_params += [*key, changes[key][name]]
        sets.append(f"{name} = {alias}.{name} + CASE {' '.join(branches)} ELSE 0 END")
    if set_extra:
        sets.append(set_extra)
    statement = (f'INSERT INTO "{table}" AS {alias} ({", ".join(columns)}) VALUES {", ".join([row_sql] * len(keys))} '
                 f"ON CONFLICT (clinic_id, {', '.join(key_columns)}) DO UPDATE SET {', '.join(sets)}{where}")
    if returning:
        statement += f" RETURNING {returning}"
    return sql.rows(ctx, statement, [*params, *set_params, *where_params])
