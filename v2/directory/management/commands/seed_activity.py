"""Fake activity in the demo places (DEBUG only). Never run on production data.

Fills each place of seed_demo.DEMO with the same recipe: about 60 patients, about 100 days of visits across the
place's doctors and sections (finished visits, no-shows, cancellations), procedures and treatment plans (the dental
chart in dental sections), payments by mixed methods with some balances left owed, a register shift per working
day, monthly expenses, pay rules and last months' staff pay, and today's queue in every stage.

Everything goes through the real services with the clock moved back (`timezone.now` patched), so invoices, shifts,
the day summaries, patient stats and the change log agree as if the staff had done it day by day. Each place is
one transaction on its shard; the summary recompute (design 5.10) then checks the past days it touched.

Deterministic (a fixed seed per place). A place that already has visits older than three weeks is skipped. There
is no --reset: undoing it would mean deleting across most tenant tables, which is neither cheap nor safe.
"""

import datetime as dt
import random
from unittest import mock

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from accounts.models import User
from billing import services as billing
from clinical import services as clinical
from clinical.services import MOLAR_ONLY, _is_molar
from clinics.models import Clinic, Membership, Schedule, Section, Service
from clinics.permissions import effective_codes
from core import sql
from core.context import Ctx
from core.days import business_day, zone
from core.routing import clinic_transaction
from directory.models import DirectoryClinic
from finance import services as finance
from patients import tags as patient_tags
from scheduling import jobs
from scheduling import services as scheduling
from scheduling.models import Appointment

from .seed_demo import DEMO

SEED = 20261112
PATIENTS_PER_DOCTOR = 170  # a busy Egyptian clinic: 10 to 20 visits a doctor an evening
MARKER_DAYS = 21  # a place with a visit this old already has activity
METHODS = (("cash", 55), ("card", 15), ("wallet", 15), ("instapay", 15))
SOURCES = (("clinic", 50), ("phone", 30), ("whatsapp", 15), ("online", 5))
TEETH = (16, 17, 26, 27, 36, 37, 46, 47, 14, 15, 24, 25, 34, 35, 44, 45, 11, 21, 12, 22)
ENGLISH = {"hamada": "Dr. Hamada Aziz", "ahmed": "Dr. Ahmed Samy", "sara": "Sara Mahmoud", "mona": "Dr. Mona Fathy",
           "karim": "Dr. Karim Youssef", "nour": "Dr. Nour Hassan", "omar": "Dr. Omar Khaled", "laila": "Laila Ibrahim"}
SECTION_ENGLISH = {"dental": "Dental", "pediatrics": "Pediatrics", "dermatology": "Dermatology"}
TAGS = (("مميز", "VIP", "#C8A400"), ("تأمين", "Insurance", "#2E7D32"), ("من فيسبوك", "Facebook", "#1565C0"))

MALE = ("محمد", "أحمد", "محمود", "مصطفى", "عمرو", "خالد", "يوسف", "عمر", "إبراهيم", "حسن", "طارق", "كريم", "هشام",
        "شريف", "وليد", "سامح", "أيمن", "ياسر", "تامر", "مينا", "زياد", "آدم", "مالك", "سيف")
FEMALE = ("فاطمة", "مريم", "نورا", "سلمى", "هبة", "منة", "آية", "ياسمين", "دينا", "رحاب", "شيماء", "إسراء", "نهى",
          "سارة", "مي", "رنا", "هالة", "جيهان", "مارينا", "أسماء", "جنى", "ليلى", "فريدة", "حبيبة")
FAMILY = ("عبد الله", "السيد", "مصطفى", "علي", "حسين", "عبد الرحمن", "فؤاد", "الشريف", "سليمان", "النجار", "عثمان",
          "رمضان", "زكي", "البنا", "جرجس", "منصور", "شاكر", "حمدي", "عادل", "سعيد", "الجمال", "توفيق")


class Clock:
    """What timezone.now() returns while the command runs."""

    def __init__(self):
        self.at = timezone.now()

    def __call__(self):
        return self.at


def weighted(rng, pairs):
    return rng.choices([p[0] for p in pairs], [p[1] for p in pairs])[0]


