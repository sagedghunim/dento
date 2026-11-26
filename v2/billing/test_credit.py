"""Patient credit and old debt at booking (Saged, 2026-11-24).

- A payment with a booking (or a walk-in) pays the new visit's fee first, then the patient's older unpaid bills,
  oldest first.
- More than that is refused unless the desk says to keep it as the patient's credit ("keep_credit").
- Credit the patient holds is used on the next booking's payment: it moves between bills and never counts as money
  in the drawer, so the day's takings count each pound once.
"""

import datetime as dt
from zoneinfo import ZoneInfo

from core.routing import clinic_transaction
from core.testing import call
from scheduling.jobs import expected_summaries
from scheduling.testing import M1TestCase, frozen, summaries, today

CAIRO = ZoneInfo("Africa/Cairo")


class CreditTests(M1TestCase):
    def setUp(self):
        super().setUp()
        self.fee = self.services_a["consultation"].price
        self.day = today(self.a)

    def old_debt(self):
        """A patient with yesterday's visit billed and unpaid: (patient id, that invoice id)."""
        yesterday = self.day - dt.timedelta(days=1)
        with frozen(dt.datetime.combine(yesterday, dt.time(12), CAIRO)):
            visit = self.book(day=yesterday.isoformat()).json()["appointment"]
            r = call(self.desk, "POST", f"/api/v1/appointments/{visit['id']}/invoice", {})
            self.assertIn(r.status_code, (200, 201), r.content)
        return visit["patient"]["id"], r.json()["invoice"]["id"]

    def invoice(self, invoice_id):
        with clinic_transaction(self.a.id, self.a.shard):
            from billing.models import Invoice

            return Invoice.objects.get(clinic_id=self.a.id, id=invoice_id)

    def quote(self, patient_id):
        doctor = self.a.membership("owner").id
        service = self.services_a["consultation"].id
        return call(self.desk, "GET", f"/api/v1/appointments/quote?doctor={doctor}&service={service}"
                                      f"&day={self.day.isoformat()}&patient={patient_id}").json()

    def assert_summaries_match(self):
        """The running totals equal what the payment rows say (the recompute's formula)."""
        from clinics.models import Clinic
        from scheduling.jobs import system_ctx

        with clinic_transaction(self.a.id, self.a.shard):
            ctx = system_ctx(Clinic.objects.get(id=self.a.id), self.a.shard)
            doctors, clinic = expected_summaries(ctx, self.day)
        have_clinic, have_doctors = summaries(self.a, self.day)
        self.assertEqual((have_clinic["collected"], have_clinic["refunded"]), (clinic["collected"], clinic["refunded"]))
        for doctor_id, want in doctors.items():
            self.assertEqual(have_doctors[doctor_id]["collected"], want["collected"], doctor_id)

    def test_the_quote_tells_what_is_owed_and_held(self):
        patient, _ = self.old_debt()
        body = self.quote(patient)
        self.assertEqual((body["owed"], body["credit"]), (self.fee, 0))

    def test_a_booking_payment_pays_the_fee_then_the_old_bill(self):
        patient, old = self.old_debt()
        r = self.book(patient_id=patient, payment={"amount": self.fee * 2, "method": "cash"})
        appt = r.json()["appointment"]
        self.assertEqual((appt["invoice"]["paid"], appt["invoice"]["total"]), (self.fee, self.fee))
        self.assertEqual(self.invoice(old).paid, self.fee)
        clinic, _ = summaries(self.a, self.day)
        self.assertEqual(clinic["collected"], self.fee * 2)
        self.assert_summaries_match()
        self.assertEqual((self.quote(patient)["owed"], self.quote(patient)["credit"]), (0, 0))

    def test_part_of_the_old_bill_is_fine(self):
        patient, old = self.old_debt()
        self.book(patient_id=patient, payment={"amount": self.fee + 100, "method": "card"})
        self.assertEqual(self.invoice(old).paid, 100)

    def test_more_than_everything_owed_is_refused_unless_kept_as_credit(self):
        patient, old = self.old_debt()
        before = summaries(self.a, self.day)
        r = self.book(expect=409, patient_id=patient, payment={"amount": self.fee * 2 + 6000, "method": "cash"})
        error = r.json()["error"]
        self.assertEqual((error["code"], error["most"]), ("over_balance", self.fee * 2))
        self.assertEqual(summaries(self.a, self.day), before)
        self.assertEqual(self.invoice(old).paid, 0)

        r = self.book(patient_id=patient, payment={"amount": self.fee * 2 + 6000, "method": "cash", "keep_credit": True})
        appt = r.json()["appointment"]
        self.assertEqual(appt["invoice"]["paid"], self.fee + 6000)
        self.assertEqual(self.invoice(old).paid, self.fee)
        self.assertEqual(self.quote(patient)["credit"], 6000)
        self.assertEqual(summaries(self.a, self.day)[0]["collected"], self.fee * 2 + 6000)
        self.assert_summaries_match()

    def test_a_new_patient_may_leave_credit_too(self):
        r = self.book(payment={"amount": self.fee + 5000, "method": "cash", "keep_credit": True})
        self.assertEqual(r.json()["appointment"]["invoice"]["paid"], self.fee + 5000)
        self.assertEqual(self.book(expect=409, payment={"amount": self.fee + 1, "method": "cash"})
                         .json()["error"]["most"], self.fee)

    def test_credit_pays_the_next_booking_and_is_not_counted_as_money_again(self):
        first = self.book(payment={"amount": self.fee + 5000, "method": "cash", "keep_credit": True}).json()["appointment"]
        patient = first["patient"]["id"]
        self.assertEqual(self.act(first["id"], "cancel", "booked").status_code, 409)  # money is paid: refund first
        # The next booking (another doctor today) proposes the fee less the credit; the desk takes just that.
        doctor = self.a.membership("doctor").id
        r = self.book(patient_id=patient, doctor_id=doctor, starts_at=self.slot_iso(self.a, self.day, "10:20"),
                      confirm_other_doctor=True, payment={"amount": self.fee - 5000, "method": "cash"})
        appt = r.json()["appointment"]
        self.assertEqual((appt["invoice"]["paid"], appt["invoice"]["total"]), (self.fee, self.fee))
        self.assertEqual(self.invoice(first["invoice"]["id"]).paid, self.fee)  # the credit left the first bill
        self.assertEqual(self.quote(patient)["credit"], 0)
        clinic, doctors = summaries(self.a, self.day)
        self.assertEqual((clinic["collected"], clinic["refunded"]), (self.fee * 2, 0))  # cash in only
        self.assertEqual(doctors[doctor]["collected"], self.fee - 5000)
        self.assert_summaries_match()

    def test_credit_that_covers_the_whole_fee_needs_no_cash(self):
        first = self.book(payment={"amount": self.fee * 2, "method": "cash", "keep_credit": True}).json()["appointment"]
        doctor = self.a.membership("doctor").id
        r = self.book(patient_id=first["patient"]["id"], doctor_id=doctor, confirm_other_doctor=True,
                      starts_at=self.slot_iso(self.a, self.day, "10:20"), payment={"amount": 0, "method": "cash"})
        self.assertEqual(r.json()["appointment"]["invoice"]["paid"], self.fee)
        self.assertEqual(self.invoice(first["invoice"]["id"]).paid, self.fee)
        self.assert_summaries_match()
        # Nothing to pay and nothing held: an empty payment is still refused.
        self.book(expect=400, payment={"amount": 0, "method": "cash"})

    def test_a_bill_paid_from_credit_is_refunded_in_cash(self):
        first = self.book(payment={"amount": self.fee * 2, "method": "cash", "keep_credit": True}).json()["appointment"]
        doctor = self.a.membership("doctor").id
        second = self.book(patient_id=first["patient"]["id"], doctor_id=doctor, confirm_other_doctor=True,
                           starts_at=self.slot_iso(self.a, self.day, "10:20"),
                           payment={"amount": 0, "method": "cash"}).json()["appointment"]
        from billing.models import Payment

        with clinic_transaction(self.a.id, self.a.shard):
            moved = Payment.objects.get(clinic_id=self.a.id, invoice_id=second["invoice"]["id"], kind="payment")
        self.assertEqual(moved.method, "credit")
        r = call(self.owner, "POST", f"/api/v1/payments/{moved.id}/refund", {"amount": moved.amount})  # all, before going in
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["payment"]["method"], "cash")
        self.assert_summaries_match()

    def test_the_screens_show_the_credit_and_the_box(self):
        self.book(payment={"amount": self.fee + 5000, "method": "cash", "keep_credit": True})
        found = self.desk.get("/patients/search?pick=1&q=Mohamed",
                              headers={"X-Membership": str(self.desk.membership_id)}).content.decode()
        self.assertIn('data-credit="5000"', found)
        self.assertIn("رصيد:", found)
        today = self.desk.get("/today").content.decode()
        self.assertIn("data-money-line", today)
        self.assertIn('data-k="over_keep"', today)
