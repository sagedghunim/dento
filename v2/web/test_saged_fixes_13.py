"""Saged's fixes, part 13 (2026-11-23).

1. Done today: the procedure field comes first; a tap drops down the whole price list, a letter narrows it.
2. What comes next is a plan: none by default, "Make a plan", how many sessions, the gap between them, and what each
   one is (the same drop-down field). The days add up from this visit; the whole plan is saved in one request.
"""

from clinical.testing import M2TestCase
from core.testing import call


class DoneTodayFieldTests(M2TestCase):
    def test_the_procedure_field_comes_before_the_quick_chips_and_drops_down(self):
        html = self.doc.get(f"/visits/{self.in_room()['id']}").content.decode()
        card = html.split("data-procs-card")[1].split("</section>")[0]
        self.assertLess(card.index("data-proc-search"), card.index("data-top-procedures"))
        self.assertIn('role="combobox"', card)
        self.assertIn('role="listbox" data-proc-results hidden', card)


class PlanTests(M2TestCase):
    def test_no_plan_by_default_and_the_builder_offers_counts_and_gaps(self):
        html = self.doc.get(f"/visits/{self.in_room()['id']}").content.decode()
        card = html.split("data-next-card")[1].split("</section>")[0]
        self.assertIn("data-plan-start", card)
        self.assertIn("data-plan-form", card)
        for n in range(1, 7):
            self.assertIn(f'data-n="{n}"', card.split("data-plan-count")[1].split("</div>")[0])
        self.assertIn('data-n="3" data-unit="1"', card)  # 3 days
        self.assertIn('data-n="1" data-unit="30"', card)  # a month
        self.assertNotIn("data-next-when", card)

    def test_a_three_session_plan_is_saved_at_once_with_the_days_added_up(self):
        visit = self.in_room()
        scaling = self.procedure("Scaling and polishing")
        r = call(self.doc, "POST", f"/api/v1/appointments/{visit['id']}/follow-ups", {"items": [
            {"label": "زيارة متابعة", "days": 3},
            {"procedure_id": scaling["id"], "days": 10},
            {"label": "زيارة متابعة", "days": 40},
        ]})
        self.assertEqual(r.status_code, 201, r.content)
        upcoming = r.json()["upcoming"]
        self.assertEqual([i["days"] for i in upcoming], [3, 10, 40])
