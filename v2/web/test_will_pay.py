"""A visit with no bill yet says "will pay", not "owes" (Instinct round 2, Saged 2026-11-25): the bill is made when
the visit is finished, so before that the patient owes nothing yet and the account's "remaining" is 0."""

from clinical.testing import M2TestCase


class WillPayTests(M2TestCase):
    def test_a_visit_not_finished_says_will_pay_and_a_finished_one_owes(self):
        visit = self.book(arrive=True).json()["appointment"]
        patient = visit["patient"]["id"]
        page = self.owner.get(f"/patients/{patient}").content.decode()
        rows = page.split('data-rows="visits"')[1].split("</ol>")[0]
        self.assertIn("data-will-pay", rows)
        self.assertIn("هيدفع", rows)
        self.assertNotIn(">باقي", rows)
        self.assertEqual(self.act(visit["id"], "call", "waiting", client=self.desk).status_code, 200)
        self.assertEqual(self.act(visit["id"], "finish", "in_room", client=self.desk).status_code, 200)
        rows = self.owner.get(f"/patients/{patient}").content.decode().split('data-rows="visits"')[1].split("</ol>")[0]
        self.assertNotIn("data-will-pay", rows)
        self.assertIn("باقي", rows)