class Place:
    """One demo place: its rows, actors and the random source, inside its shard transaction."""

    def __init__(self, entry, rng, clock, history_days, out):
        self.entry, self.rng, self.clock, self.out = entry, rng, clock, out
        self.alias = entry.shard
        self.clinic = Clinic.objects.get(id=entry.id)
        self.tz = zone(self.clinic)
        self.today = business_day(self.clinic, clock.at)
        self.first_day = self.today - dt.timedelta(days=history_days)
        members = list(Membership.objects.filter(clinic_id=entry.id, is_active=True).order_by("id"))
        self.members = {m.id: m for m in members}
        self.ctx = {m.id: Ctx(clinic=self.clinic, membership=m, user_id=m.user_id, actor="member",
                              codes=effective_codes(m.role, m.granted, m.revoked), mfa_verified=True,
                              request_id="seed_activity", alias=self.alias) for m in members}
        owner = next(m for m in members if m.role == "owner")
        assistant = next((m for m in members if m.role == "assistant"), None)
        self.owner = self.ctx[owner.id]
        self.desk = self.ctx[(assistant or owner).id]
        self.sections = list(Section.objects.filter(clinic_id=entry.id, is_active=True).order_by("sort", "id"))
        self.consultation = {}
        for service in Service.objects.filter(clinic_id=entry.id, is_active=True, kind="consultation").order_by("sort", "id"):
            self.consultation.setdefault(service.section_id, service)
        self.procedures = {}
        for row in sql.dicts(self.owner, 'SELECT id, section_id, unit, condition, price, name_en FROM "clinical_procedure" '
                                         "WHERE clinic_id = %s AND is_active ORDER BY sort, id", [entry.id]):
            self.procedures.setdefault(row["section_id"], []).append(row)
        self.sessions = {}  # weekday -> [schedule]
        for s in Schedule.objects.filter(clinic_id=entry.id).order_by("starts", "id"):
            if s.doctor_id in self.members and s.section_id in self.consultation:
                self.sessions.setdefault(s.weekday, []).append(s)
        self.counts = dict.fromkeys(("patients", "done", "no_show", "cancelled", "procedures", "payments", "owed",
                                     "plans", "teeth", "expenses", "payouts", "shifts", "today"), 0)
        self.patients = []
        self.owed = {}  # patient -> [(appointment id, amount)]
        self.plans = {}  # patient -> [(plan item id, procedure id, tooth)] still planned

    # --- time ---

    def local(self, day, time, minutes=0):
        """A business day's local wall time as UTC; a time before the day end is after midnight."""
        date = day if time >= self.clinic.day_ends_at else day + dt.timedelta(days=1)
        return (dt.datetime.combine(date, time, tzinfo=self.tz) + dt.timedelta(minutes=minutes)).astimezone(dt.UTC)

    def session_bounds(self, day, s):
        start = self.local(day, s.starts)
        end = self.local(day, s.ends)
        if end <= start:
            end += dt.timedelta(days=1)
        return start, end

    def at(self, instant):
        self.clock.at = instant

    def doctor_ctx(self, doctor_id):
        return self.ctx[doctor_id]

    # --- the plan ---

    def make_patients(self):
        rng = self.rng
        dental = {s.id for s in self.sections if s.specialty_code == "dental"}
        kids = {s.id for s in self.sections if s.specialty_code == "pediatrics"}
        month_start = self.today.replace(day=1)
        span = (self.today - self.first_day).days
        doctors = {s.doctor_id for day in self.sessions.values() for s in day}
        count = PATIENTS_PER_DOCTOR * max(len(doctors), 1)
        for i in range(count):
            section = self.sections[i % len(self.sections)].id
            sex = rng.choice("mf")
            first = rng.choice(MALE if sex == "m" else FEMALE)
            father, family = rng.sample(MALE if rng.random() < 0.7 else FAMILY, 1)[0], rng.choice(FAMILY)
            age = rng.randint(1, 12) if section in kids else rng.randint(18, 68)
            fields = {"full_name": f"{first} {father} {family}", "sex": sex, "age": age,
                      "phone": rng.choice(("010", "011", "012", "015")) + f"{rng.randrange(10**8):08d}"}
            if age >= 40 and rng.random() < 0.25:
                fields["chronic_conditions"] = rng.choice(("ضغط", "سكر", "ضغط وسكر"))
            if rng.random() < 0.06:
                fields["allergies"] = "بنسلين"
            # Who they are in the insights (F6): once and gone, used to come, new this month, or regular.
            share = i / count
            if share < 0.15:
                profile, window, visits = "once", (span - 2, 35), 1
            elif share < 0.27:
                profile, window, visits = "quiet", (span, max(span - 14, 91)), rng.randint(2, 3)
            elif share < 0.42:
                profile, window, visits = "new", (min(20, (self.today - month_start).days + 7), 1), rng.randint(1, 3)
            else:
                profile, window, visits = "regular", (span, 1), rng.randint(4, 9)
            self.patients.append({"index": i, "fields": fields, "section": section, "profile": profile,
                                  "window": window, "visits": visits, "id": None, "dental": section in dental})

    def plan_visits(self):
        """{day: [(schedule, patient)]}: each patient's visits on working days of their section, in their window."""
        by_day = {}
        for patient in self.patients:
            far, near = patient["window"]
            options = []
            for back in range(max(near, 1), max(far, near) + 1):
                day = self.today - dt.timedelta(days=back)
                if day < self.first_day:
                    continue
                for s in self.sessions.get(day.weekday(), []):
                    if s.section_id == patient["section"]:
                        options.append((day, s))
            if not options:
                continue
            days = {}
            for day, s in self.rng.sample(options, min(len(options), patient["visits"] * 3)):
                if day not in days and len(days) < patient["visits"]:
                    days[day] = s
            for day, s in days.items():
                by_day.setdefault(day, []).append((s, patient))
        return by_day

    # --- one visit ---

    def book(self, day, s, patient, booked_at, starts_at=None):
        self.at(booked_at)
        request = {"day": day, "doctor_id": s.doctor_id, "service_id": self.consultation[s.section_id].id,
                   "starts_at": starts_at, "extra": False, "arrive": False, "payment": None, "note": "",
                   "source": weighted(self.rng, SOURCES), "walk_in": s.mode == "slots" and starts_at is None,
                   "patient_id": patient["id"], "new_patient": None if patient["id"] else dict(patient["fields"])}
        body, _version = scheduling.book(self.desk, request)
        if patient["id"] is None:
            patient["id"] = body["patient"]["id"]
            self.counts["patients"] += 1
        return body

    def procedures_for(self, patient, section_id, first_visit):
        """[(record request)] for this visit: plan items on the dental chart, or a section procedure."""
        rng, found = self.rng, []
        items = self.procedures.get(section_id, [])
        if patient["dental"]:
            planned = self.plans.get(patient["id"])
            if planned:
                item_id, procedure_id, tooth = planned.pop(0)
                found.append({"procedure_id": procedure_id, "tooth": tooth, "plan_item_id": item_id})
            elif not first_visit and rng.random() < 0.3:
                cheap = [p for p in items if p["unit"] in ("arch", "case", "session") and p["price"] <= 100000]
                if cheap:
                    found.append({"procedure_id": rng.choice(cheap)["id"], "tooth": None, "plan_item_id": None})
        elif items and rng.random() < 0.4:
            fitting = [p for p in items if p["unit"] != "tooth" and p["price"] <= 200000]
            if fitting:
                found.append({"procedure_id": rng.choice(fitting)["id"], "tooth": None, "plan_item_id": None})
        return found

    def examine(self, doctor, patient, appointment_id):
        """A dental patient's first visit: caries found on the chart and a treatment plan for them."""
        rng = self.rng
        by_condition = {}
        for p in self.procedures.get(patient["section"], []):
            if p["unit"] == "tooth" and p["condition"] and p["price"] <= 300000:
                by_condition.setdefault(p["condition"], []).append(p)
        teeth = rng.sample(TEETH, rng.randint(1, 3))
        items = []
        for tooth in teeth:
            clinical.set_tooth(doctor, patient["id"], tooth, "caries", rng.choice((["O"], ["M", "O"], ["D", "O"])),
                               "", appointment_id, expected=False)
            self.counts["teeth"] += 1
            condition = weighted(rng, (("filling", 60), ("root_canal", 25), ("crown", 10), ("extracted", 5)))
            choice = by_condition.get(condition) or by_condition.get("filling")
            # A molar procedure only on a molar (clinical.services._fits_the_tooth).
            choice = [p for p in choice or [] if _is_molar(tooth)
                      or not any(mark in (p.get("name_en") or "").lower() for mark in MOLAR_ONLY)]
            if choice:
                items.append({"procedure_id": rng.choice(choice)["id"], "tooth": tooth})
        if rng.random() < 0.3:
            clinical.set_tooth(doctor, patient["id"], rng.choice([t for t in TEETH if t not in teeth]), "missing", [], "",
                               appointment_id, expected=False)
            self.counts["teeth"] += 1
        if not items:
            return
        section = {"id": patient["section"]}
        plan_id = clinical.create_plan(doctor, patient["id"], section, "خطة العلاج", items)
        self.counts["plans"] += 1
        self.plans[patient["id"]] = [tuple(r) for r in sql.rows(
            doctor, 'SELECT id, procedure_id, tooth FROM "clinical_plan_item" WHERE clinic_id = %s AND plan_id = %s '
                    "ORDER BY position", [self.entry.id, plan_id])]

    def treat(self, appointment, patient, arrived, called, finished, first_visit):
        """Arrive, call in, record procedures, finish; then the desk takes the payment (or part of it)."""
        rng, desk, doctor = self.rng, self.desk, self.doctor_ctx(appointment["doctor_id"])
        appointment_id = appointment["id"]
        self.at(arrived)
        scheduling.change_status(desk, appointment_id, "arrive", "booked")
        self.at(called)
        scheduling.change_status(doctor, appointment_id, "call", "waiting")
        total = appointment["fee"]
        self.at(called + dt.timedelta(minutes=3))
        if patient["dental"] and first_visit and patient["profile"] == "regular":
            self.examine(doctor, patient, appointment_id)
        for request in self.procedures_for(patient, appointment["section_id"], first_visit):
            _record, invoice, _state, _version = clinical.record_procedure(doctor, appointment_id, {
                "surfaces": [], "area": "", "qty": 1, "unit_price": None, "note": "", **request})
            total = invoice["total"]
            self.counts["procedures"] += 1
        if finished is None:
            return total  # still in the room
        self.at(finished)
        scheduling.change_status(doctor, appointment_id, "finish", "in_room")
        self.counts["done"] += 1
        self.at(finished + dt.timedelta(minutes=2))
        older = self.owed.pop(patient["id"], [])
        if older and rng.random() < 0.6:
            for old_id, amount in older:
                billing.pay(desk, old_id, amount, weighted(rng, METHODS))
                self.counts["payments"] += 1
            older = []
        if total > 0:
            roll = rng.random()
            amount = total if roll < 0.85 else (total // 2 // 5000 * 5000 if roll < 0.97 else 0)
            if amount > 0:
                billing.pay(desk, appointment_id, amount, weighted(rng, METHODS))
                self.counts["payments"] += 1
            if amount < total:
                older.append((appointment_id, total - amount))
        if older:
            self.owed[patient["id"]] = older
        return total

    # --- a past day ---

    def past_day(self, day, visits):
        rng = self.rng
        sessions = self.sessions.get(day.weekday(), [])
        if not sessions:
            return
        bounds = [self.session_bounds(day, s) for s in sessions]
        opens = min(b[0] for b in bounds) - dt.timedelta(minutes=15)
        closes = max(b[1] for b in bounds) + dt.timedelta(minutes=20)
        self.at(opens)
        shift = finance.open_shift(self.desk, rng.choice((0, 20000, 50000)))
        self.counts["shifts"] += 1
        self.month_end_work(day, opens + dt.timedelta(minutes=5))

        for s, (start, end) in zip(sessions, bounds):
            mine = [p for sess, p in visits if sess.id == s.id]
            rng.shuffle(mine)
            slots = []
            if s.mode == "slots" and s.slot_minutes:
                step = dt.timedelta(minutes=s.slot_minutes)
                at = start
                while at + step <= end:
                    slots.append(at)
                    at += step
                picks = sorted(rng.sample(range(len(slots)), min(len(slots), len(mine))))
                slots = [slots[i] for i in picks]
            booked = []
            for n, patient in enumerate(mine):
                first_visit = patient["id"] is None
                lead = 0 if first_visit else rng.choice((0, 0, 1, 2, 3))
                booked_at = min(start - dt.timedelta(hours=rng.randint(1, 5)), start) - dt.timedelta(days=lead)
                starts_at = slots[n] if n < len(slots) else None
                appointment = self.book(day, s, patient, booked_at, starts_at)
                roll = rng.random()
                outcome = "done" if first_visit or patient["profile"] != "regular" or roll < 0.84 else (
                    "no_show" if roll < 0.92 else "cancelled")
                booked.append((appointment, patient, outcome, first_visit, booked_at))
            free = start + dt.timedelta(minutes=rng.randint(0, 15))
            length = max((end - start).total_seconds() / 60, 60)
            gap = length / max(len(booked), 1)
            for n, (appointment, patient, outcome, first_visit, booked_at) in enumerate(booked):
                if outcome == "cancelled":
                    self.at(booked_at + dt.timedelta(hours=rng.randint(1, 3)))
                    scheduling.change_status(self.desk, appointment["id"], "cancel", "booked", reason="اعتذر المريض")
                    self.counts["cancelled"] += 1
                    continue
                if outcome == "no_show":
                    self.counts["no_show"] += 1  # left booked: the end-of-day sweep closes it
                    continue
                if appointment["starts_at"]:
                    arrived = dt.datetime.fromisoformat(appointment["starts_at"]) - dt.timedelta(minutes=rng.randint(3, 15))
                else:
                    arrived = start + dt.timedelta(minutes=int(n * gap * 0.8) + rng.randint(-10, 10))
                called = max(arrived + dt.timedelta(minutes=rng.randint(4, 25)), free)
                finished = called + dt.timedelta(minutes=rng.randint(8, 30))
                free = finished + dt.timedelta(minutes=1)
                self.treat(appointment, patient, arrived, called, finished, first_visit)

        amount = rng.choice((3000, 5000, 7500, 10000))
        held = finance.expected_cash(shift["opening_float"], finance.shift_totals(self.desk, shift["id"]))
        if rng.random() < 0.25 and amount <= held:  # cash never beyond the drawer (2026-11-23)
            self.at(closes - dt.timedelta(minutes=30))
            finance.add_expense(self.desk, finance.category_by_code(self.desk, "other"), amount, day,
                                rng.choice(("مياه وبوفيه", "أدوات نظافة")), from_shift=True)
            self.counts["expenses"] += 1
        self.at(closes)
        totals = finance.shift_totals(self.desk, shift["id"])
        expected = finance.expected_cash(shift["opening_float"], totals)
        counted = expected - (rng.choice((500, 1000, 2000)) if rng.random() < 0.08 else 0)
        finance.close_shift(self.desk, shift["id"], max(counted, 0))
        # The end-of-day sweep, half an hour after the day ends: no-shows close (and count, F8).
        jobs.sweep_clinic(self.entry.id, self.alias, now=self.local(day + dt.timedelta(days=1), self.clinic.day_ends_at, 45))

    def month_end_work(self, day, at):
        """The owner's monthly money: rent and bills, supplies, and last month's staff pay."""
        rng, owner = self.rng, self.owner
        month = day.replace(day=1)
        if getattr(self, "_month", None) == month:
            return
        self._month = month
        self.at(at)
        dental = any(s.specialty_code == "dental" for s in self.sections)
        size = len(self.sections)
        # Cairo and Alexandria prices, 2026: rent 12,000 to 30,000 a month, bills 2,500 to 4,500.
        bills = [("rent", day, (1200000, 2000000, 3000000)[min(size, 3) - 1], "إيجار الشهر"),
                 ("utilities", min(day + dt.timedelta(days=9), self.today), rng.randint(25, 45) * 10000 * size,
                  "كهرباء ومياه وإنترنت")]
        if dental:
            bills.append(("materials", min(day + dt.timedelta(days=4), self.today), rng.randint(60, 90) * 10000,
                          "خامات حشو وتخدير"))
            bills.append(("lab", min(day + dt.timedelta(days=15), self.today), rng.randint(40, 80) * 10000,
                          "معمل تركيبات"))
        if size > 1 or not dental:
            bills.append(("materials", min(day + dt.timedelta(days=6), self.today), rng.randint(15, 30) * 10000,
                          "مستلزمات طبية"))
        for code, when, amount, note in bills:
            finance.add_expense(owner, finance.category_by_code(owner, code), amount, min(when, day), note,
                                from_shift=False)
            self.counts["expenses"] += 1
        previous = (month - dt.timedelta(days=1)).replace(day=1)
        if previous < self.first_day.replace(day=1):
            return
        for rule in finance.rules(owner):
            if not any(rule["rule"][k] for k in ("percent_bp", "per_visit", "per_procedure", "monthly_salary")):
                continue
            earned = finance.statement(owner, rule["id"], previous)["balance"]
            if earned > 0:
                method = "transfer"  # the drawer rarely holds a month's pay (cash never beyond it, 2026-11-23)
                finance.payout(owner, rule["id"], previous, earned, method, "")
                self.counts["payouts"] += 1

    # --- today ---

    def today_work(self, now):
        """Today's queue in every stage, at times between the morning and now."""
        rng = self.rng
        day = self.today
        sessions = self.sessions.get(day.weekday(), [])
        day_start = self.local(day, self.clinic.day_ends_at, 60)
        window_start = max(now - dt.timedelta(hours=4), day_start)
        if not sessions or now - window_start < dt.timedelta(minutes=30):
            return
        self.at(window_start - dt.timedelta(minutes=10))
        if finance.current_shift(self.desk) is None:
            finance.open_shift(self.desk, 20000)
        pool = [p for p in self.patients if p["profile"] in ("regular", "new") and p["id"] is not None]
        rng.shuffle(pool)
        stages = ["done"] * 7 + ["in_room", "ready", "waiting", "waiting", "waiting", "booked", "booked", "booked"]
        span = (now - window_start - dt.timedelta(minutes=5)).total_seconds()
        for s in sessions:
            mine = [p for p in pool if p["section"] == s.section_id][:len(stages)]
            for p in mine:
                pool.remove(p)
            start, end = self.session_bounds(day, s)
            slots = []
            if s.mode == "slots" and s.slot_minutes:
                at = start
                while at + dt.timedelta(minutes=s.slot_minutes) <= end:
                    slots.append(at)
                    at += dt.timedelta(minutes=s.slot_minutes)
                held = set(Appointment.objects.filter(clinic_id=self.entry.id, doctor_id=s.doctor_id, day=day,
                                                      holds_slot=True).values_list("starts_at", flat=True))
                slots = [at for at in slots if at not in held]
            booked = [self.book(day, s, p, window_start, slots[n] if n < len(slots) else None)
                      for n, p in enumerate(mine)]
            step = span / max(len(booked), 1)
            for n, (appointment, patient, stage) in enumerate(zip(booked, mine, stages)):
                self.counts["today"] += 1
                if stage == "booked":
                    continue
                arrived = window_start + dt.timedelta(seconds=n * step)
                if stage in ("waiting", "ready"):
                    self.at(arrived)
                    scheduling.change_status(self.desk, appointment["id"], "arrive", "booked")
                    if stage == "ready":
                        self.at(arrived + dt.timedelta(minutes=2))
                        scheduling.change_status(self.desk, appointment["id"], "ready", "waiting")
                    continue
                called = arrived + dt.timedelta(seconds=step * 0.3)
                finished = arrived + dt.timedelta(seconds=step * 0.9) if stage == "done" else None
                self.treat(appointment, patient, arrived, called, finished, False)

    # --- the rest ---

    def extras(self, users):
        """English names where empty, three patient tags, and pay rules for the team."""
        for m in self.members.values():
            english = ENGLISH.get(users.get(m.user_id, ""), "")
            if english and not m.display_name_en:
                Membership.objects.filter(clinic_id=self.entry.id, id=m.id, display_name_en="").update(
                    display_name_en=english)
        for section in self.sections:
            if not section.name_en and section.specialty_code in SECTION_ENGLISH:
                Section.objects.filter(clinic_id=self.entry.id, id=section.id, name_en="").update(
                    name_en=SECTION_ENGLISH[section.specialty_code])
        have = {t["name"]: t["id"] for t in patient_tags.tags(self.owner)}
        self.tag_ids = [have.get(name) or patient_tags.create_tag(self.owner, name, color, english)["id"]
                        for name, english, color in TAGS]
        rules = {r["id"]: r for r in finance.rules(self.owner)}
        for m in self.members.values():
            if m.role == "owner" or rules.get(m.id, {}).get("rule", {}).get("version"):
                continue
            # The usual deal: the assistant on a salary, a doctor on a share of what their visits bring in.
            values = ({"percent_bp": 0, "per_visit": 0, "per_procedure": 0, "monthly_salary": 650000}
                      if m.role == "assistant" else
                      {"percent_bp": 4000, "per_visit": 0, "per_procedure": 0, "monthly_salary": 0})
            finance.set_rule(self.owner, m.id, values, 0)

    def tag_patients(self):
        for patient in self.patients:
            if patient["id"] is None or self.rng.random() > 0.2:
                continue
            patient_tags.set_patient_tags(self.owner, patient["id"], [self.rng.choice(self.tag_ids)])

    def close_open_shift(self, now):
        """A shift left open from earlier use would take the past days' cash: close it, counted as expected."""
        self.at(now)
        shift = finance.current_shift(self.desk)
        if shift is not None:
            expected = finance.expected_cash(shift["opening_float"], finance.shift_totals(self.desk, shift["id"]))
            finance.close_shift(self.desk, shift["id"], max(expected, 0), "")
            self.out(f"  closed the open shift {shift['id']} (counted = expected) before adding history")

    def run(self, users):
        now = self.clock.at
        self.close_open_shift(now)
        self.at(self.local(self.first_day, dt.time(12)) - dt.timedelta(days=1))
        self.extras(users)
        self.make_patients()
        by_day = self.plan_visits()
        day = self.first_day
        while day < self.today:
            self.past_day(day, by_day.get(day, []))
            day += dt.timedelta(days=1)
        self.at(now)
        self.tag_patients()
        self.today_work(now)
        self.at(now)
        self.counts["owed"] = sum(len(v) for v in self.owed.values())


class Command(BaseCommand):
    help = ("Fill the demo places (seed_demo) with fake activity: patients, about 100 days of visits, payments, "
            "plans and dental charts, expenses, staff pay and today's queue. DEBUG only; skips places that already "
            "have older visits.")

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=105, help="Days of history before today (30 to 200).")

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("seed_activity only runs with DJANGO_DEBUG=1.")
        days = options["days"]
        if not 30 <= days <= 200:
            raise CommandError("--days must be between 30 and 200.")
        users = dict(User.objects.filter(username__in=list(ENGLISH)).values_list("id", "username"))
        clock = Clock()
        for index, spec in enumerate(DEMO):
            entry = DirectoryClinic.objects.filter(name=spec["name"]).first()
            if entry is None:
                self.stdout.write(f"{spec['name']}: not found (run seed_demo first).")
                continue
            clock.at = timezone.now()
            with mock.patch("django.utils.timezone.now", new=clock):
                with clinic_transaction(entry.id, entry.shard):
                    place = Place(entry, random.Random(SEED + index), clock, days, self.stdout.write)
                    marker = place.today - dt.timedelta(days=MARKER_DAYS)
                    if Appointment.objects.filter(clinic_id=entry.id, day__lte=marker).exists():
                        self.stdout.write(f"{spec['name']}: already has activity, skipped.")
                        continue
                    place.run(users)
            fixed = 0
            for _round in range(5):
                if not jobs.days_to_recompute(entry.id, entry.shard):
                    break
                fixed += len(jobs.recompute_clinic(entry.id, entry.shard))
            counts = ", ".join(f"{k} {v}" for k, v in place.counts.items())
            self.stdout.write(self.style.SUCCESS(f"{spec['name']}: {counts}; recompute differences {fixed}"))
