"""The price list by Saged's main services (2026-11-22).

- Every dental procedure sits under one of his main services; the rest of his list is there at starting prices.
- Book and Walk-in show the main services as chips; each price-list option names its main service.
- The doctor adds, changes and takes off procedures but sets no price without seeing money; the desk changes
  prices only; the owner both.
"""

import re

from core.testing import call

from clinical.testing import M2TestCase


class PriceListTests(M2TestCase):
    def test_every_procedure_has_its_main_service_and_the_new_ones_are_there(self):
        from clinics.catalogue.dental import FAMILY_OF

        items = self.catalogue()
        self.assertTrue(all(p["family"] for p in items), [p["name_en"] for p in items if not p["family"]])
        names = {p["name_en"]: p for p in items}
        for name in ("Clear aligners", "All-on-4, per arch", "Pulp capping", "Botox", "Cephalometric X-ray"):
            self.assertIn(name, names)
            self.assertEqual(names[name]["family"], FAMILY_OF[name])
        self.assertGreater(names["Clear aligners"]["price"], 0)  # a starting price, the clinic's to change

    def test_book_and_walk_in_show_the_main_services_first(self):
        html = self.desk.get("/today").content.decode()
        self.assertEqual(html.count("data-families"), 2)  # Book and Walk-in
        self.assertIn('data-family="restoration"', html)
        self.assertIn(">حشو</bdi>", html)
        filling = self.procedure("Composite filling, one surface")
        self.assertRegex(html, r'data-family="restoration">' + re.escape(filling["name_ar"]))
        self.assertNotIn('data-family="examination"', html)  # the visit fee covers an examination

    def test_the_doctor_keeps_the_procedures_and_the_desk_the_prices(self):
        r = call(self.doc, "POST", "/api/v1/procedures", {"name_ar": "تلبيسة مؤقتة خاصة", "family": "crown", "unit": "tooth"})
        self.assertEqual(r.status_code, 201, r.content)
        made = r.json()["procedure"]
        url = f"/api/v1/procedures/{made['id']}"
        self.assertEqual(call(self.doc, "PATCH", url, {"changes": {"name_ar": "تلبيسة مؤقتة"}}).status_code, 200)
        # The doctor prices their own services without seeing any other money (Saged, 2026-11-22).
        self.assertEqual(call(self.doc, "PATCH", url, {"changes": {"price": 50000}}).status_code, 200)
        self.assertEqual(call(self.doc, "POST", "/api/v1/procedures",
                              {"name_ar": "خدمة بسعر", "price": 10000}).status_code, 201)
        self.assertEqual(call(self.doc, "GET", "/api/v1/finances/month").status_code in (403, 404), True)
        self.assertEqual(call(self.desk, "PATCH", url, {"changes": {"price": 50000}}).status_code, 200)
        self.assertEqual(call(self.desk, "PATCH", url, {"changes": {"name_ar": "غيره"}}).status_code, 403)
        self.assertEqual(call(self.desk, "POST", "/api/v1/procedures", {"name_ar": "جديدة"}).status_code, 403)
        self.assertEqual(call(self.desk, "DELETE", url).status_code, 403)
        self.assertEqual(call(self.doc, "DELETE", url).status_code, 200)
        self.set_codes("doctor", revoked=["prices.edit"])  # the owner switches it off for this doctor
        self.assertEqual(call(self.doc, "PATCH", url, {"changes": {"price": 60000}}).status_code, 403)

    def test_the_page(self):
        desk = self.desk.get("/prices")
        self.assertEqual(desk.status_code, 200)
        html = desk.content.decode()
        self.assertIn("data-price ", html)
        self.assertNotIn("data-add-procedure", html)
        self.assertIn('href="/prices"', html)
        html = self.doc.get("/prices").content.decode()
        self.assertIn("data-add-procedure", html)
        self.assertIn("data-price ", html)  # the doctor's own prices
        self.assertNotIn('href="/finances"', html)  # and no other money
        self.assertIn("تاج / كوبري", html)
