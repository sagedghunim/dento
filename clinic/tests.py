import json
import threading
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import IntegrityError, connection, connections, transaction
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from clinic import services
from clinic.management.commands.seed_demo import default_permissions, ensure_specialties
from clinic.models import (Activity, Appointment, Clinic, Consultation, Expense, Medicine, Membership, Patient, Payment,
                           Prescription, Specialty, StockItem, StockMovement)


def make_clinic(prefix):
    ensure_specialties()
    clinic = Clinic.objects.create(name=f"{prefix} clinic", city="Cairo", specialty=Specialty.objects.get(name="Dental"))
    default_permissions(clinic)
    users = get_user_model().objects
    owner = Membership.objects.create(clinic=clinic, user=users.create_user(f"{prefix}_owner", password="x"), role="owner", display_name="Owner")
    asst = Membership.objects.create(clinic=clinic, user=users.create_user(f"{prefix}_asst", password="x"), role="assistant", display_name="Asst")
    assoc = Membership.objects.create(clinic=clinic, user=users.create_user(f"{prefix}_assoc", password="x"), role="associate", display_name="Assoc")
    item = StockItem.objects.create(clinic=clinic, name="Gloves", unit="boxes", quantity=5, threshold=2, unit_price=1000)
    return clinic, owner, asst, assoc, item


def client(member):
    c = Client()
    c.force_login(member.user)
    return c


def post(c, url, body=None, **headers):
    return c.post(url, json.dumps(body or {}), content_type="application/json", headers=headers)


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.clinic, self.owner, self.asst, self.assoc, self.item = make_clinic("a")
        self.c2, self.owner2, _, _, self.item2 = make_clinic("b")
        self.oc, self.ac = client(self.owner), client(self.asst)
        self.other = client(self.owner2)

    def appt(self, status="booked", fee=0, clinic=None):
        clinic = clinic or self.clinic
        return Appointment.objects.create(clinic=clinic, patient=services.patient_named(clinic.id, "P"), service="Filling",
                                          starts_at=timezone.now(), status=status, fee=fee)


class AuthTests(Base):
    def test_every_api_needs_login(self):
        for url in ["/api/me", "/api/today", "/api/inventory", "/api/finances", "/api/rx", "/api/consultations", "/api/team"]:
            self.assertEqual(Client().get(url).status_code, 401, url)

    def test_login_and_wrong_password(self):
        self.assertEqual(post(Client(), "/api/login", {"username": "a_owner", "password": "bad"}).status_code, 401)
        self.assertEqual(post(Client(), "/api/login", {"username": "a_owner", "password": "x"}).status_code, 200)

    def test_login_is_rate_limited(self):
        codes = [post(Client(), "/api/login", {"username": "a_owner", "password": "bad"}).status_code for _ in range(9)]
        self.assertEqual(codes[-1], 429)
        self.assertEqual(post(Client(), "/api/login", {"username": "a_owner", "password": "x"}).status_code, 429)

    def test_csrf_enforced(self):
        c = Client(enforce_csrf_checks=True)
        c.force_login(self.owner.user)
        self.assertEqual(post(c, "/api/expenses", {"name": "x", "category": "Other", "amount": 1}).status_code, 403)
        self.assertFalse(Expense.objects.exists())


class PermissionTests(Base):
    def test_assistant_blocked_by_default(self):
        for url in ["/api/finances", "/api/rx", "/api/consultations", "/api/team"]:
            self.assertEqual(self.ac.get(url).status_code, 403, url)
        self.assertEqual(post(self.ac, "/api/expenses", {"name": "x", "category": "Other", "amount": 1}).status_code, 403)

    def test_owner_change_applies_at_once_and_is_audited(self):
        self.assertEqual(post(self.oc, "/api/team/permission", {"role": "assistant", "perm": "finances", "allowed": True}).status_code, 200)
        self.assertEqual(self.ac.get("/api/finances").status_code, 200)
        self.assertTrue(Activity.objects.filter(clinic=self.clinic, action="permission", sensitive=True).exists())

    def test_turning_off_blocks_writes(self):
        post(self.oc, "/api/team/permission", {"role": "assistant", "perm": "inventory", "allowed": False})
        r = post(self.ac, "/api/stock/use", {"item": self.item.id, "patient": "P", "quantity": 1})
        self.assertEqual(r.status_code, 403)

    def test_assistant_cannot_grant_self(self):
        r = post(self.ac, "/api/team/permission", {"role": "assistant", "perm": "finances", "allowed": True})
        self.assertEqual(r.status_code, 403)

    def test_only_owner_grants_team(self):
        post(self.oc, "/api/team/permission", {"role": "associate", "perm": "team", "allowed": False})
        post(self.oc, "/api/team/permission", {"role": "assistant", "perm": "team", "allowed": True})
        r = post(self.ac, "/api/team/permission", {"role": "associate", "perm": "team", "allowed": True})
        self.assertEqual(r.status_code, 403)


