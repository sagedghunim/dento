"""Test history (Saged, 2026-11-25): fills the history ("السجل") of one clinic's stock items with about two months of
movements, so the screen can be seen full: a first purchase, uses on the clinic's patients, a later purchase, waste,
and a stock count that lands on the quantity the item has now. Only items with no history yet get one, so a second
run adds nothing. Test data only: every movement it adds carries DEMO_REASON in its reason.

    python manage.py seed_stock_history "مركز سمايل لطب الأسنان"
"""

import random
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.routing import clinic_transaction
from directory.models import DirectoryClinic

DEMO_REASON = "بيانات تجربة"
DAYS = 60


def history(rng, quantity):
    """(kind, qty, reason, uses_patient) in order, ending on `quantity`. Balances never go below zero."""
    first = max(quantity, 0) + rng.randint(15, 30)
    moves = [("purchase", first, "", False)]
    balance = first
    for step in range(rng.randint(6, 10)):
        if step == 4 and balance < first // 2:
            more = rng.randint(10, 20)
            moves.append(("purchase", more, "", False))
            balance += more
            continue
        if step == 6 and balance > 2:
            moves.append(("waste", -1, f"{DEMO_REASON} - انتهت صلاحيته", False))
            balance -= 1
            continue
        take = min(rng.randint(1, 3), balance)
        if take:
            moves.append(("use", -take, DEMO_REASON, True))
            balance -= take
    if balance != quantity:
        moves.append(("adjust", quantity - balance, f"{DEMO_REASON} - جرد", False))
    return moves


class Command(BaseCommand):
    help = "Give one clinic's stock items a test history of movements (test data only)."

    def add_arguments(self, parser):
        parser.add_argument("clinic_name")

    def handle(self, *args, clinic_name, **options):
        from clinics.models import Membership
        from inventory.models import Item, Movement
        from patients.models import Patient

        entry = DirectoryClinic.objects.filter(name=clinic_name).first()
        if entry is None:
            raise CommandError(f"No clinic named {clinic_name}")
        now = timezone.now()
        with clinic_transaction(entry.id, entry.shard):
            people = list(Membership.objects.filter(clinic_id=entry.id).values_list("id", flat=True)[:5])
            patients = list(Patient.objects.filter(clinic_id=entry.id).values_list("id", flat=True)[:200])
            have = set(Movement.objects.filter(clinic_id=entry.id).values_list("item_id", flat=True).distinct())
            added, items = [], 0
            for item in Item.objects.filter(clinic_id=entry.id, is_active=True).order_by("id"):
                if item.id in have:
                    continue
                rng = random.Random(item.id)
                moves = history(rng, item.quantity)
                # Spread over the last DAYS days, oldest first, in clinic hours.
                days = sorted(rng.sample(range(1, DAYS), min(len(moves), DAYS - 1)), reverse=True)
                balance = 0
                for (kind, qty, reason, uses_patient), ago in zip(moves, days):
                    balance += qty
                    at = (now - timedelta(days=ago)).replace(hour=rng.randint(8, 18), minute=rng.choice((0, 15, 30, 45)))
                    if kind == "adjust":
                        at = now - timedelta(hours=rng.randint(2, 20))
                    added.append(Movement(
                        clinic_id=entry.id, item_id=item.id, kind=kind, qty=qty, balance_after=balance,
                        unit_cost=item.unit_cost, reason=reason,
                        patient_id=rng.choice(patients) if uses_patient and patients else None,
                        created_by_id=rng.choice(people) if people else None, at=at))
                items += 1
            Movement.objects.bulk_create(added)
        self.stdout.write(f"Added {len(added)} movements to {items} items ({len(have)} already had a history).")
