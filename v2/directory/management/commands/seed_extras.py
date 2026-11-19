"""Demo data for the parts seed_activity leaves empty (demo places only, never real data; Saged, 2026-11-19).

Discount types with names a clinic really uses (the old test ones stop), discounts given on some open bills, a few
refunds, and installments for patients who owe (some late), so the alerts, the account and the installments all
show something. Everything goes through the services, so the change log and the summaries agree.

Run after seed_activity. Running it again adds the missing discount types only and skips the rest when done once.
"""

import datetime as dt
import random

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from billing import discounts
from billing import services as billing
from clinics.models import Clinic, Membership
from clinics.permissions import effective_codes
from core import sql
from core.context import Ctx
from core.days import business_day
from core.errors import ApiError
from core.routing import clinic_transaction
from directory.models import DirectoryClinic

from .seed_demo import DEMO

TYPES = (("خصم الأصحاب", "percent", 10), ("خصم العيلة", "percent", 15), ("خصم الطلبة", "percent", 20),
         ("كبار السن", "percent", 10), ("خصم الأطباء والتمريض", "percent", 25), ("عرض الافتتاح", "fixed", 100_00))
SEED = 20261119


class Command(BaseCommand):
    help = "Discount types, discounts, refunds and installments for the demo places (demo data only)."

    def add_arguments(self, parser):
        parser.add_argument("--place", default="", help="Part of one place's name (default: all demo places).")

    def handle(self, *args, **options):
        names = [spec["name"] for spec in DEMO if options["place"] in spec["name"]]
        for place in DirectoryClinic.objects.filter(name__in=names).order_by("id"):
            with clinic_transaction(place.id, place.shard):
                self.stdout.write(f"{place.name}: {self.fill(place)}")

    def fill(self, place):
        clinic = Clinic.objects.using(place.shard).get(id=place.id)
        owner = Membership.objects.using(place.shard).filter(clinic_id=clinic.id, role="owner").first()
        ctx = Ctx(clinic=clinic, membership=owner, user_id=owner.user_id, actor="member",
                  codes=effective_codes(owner.role, owner.granted, owner.revoked), mfa_verified=True,
                  request_id="seed_extras", alias=place.shard)
        rng = random.Random(SEED + clinic.id)
        out = []

        # 1. Discount types a clinic really gives; test leftovers (Latin-only names) stop being offered.
        current = discounts.types(ctx, include_inactive=True)
        have = {t["name"] for t in current}
        for t in current:
            if t["is_active"] and not any("؀" <= ch <= "ۿ" for ch in t["name"]):
                discounts.update(ctx, t["id"], {"is_active": False})
        added = 0
        for name, kind, value in TYPES:
            if name not in have:
                with transaction.atomic(using=place.shard):
                    discounts.create(ctx, name, kind, value, desk_can_apply=kind == "percent" and value <= 15)
                added += 1
        out.append(f"{added} discount types")
        if sql.row(ctx, 'SELECT 1 FROM "audit_change" WHERE clinic_id = %s AND request_id = %s '
                        "AND action IN ('invoice.discount', 'payment.refund') LIMIT 1", [clinic.id, "seed_extras"]):
            return ", ".join(out) + " (the rest was done before)"
        types = [t for t in discounts.types(ctx) if t["is_active"]]

        # 2. Discounts on some open bills that still have something to pay.
        open_bills = sql.rows(ctx, """
            SELECT id, discount FROM "billing_invoice" WHERE clinic_id = %s AND status = 'open' AND paid < total
               AND discount = 0 ORDER BY id DESC LIMIT 40""", [clinic.id])
        given = 0
        for invoice_id, discount in rng.sample(open_bills, min(8, len(open_bills))):
            try:
                with transaction.atomic(using=place.shard):
                    discounts.apply(ctx, invoice_id, rng.choice(types)["id"], discount)
                given += 1
            except ApiError:
                pass
        out.append(f"{given} discounts")

        # 3. A few partial refunds of recent payments.
        payments = sql.rows(ctx, """
            SELECT id, amount FROM "billing_payment" WHERE clinic_id = %s AND kind = 'payment' AND refunded = 0
               AND amount >= 20000 ORDER BY id DESC LIMIT 30""", [clinic.id])
        refunded = 0
        for payment_id, amount in rng.sample(payments, min(3, len(payments))):
            try:
                with transaction.atomic(using=place.shard):
                    billing.refund(ctx, payment_id, (amount // 4) // 100 * 100 or 100, note="رجوع جزء بعد إلغاء جلسة")
                refunded += 1
            except ApiError:
                pass
        out.append(f"{refunded} refunds")

        # 4. Installments for patients who owe the most; the first ones started weeks ago, so some parts are late.
        today = business_day(clinic)
        owing = sql.rows(ctx, """
            SELECT patient_id, sum(total - paid) FROM "billing_invoice" WHERE clinic_id = %s AND status = 'open'
               AND paid < total GROUP BY patient_id HAVING sum(total - paid) >= 60000 ORDER BY 2 DESC LIMIT 5""",
                         [clinic.id])
        plans = 0
        for n, (patient_id, owed) in enumerate(owing):
            owed = int(owed)
            if sql.row(ctx, 'SELECT 1 FROM "billing_installment_plan" WHERE clinic_id = %s AND patient_id = %s',
                       [clinic.id, patient_id]):
                continue
            started = today - dt.timedelta(days=[35, 21, 10, 3, 0][n % 5])
            down = owed * 30 // 100 // 100 * 100
            count = 3
            each = -(-(owed - down) // count)
            parts = [(started, down)] + [(started + dt.timedelta(days=30 * (i + 1)), min(each, owed - down - i * each))
                                         for i in range(count)]
            parts = [(d, a) for d, a in parts if a > 0]
            created = timezone.now() - dt.timedelta(days=(today - started).days)
            plan_id = sql.row(ctx, """
                INSERT INTO "billing_installment_plan" (clinic_id, patient_id, total, status, note, created_at, created_by_id)
                VALUES (%s, %s, %s, 'active', %s, %s, %s) RETURNING id""",
                              [clinic.id, patient_id, sum(a for _d, a in parts), "تقسيط التركيبات", created, owner.id])[0]
            for position, (day, amount) in enumerate(parts):
                sql.run(ctx, 'INSERT INTO "billing_installment" (clinic_id, plan_id, patient_id, position, due_on, amount) '
                             "VALUES (%s, %s, %s, %s, %s, %s)", [clinic.id, plan_id, patient_id, position, day, amount])
            plans += 1
        out.append(f"{plans} installment plans")
        return ", ".join(out)