class IsolationTests(Base):
    def test_other_clinic_cannot_touch_records(self):
        a = self.appt()
        self.assertEqual(post(self.other, f"/api/appointments/{a.id}/checkin").status_code, 404)
        self.assertEqual(post(self.other, "/api/stock/use", {"item": self.item.id, "patient": "P", "quantity": 1}).status_code, 404)
        self.assertEqual(post(self.other, "/api/stock/purchase", {"item": self.item.id, "quantity": 1, "price": 1}).status_code, 404)
        con = Consultation.objects.create(clinic=self.clinic, patient=a.patient, reason="r", scheduled_at=timezone.now())
        self.assertEqual(post(self.other, f"/api/consultations/{con.id}/accept").status_code, 404)
        med = Medicine.objects.create(clinic=self.clinic, name="M", form="f", dose="d")
        r = post(self.other, "/api/prescriptions", {"patient": a.patient.id, "items": [{"medicine": med.id, "quantity": 1, "days": 1}]})
        self.assertEqual(r.status_code, 404)
        p2 = services.patient_named(self.c2.id, "Q")
        r = post(self.other, "/api/prescriptions", {"patient": p2.id, "items": [{"medicine": med.id, "quantity": 1, "days": 1}]})
        self.assertEqual(r.status_code, 404)
        a.refresh_from_db(); self.item.refresh_from_db()
        self.assertEqual((a.status, self.item.quantity), ("booked", 5))

    def test_lists_never_mix_clinics(self):
        self.appt()
        Expense.objects.create(clinic=self.clinic, name="SECRET", category="Rent", amount=1, spent_at=timezone.now())
        self.assertNotIn(b"SECRET", self.other.get("/api/finances").content)
        self.assertEqual(self.other.get("/api/today").json()["appointments"], [])
        self.assertEqual(self.other.get("/api/finances").json()["spent"], 0)


class StockAndMoneyTests(Base):
    def test_purchase_adds_stock_and_expense_once(self):
        r = post(self.oc, "/api/stock/purchase", {"item": self.item.id, "quantity": 3, "price": 4550})
        self.assertEqual(r.status_code, 200)
        self.item.refresh_from_db()
        self.assertEqual((self.item.quantity, self.item.unit_price), (8, 4550))
        self.assertEqual(Expense.objects.get(clinic=self.clinic).amount, 13650)

    def test_cannot_use_more_than_stock(self):
        r = post(self.ac, "/api/stock/use", {"item": self.item.id, "patient": "P", "quantity": 6})
        self.assertEqual(r.status_code, 409)
        self.assertFalse(StockMovement.objects.exists())

    def test_database_rejects_negative_stock(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            StockItem.objects.filter(pk=self.item.pk).update(quantity=-1)

    def test_failed_step_rolls_everything_back(self):
        with mock.patch("clinic.services.note", side_effect=RuntimeError):
            self.client.raise_request_exception = False
            c = client(self.owner); c.raise_request_exception = False
            self.assertEqual(post(c, "/api/stock/purchase", {"item": self.item.id, "quantity": 3, "price": 1}).status_code, 500)
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity, 5)
        self.assertFalse(Expense.objects.exists())

    def test_finish_visit_collects_fee_once(self):
        a = self.appt("room", fee=30000)
        self.assertEqual(post(self.oc, f"/api/appointments/{a.id}/finish").status_code, 200)
        self.assertEqual(post(self.oc, f"/api/appointments/{a.id}/finish").status_code, 409)
        self.assertEqual(Payment.objects.get().amount, 30000)
        self.assertEqual(self.oc.get("/api/finances").json()["collected"], 30000)

    def test_rejects_bad_input(self):
        for body in [{"item": self.item.id, "quantity": 0, "price": 1}, {"item": self.item.id, "quantity": "2", "price": 1},
                     {"item": self.item.id, "quantity": True, "price": 1}, {"item": self.item.id, "quantity": 1, "price": -1}]:
            self.assertEqual(post(self.oc, "/api/stock/purchase", body).status_code, 400, body)
        self.assertEqual(self.oc.post("/api/expenses", "not json", content_type="application/json").status_code, 400)


