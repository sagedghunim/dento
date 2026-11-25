"""Test stock (Saged, 2026-11-25): the ready items (inventory/catalog.py) put into one clinic's stock, shared by its
sections, with quantities spread over the cases the screens show: well above the minimum, near it, below it, and out.
The approximate store price is the unit cost. Names the clinic already has are left as they are, so a second run
adds nothing. Test data only: every item it adds carries DEMO_NOTE.

    python manage.py seed_stock_catalog "مركز سمايل لطب الأسنان"
"""

from django.core.management.base import BaseCommand, CommandError

from core.routing import clinic_transaction
from directory.models import DirectoryClinic

DEMO_NOTE = "بيانات تجربة - أصناف جاهزة"
THRESHOLD = 5
# (quantity, minimum) by case, in turn: plenty, near the minimum, below it, out.
CASES = ((40, THRESHOLD), (THRESHOLD + 1, THRESHOLD), (2, THRESHOLD), (0, THRESHOLD))


class Command(BaseCommand):
    help = "Put the ready dental items into one clinic's stock with test quantities (test data only)."

    def add_arguments(self, parser):
        parser.add_argument("clinic_name")

    def handle(self, *args, clinic_name, **options):
        from inventory.catalog import ITEMS
        from inventory.models import Item

        entry = DirectoryClinic.objects.filter(name=clinic_name).first()
        if entry is None:
            raise CommandError(f"No clinic named {clinic_name}")
        with clinic_transaction(entry.id, entry.shard):
            have = set(Item.objects.filter(clinic_id=entry.id, is_active=True).values_list("name", flat=True))
            added = []
            for n, (name, name_en, category, unit, price) in enumerate(ITEMS):
                if name in have:
                    continue
                quantity, threshold = CASES[n % len(CASES)]
                added.append(Item(clinic_id=entry.id, name=name, name_en=name_en, category=category[:40],
                                  unit=unit[:20], quantity=quantity, threshold=threshold,
                                  unit_cost=(price or 0) * 100, notes=DEMO_NOTE))
            Item.objects.bulk_create(added)
        self.stdout.write(f"Added {len(added)} items ({len(ITEMS) - len(added)} were already there).")
