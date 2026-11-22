"""How many of a service a booking takes follows its price-list unit (Saged, 2026-11-22).

A filling counts teeth (up to 20), deep scaling counts quadrants (up to 4), a night guard counts jaws (up to 2); a
scaling and polishing is a whole case, booked once whatever count is sent. Book and Walk-in show the count with the
unit's own words, and no count for a case.
"""

import re

from core.testing import call, new_patient

from .test_price_list import PriceListCase


class BookingCountTests(PriceListCase):
    def service_of(self, name_en):
        return self.mirrors()[self.procedure(name_en)["id"]]

    def try_walk_in(self, service, qty):
        body = {"service_id": service.id, "doctor_id": self.a.membership("doctor").id, "new_patient": new_patient(),
                "qty": qty}
        return call(self.desk, "POST", "/api/v1/appointments/walk-in", body)

    def test_teeth_quadrants_and_jaws_are_counted_up_to_their_ceiling(self):
        for name, ceiling in (("Composite filling, two surfaces", 20),
                              ("Deep scaling and root planing, per quadrant", 4), ("Night guard", 2)):
            service = self.service_of(name)
            r = self.try_walk_in(service, ceiling)
            self.assertEqual(r.status_code, 201, (name, r.content))
            visit = r.json()["appointment"]
            self.assertEqual((visit["qty"], visit["fee"]), (ceiling, ceiling * service.price), name)
            r = self.try_walk_in(service, ceiling + 1)
            self.assertEqual(r.status_code, 400, name)
            self.assertEqual(r.json()["error"]["fields"], {"qty": "range"})

    def test_a_whole_case_is_one_whatever_is_sent(self):
        service = self.service_of("Scaling and polishing")
        r = self.try_walk_in(service, 3)
        self.assertEqual(r.status_code, 201, r.content)
        visit = r.json()["appointment"]
        self.assertEqual((visit["qty"], visit["fee"]), (1, service.price))

    def test_the_dialogs_carry_the_unit_and_its_words(self):
        html = self.desk.get("/today").content.decode()
        for name, unit in (("Composite filling, two surfaces", "tooth"), ("Night guard", "arch"),
                           ("Deep scaling and root planing, per quadrant", "quadrant"), ("Scaling and polishing", "")):
            self.assertRegex(html, rf'<option value="{self.service_of(name).id}"[^>]*data-unit="{unit}"', name)
        boxes = re.findall(r"<label[^>]*data-qty-box[^>]*>", html)
        self.assertEqual(len(boxes), 2)  # Book and Walk-in
        self.assertIn('data-tooth="كام سن؟"', boxes[0])
        self.assertNotIn("حشو", "".join(boxes))  # no more "for example 3 fillings"