class FlowTests(Base):
    def test_appointment_rules(self):
        a = self.appt()
        self.assertEqual(post(self.ac, f"/api/appointments/{a.id}/finish").status_code, 409)
        for step in ["checkin", "callin", "finish"]:
            self.assertEqual(post(self.ac, f"/api/appointments/{a.id}/{step}").status_code, 200)
        self.assertEqual(post(self.ac, f"/api/appointments/{a.id}/cancel").status_code, 409)
        self.assertEqual(post(self.ac, f"/api/appointments/{a.id}/nonsense").status_code, 404)

    def test_booking_appears_today(self):
        r = post(self.ac, "/api/appointments", {"name": "😀 مريض", "service": "Filling", "time": "23:30", "source": "phone", "fee": 0})
        self.assertEqual(r.status_code, 200)
        rows = self.ac.get("/api/today").json()["appointments"]
        self.assertEqual((rows[0]["patient"], rows[0]["time"]), ("😀 مريض", "23:30"))

    def test_service_must_belong_to_specialty(self):
        r = post(self.ac, "/api/appointments", {"name": "X", "service": "Laser session", "time": "10:00", "source": "phone", "fee": 0})
        self.assertEqual(r.status_code, 400)

    def test_double_submit_with_same_key_acts_once(self):
        body = {"name": "X", "service": "Filling", "time": "10:00", "source": "phone", "fee": 0}
        a = post(self.ac, "/api/appointments", body, **{"Idempotency-Key": "k1"})
        b = post(self.ac, "/api/appointments", body, **{"Idempotency-Key": "k1"})
        self.assertEqual((a.status_code, b.status_code, a.json()), (200, 200, b.json()))
        self.assertEqual(Appointment.objects.count(), 1)

    def test_failed_request_can_be_retried_with_same_key(self):
        bad = post(self.ac, "/api/stock/use", {"item": self.item.id, "patient": "P", "quantity": 99}, **{"Idempotency-Key": "k2"})
        self.assertEqual(bad.status_code, 409)
        good = post(self.ac, "/api/stock/use", {"item": self.item.id, "patient": "P", "quantity": 1}, **{"Idempotency-Key": "k2"})
        self.assertEqual(good.status_code, 200)

    def test_prescription_keeps_a_copy(self):
        a = self.appt()
        med = Medicine.objects.create(clinic=self.clinic, name="Amox", form="Caps", dose="1 a day")
        r = post(self.oc, "/api/prescriptions", {"patient": a.patient.id, "items": [{"medicine": med.id, "quantity": 14, "days": 5}]})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Amox", r.json()["text"])
        med.name = "Changed"; med.save()
        self.assertEqual(Prescription.objects.get().items.get().medicine_name, "Amox")

    def test_consultation_flow(self):
        p = services.patient_named(self.clinic.id, "P")
        c = Consultation.objects.create(clinic=self.clinic, patient=p, reason="r", scheduled_at=timezone.now())
        self.assertEqual(post(self.oc, f"/api/consultations/{c.id}/end").status_code, 409)
        for step in ["accept", "start", "end"]:
            self.assertEqual(post(self.oc, f"/api/consultations/{c.id}/{step}").status_code, 200)


class ScaleTests(Base):
    def test_query_count_does_not_grow_with_rows(self):
        def count():
            with CaptureQueriesContext(connection) as q:
                self.assertEqual(self.oc.get("/api/today").status_code, 200)
            return len(q)
        self.appt()
        first = count()
        for _ in range(40):
            self.appt()
        self.assertEqual(count(), first)

    def test_lists_are_capped(self):
        Expense.objects.bulk_create([Expense(clinic=self.clinic, name=str(i), category="Other", amount=1, spent_at=timezone.now()) for i in range(150)])
        data = self.oc.get("/api/finances").json()
        self.assertEqual(len(data["expenses"]), 100)
        self.assertEqual(data["spent"], 150)


class ConcurrencyTests(TransactionTestCase):
    """Real threads on separate connections."""

    def run_all(self, jobs):
        results = []
        def work(fn):
            try:
                results.append(fn())
            finally:
                connections.close_all()
        threads = [threading.Thread(target=work, args=(j,)) for j in jobs]
        [t.start() for t in threads]; [t.join() for t in threads]
        return results

    def test_last_unit_goes_to_one_person_only(self):
        cache.clear()
        clinic, owner, asst, assoc, item = make_clinic("c")
        StockItem.objects.filter(pk=item.pk).update(quantity=3)
        clients = [client(owner) for _ in range(8)]
        def job(c):
            return lambda: post(c, "/api/stock/use", {"item": item.id, "patient": "P", "quantity": 1}).status_code
        codes = self.run_all([job(c) for c in clients])
        item.refresh_from_db()
        self.assertEqual(sorted(codes), [200] * 3 + [409] * 5)
        self.assertEqual(item.quantity, 0)
        self.assertEqual(StockMovement.objects.count(), 3)


@override_settings(DEBUG=False)
class HealthTests(TestCase):
    def test_health(self):
        self.assertEqual(self.client.get("/healthz").content, b"ok")
