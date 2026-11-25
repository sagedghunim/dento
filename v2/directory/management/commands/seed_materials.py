"""Demo materials use for the team report's colours (Saged, 2026-11-25): stock items, each visit procedure's expected
materials this month, and per doctor one pattern: the busiest takes more than expected, the next takes less, the
rest use exactly what was expected. Every row it writes carries DEMO_REASON, and a second run changes nothing.

    python manage.py seed_materials "مركز سمايل لطب الأسنان"
"""

import datetime as dt

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.routing import clinic_transaction
from directory.models import DirectoryClinic

DEMO_REASON = "بيانات تجربة - خامات"
ITEMS = (  # name, unit, cost of one unit in piastres
    ("كومبوزيت A2 (سرنجة 4 جم)", "سرنجة", 85000),
    ("بوند يونيفرسال", "نقطة", 1500),
    ("أمبول تخدير أرتيكايين", "أمبول", 700),
    ("مبارد K يدوي", "مبرد", 1400),
    ("جوتا بيركا", "قمع", 150),
    ("جوانتي لاتكس", "زوج", 200),
)


class Command(BaseCommand):
    help = "Demo materials use for one clinic's team report (demo data only)."

    def add_arguments(self, parser):
        parser.add_argument("clinic_name")

    def handle(self, *args, clinic_name, **options):
        from inventory.models import Item, Movement
        from scheduling.models import Appointment

        entry = DirectoryClinic.objects.filter(name=clinic_name).first()
        if entry is None:
            raise CommandError(f"No clinic named {clinic_name}")
        with clinic_transaction(entry.id, entry.shard):
            from django.db import connections

            if Movement.objects.filter(clinic_id=entry.id, reason=DEMO_REASON).exists():
                self.stdout.write("Demo materials already there.")
                return
            items = []
            for name, unit, cost in ITEMS:
                item = Item.objects.filter(clinic_id=entry.id, name=name).first()
                if item is None:
                    item = Item.objects.create(clinic_id=entry.id, name=name, unit=unit, quantity=500, threshold=20,
                                               unit_cost=cost, category="خامات", notes=DEMO_REASON)
                items.append(item)
            composite, bond, ampoule, gloves = items[0], items[1], items[2], items[5]
            today = timezone.localdate()
            first = today.replace(day=1)
            cursor = connections[entry.shard].cursor()
            cursor.execute('SELECT vp.id, vp.appointment_id, a.doctor_id, vp.patient_id FROM "clinical_visit_procedure" vp '
                           'JOIN "scheduling_appointment" a ON a.clinic_id = vp.clinic_id AND a.id = vp.appointment_id '
                           "WHERE vp.clinic_id = %s AND a.day BETWEEN %s AND %s ORDER BY vp.id",
                           [entry.id, first - dt.timedelta(days=1), today])
            records = cursor.fetchall()
            by_doctor = {}
            for _id, _appointment, doctor, _patient in records:
                by_doctor[doctor] = by_doctor.get(doctor, 0) + 1
            order = sorted(by_doctor, key=lambda d: -by_doctor[d])
            pattern = {d: ("more" if i == 0 else "less" if i == 1 else "exact") for i, d in enumerate(order)}
            rows, seen_visits = [], set()

            def move(item, qty, appointment, patient, procedure=None, kind="use"):
                rows.append(Movement(clinic_id=entry.id, item_id=item.id, kind=kind, qty=qty, balance_after=item.quantity,
                                     unit_cost=item.unit_cost, appointment_id=appointment, patient_id=patient,
                                     visit_procedure_id=procedure, reason=DEMO_REASON))

            for record_id, appointment, doctor, patient in records:
                # Expected: what the procedure's recipe takes (a gram of composite in 4, two drops of bond, a cartridge, gloves).
                for item, qty in ((composite, 1), (bond, 2), (ampoule, 1), (gloves, 1)):
                    move(item, -qty, appointment, patient, record_id)
                if appointment in seen_visits:
                    continue
                seen_visits.add(appointment)
                if pattern[doctor] == "more":      # a second syringe opened on the visit
                    move(composite, -1, appointment, patient)
                elif pattern[doctor] == "less":    # an unused cartridge put back
                    move(ampoule, 1, appointment, patient, kind="return")
            Movement.objects.bulk_create(rows)
        names = ", ".join(f"{d}:{p}" for d, p in pattern.items())
        self.stdout.write(f"{len(rows)} demo movements for {len(records)} procedures ({names}).")
