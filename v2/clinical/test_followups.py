"""Follow-ups (Saged, 2026-11-13): recipes on procedures, what comes next from a visit, the desk's list, and the
doctor's price lock."""

import datetime as dt

from clinical import followups
from clinical.models import PlanItem, Procedure
from clinical.testing import M2TestCase
from clinics.specialties import SPECIALTIES
from core.routing import clinic_transaction
from core.testing import call, new_patient
from scheduling.testing import today


class RecipeTests(M2TestCase):
    def test_the_catalogue_ships_the_standard_recipes(self):
        dental = {p["name_en"]: p["follow_up"] for p in SPECIALTIES["dental"]["procedures"]}
        self.assertEqual([s["days"] for s in dental["Root canal treatment, molar"]], [7, 14])  # three sessions, weekly
        self.assertEqual([s["days"] for s in dental["In-office laser whitening"]], [2, 7])
        self.assertEqual([s["days"] for s in dental["Monthly orthodontic adjustment"]], [30])
        self.assertEqual([s["days"] for s in dental["Implant fixture"]], [7, 90])
        self.assertEqual([s["days"] for s in dental["Simple extraction"]], [7])
        for code in ("pediatrics", "dermatology"):
            self.assertTrue(any(p["follow_up"] for p in SPECIALTIES[code]["procedures"]), code)
        for code, spec in SPECIALTIES.items():  # every recipe names a procedure that exists, with Arabic and English
            for p in spec["procedures"]:
                for step in p["follow_up"]:
                    self.assertTrue(step["label"] and step["label_en"] and step["days"] > 0, (code, p["name_en"]))

    def test_a_copied_section_has_the_recipes_and_the_clinic_edits_or_turns_them_off(self):
        root_canal = self.procedure("Root canal treatment, molar")
        self.assertEqual([s["days"] for s in root_canal["follow_up"]], [7, 14])
        owner = self.a.client("owner")
        url = f"/api/v1/procedures/{root_canal['id']}"
        r = call(owner, "PATCH", url, {"changes": {"follow_up": [{"days": 21, "label": "جلسة تانية"}, {"days": 10, "label": "أولى"}]}})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual([s["days"] for s in r.json()["procedure"]["follow_up"]], [10, 21])  # soonest first
        self.assertEqual(call(owner, "PATCH", url, {"changes": {"follow_up": []}}).json()["procedure"]["follow_up"], [])
        for bad in ([{"days": 0, "label": "x"}], [{"days": 7}], [{"days": 7, "label": "x"}] * 7, "weekly"):
            self.assertEqual(call(owner, "PATCH", url, {"changes": {"follow_up": bad}}).status_code, 400, bad)
        # The doctor keeps the follow-ups (Saged, 2026-11-19), the procedures and their prices (2026-11-22), never
        # the cost of materials.
        self.assertEqual(call(self.doc, "PATCH", url, {"changes": {"internal_cost": 1}}).status_code, 403)  # settings.manage
        self.assertEqual(call(self.desk, "PATCH", url, {"changes": {"follow_up": []}}).status_code, 403)

    def test_the_migration_fills_only_untouched_procedures(self):
        import importlib

        from django.db import connections

        migration = importlib.import_module("clinical.migrations.0006_follow_up_recipes")
        keep = [{"days": 3, "label": "خاص بالعيادة", "label_en": ""}]
        with clinic_transaction(self.a.id, self.a.shard):
            Procedure.objects.filter(clinic_id=self.a.id, name_en="Simple extraction").update(follow_up=[])
            Procedure.objects.filter(clinic_id=self.a.id, name_en="Implant fixture").update(follow_up=keep)

        class Editor:
            connection = connections[self.a.shard]

        migration.fill_recipes(None, Editor)
        with clinic_transaction(self.a.id, self.a.shard):
            found = dict(Procedure.objects.filter(clinic_id=self.a.id, name_en__in=["Simple extraction", "Implant fixture"])
                         .values_list("name_en", "follow_up"))
        self.assertEqual([s["days"] for s in found["Simple extraction"]], [7])
        self.assertEqual(found["Implant fixture"], keep)


class PriceLockTests(M2TestCase):
    def setUp(self):
        super().setUp()
        self.visit = self.in_room()
        self.url = f"/api/v1/appointments/{self.visit['id']}/procedures"
        self.scaling = self.procedure("Scaling and polishing")

    def test_a_doctor_records_at_the_list_price_only_whatever_was_granted(self):
        self.sees_money(also=["billing.discount"])
        body = {"procedure_id": self.scaling["id"], "unit_price": self.scaling["price"] - 50_00}
        r = call(self.doc, "POST", self.url, body)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "price_locked"), r.content)
        r = call(self.doc, "POST", self.url, {"procedure_id": self.scaling["id"], "unit_price": self.scaling["price"]})
        self.assertEqual(r.status_code, 201, r.content)  # the list price, named or not
        # Unnamed, on the patient's next visit (a procedure goes on a visit once, Saged 2026-11-17).
        self.assertEqual(self.act(self.visit["id"], "finish", "in_room").status_code, 200)
        later = self.in_room(self.visit["patient"]["id"])
        r = call(self.doc, "POST", f"/api/v1/appointments/{later['id']}/procedures", {"procedure_id": self.scaling["id"]})
        self.assertEqual((r.status_code, r.json()["procedure"]["unit_price"]), (201, self.scaling["price"]))

    def test_no_one_charges_another_price_the_owner_included(self):
        """No one edits prices (Saged, 2026-11-16): billing.discount gives discounts, never another price."""
        body = {"procedure_id": self.scaling["id"], "unit_price": self.scaling["price"] - 50_00}
        self.set_codes("assistant", granted=["procedures.record", "billing.discount"])
        for client in (self.desk, self.owner):
            r = call(client, "POST", self.url, body)
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "price_locked"), r.content)
        r = call(self.owner, "POST", self.url, {"procedure_id": self.scaling["id"]})
        self.assertEqual((r.status_code, r.json()["procedure"]["unit_price"]), (201, self.scaling["price"]))

    def test_a_tooth_procedure_is_recorded_in_one_tap_without_the_tooth(self):
        filling = self.procedure("Composite filling, two surfaces")
        r = call(self.doc, "POST", self.url, {"procedure_id": filling["id"]})
        self.assertEqual((r.status_code, r.json()["procedure"]["tooth"]), (201, None), r.content)


class FollowUpTests(M2TestCase):
    def setUp(self):
        super().setUp()
        self.visit = self.in_room()
        self.patient = self.visit["patient"]["id"]
        self.url = f"/api/v1/appointments/{self.visit['id']}/follow-ups"
        self.root_canal = self.procedure("Root canal treatment, molar")
        self.day = today(self.a)

    def items(self, fixture=None):
        fixture = fixture or self.a
        with clinic_transaction(fixture.id, fixture.shard):
            return list(PlanItem.objects.filter(clinic_id=fixture.id).order_by("id"))

    def test_a_recipe_adds_its_sessions_once_and_they_can_be_moved_or_dropped(self):
        key = "7d0c6f0e-3c58-4c4b-9d5e-2f6f1c1d7a11"
        r = call(self.doc, "POST", self.url, {"recipe_of": self.root_canal["id"], "tooth": 36}, key=key)
        self.assertEqual(r.status_code, 201, r.content)
        upcoming = r.json()["upcoming"]
        self.assertEqual([(u["name"], u["days"], u["tooth"]) for u in upcoming],
                         [("جلسة عصب تانية", 7, 36), ("جلسة عصب تالتة", 14, 36)])
        self.assertEqual(upcoming[0]["due_on"], (self.day + dt.timedelta(days=7)).isoformat())
        again = call(self.doc, "POST", self.url, {"recipe_of": self.root_canal["id"], "tooth": 36}, key=key)
        self.assertEqual(len(again.json()["upcoming"]), 2)  # the same key: nothing added twice
        self.assertEqual(len(self.items()), 2)
        first = upcoming[0]["id"]
        r = call(self.doc, "POST", f"/api/v1/follow-ups/{first}/due", {"days": 30})
        self.assertEqual([u["days"] for u in r.json()["upcoming"]], [14, 30])
        r = call(self.doc, "POST", f"/api/v1/follow-ups/{first}/drop", {})
        self.assertEqual([u["days"] for u in r.json()["upcoming"]], [14])
        self.assertEqual(call(self.doc, "POST", f"/api/v1/follow-ups/{first}/drop", {}).status_code, 200)  # a repeat
        self.assertEqual(call(self.doc, "POST", f"/api/v1/follow-ups/{first}/due", {"days": 7}).status_code, 409)

    def test_a_manual_follow_up_is_a_label_or_a_procedure_at_the_list_price(self):
        scaling = self.procedure("Scaling and polishing")
        r = call(self.doc, "POST", self.url, {"items": [{"label": "متابعة", "days": 2},
                                                         {"procedure_id": scaling["id"], "days": 30, "price": 1}]})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual([(u["name"], u["days"]) for u in r.json()["upcoming"]], [("متابعة", 2), (scaling["name"], 30)])
        self.assertEqual([i.price for i in self.items()], [0, scaling["price"]])  # never a price from the screen
        for bad in ({"items": []}, {"items": [{"days": 7}]}, {"items": [{"label": "x", "days": 0}]},
                    {"items": [{"label": "x", "days": 9999}]}, {"items": [{"procedure_id": 999999, "days": 7}]}):
            self.assertIn(call(self.doc, "POST", self.url, bad).status_code, (400, 404), bad)
        self.assertEqual(call(self.desk, "POST", self.url, {"items": [{"label": "x", "days": 7}]}).status_code, 403)

    def test_the_desk_sees_them_only_once_the_visit_is_finished(self):
        call(self.doc, "POST", self.url, {"recipe_of": self.root_canal["id"]})
        self.assertNotIn("Mohamed Ahmed", self.desk.get("/follow-ups").content.decode())
        self.assertEqual(self.act(self.visit["id"], "finish", "in_room", client=self.doc).status_code, 200)
        html = self.desk.get("/follow-ups").content.decode()
        self.assertIn("Mohamed Ahmed", html)
        self.assertIn("جلسة عصب تانية", html)
        phone = self.visit["patient"]["phone"]
        self.assertIn(f'href="tel:{phone}"', html)
        self.assertIn(f"https://wa.me/2{phone}", html)
        self.assertIn("data-fu-book", html)
        self.assertIn(f'data-doctor="{self.a.membership("doctor").id}"', html)

    def test_booking_links_the_follow_up_and_a_cancelled_booking_puts_it_back(self):
        ids = [u["id"] for u in call(self.doc, "POST", self.url, {"recipe_of": self.root_canal["id"]}).json()["upcoming"]]
        self.act(self.visit["id"], "finish", "in_room", client=self.doc)
        def rows():
            return self.desk.get("/follow-ups").content.decode().count('class="num fu-day"')

        self.assertEqual(rows(), 2)  # both sessions wait for a booking
        booked = self.book(patient_id=self.patient, doctor_id=self.a.membership("doctor").id,
                           day=(self.day + dt.timedelta(days=7)).isoformat(), expect=None)
        if booked.status_code != 201:  # the doctor is off that day in the fixture: any appointment of the patient's will do
            booked = self.book(patient_id=self.patient)
        appointment = booked.json()["appointment"]["id"]
        r = call(self.desk, "POST", "/api/v1/follow-ups/booked", {"item_ids": [ids[0]], "appointment_id": appointment})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(rows(), 1)
        self.assertEqual(call(self.desk, "POST", "/api/v1/follow-ups/booked",
                              {"item_ids": [ids[0]], "appointment_id": appointment}).status_code, 200)  # a repeat
        other = self.book(new_patient=new_patient(full_name="Someone Else", phone="01055555555")).json()["appointment"]["id"]
        r = call(self.desk, "POST", "/api/v1/follow-ups/booked", {"item_ids": [ids[1]], "appointment_id": other})
        self.assertEqual(r.status_code, 400, r.content)  # another patient's appointment
        self.assertEqual(self.act(appointment, "cancel", "booked").status_code, 200)
        self.assertEqual(rows(), 2)  # the cancelled booking's follow-up is back

    def test_later_hides_the_row_for_a_week(self):
        ids = [u["id"] for u in call(self.doc, "POST", self.url, {"items": [{"label": "متابعة", "days": 2}]}).json()["upcoming"]]
        self.act(self.visit["id"], "finish", "in_room", client=self.doc)
        self.assertIn("Mohamed Ahmed", self.desk.get("/follow-ups").content.decode())
        r = call(self.desk, "POST", "/api/v1/follow-ups/snooze", {"item_ids": ids})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertNotIn("Mohamed Ahmed", self.desk.get("/follow-ups").content.decode())
        self.assertEqual(self._to_book(self.day + dt.timedelta(days=6)), [])
        groups = self._to_book(self.day + dt.timedelta(days=7))  # a week on, it is back, and overdue
        self.assertEqual([g["key"] for g in groups], ["overdue"])

    def _to_book(self, day):
        from clinics.models import Clinic

        class DeskCtx:
            clinic_id, alias = self.a.id, self.a.shard
            membership = self.a.membership("assistant")
            membership_id = membership.id

        with clinic_transaction(self.a.id, self.a.shard):
            DeskCtx.clinic = Clinic.objects.get(id=self.a.id)
            return followups.to_book(DeskCtx, day)[0]

    def test_a_doctor_never_sees_or_touches_another_doctors_follow_ups(self):
        owner = self.a.client("owner")
        # The owner's own patient, with a follow-up from the owner's visit.
        theirs = self.book(new_patient=new_patient(full_name="Owner Patient", phone="01077777777")).json()["appointment"]
        self.assertEqual(self.act(theirs["id"], "arrive", "booked").status_code, 200)
        self.assertEqual(self.act(theirs["id"], "call", "waiting").status_code, 200)
        r = call(owner, "POST", f"/api/v1/appointments/{theirs['id']}/follow-ups", {"items": [{"label": "متابعة", "days": 7}]})
        self.assertEqual(r.status_code, 201, r.content)
        item = r.json()["upcoming"][0]["id"]
        self.assertEqual(self.act(theirs["id"], "finish", "in_room").status_code, 200)
        # The doctor's own, finished too.
        call(self.doc, "POST", self.url, {"items": [{"label": "متابعة", "days": 7}]})
        self.act(self.visit["id"], "finish", "in_room", client=self.doc)
        html = self.doc.get("/follow-ups").content.decode()
        self.assertIn("Mohamed Ahmed", html)
        self.assertNotIn("Owner Patient", html)
        desk = self.desk.get("/follow-ups").content.decode()
        self.assertIn("Owner Patient", desk)
        self.assertIn("Mohamed Ahmed", desk)
        self.assertIn("Owner Patient", owner.get("/follow-ups").content.decode())
        for url, body in ((f"/api/v1/appointments/{theirs['id']}/follow-ups", {"items": [{"label": "x", "days": 7}]}),
                          (f"/api/v1/follow-ups/{item}/due", {"days": 14}),
                          (f"/api/v1/follow-ups/{item}/drop", {}),
                          ("/api/v1/follow-ups/snooze", {"item_ids": [item]}),
                          ("/api/v1/follow-ups/booked", {"item_ids": [item], "appointment_id": theirs["id"]})):
            self.assertEqual(call(self.doc, "POST", url, body).status_code, 404, url)
        # Another place's follow-up is 404 as well.
        self.assertEqual(call(self.b.client("assistant"), "POST", "/api/v1/follow-ups/snooze", {"item_ids": [item]}).status_code, 404)
        self.assertEqual(self.items()[0].status, "planned")

    def test_the_page_needs_appointments_book_and_shows_in_the_side_bar(self):
        self.set_codes("doctor", revoked=["appointments.book"])
        self.assertEqual(self.doc.get("/follow-ups").status_code, 403)
        html = self.desk.get("/follow-ups").content.decode()
        self.assertIn('lang="ar" dir="rtl"', html)
        self.assertIn('href="/follow-ups" aria-current="page"', html)
        self.assertIn("المتابعات", html)
        self.assertIn("مفيش متابعات مستنية حجز.", html)


class VisitScreenPlanTests(M2TestCase):
    def test_the_screen_shows_the_bill_the_quick_procedures_and_what_comes_next(self):
        visit = self.in_room()
        scaling = self.procedure("Scaling and polishing")
        call(self.doc, "POST", f"/api/v1/appointments/{visit['id']}/procedures", {"procedure_id": scaling["id"]})
        call(self.doc, "POST", f"/api/v1/appointments/{visit['id']}/follow-ups", {"items": [{"label": "متابعة", "days": 7}]})
        html = self.doc.get(f"/visits/{visit['id']}").content.decode()
        self.assertIn("اللي اتعمل النهارده", html)
        self.assertIn("الجاي", html)
        self.assertIn("الشكوى، الفحص، اللي اتعمل، الجاي…", html)
        self.assertEqual(html.count("data-proc="), 6)  # six one-tap procedures
        self.assertIn(f'data-proc="{scaling["id"]}"', html)  # the one this doctor used comes first
        self.assertLess(html.index("data-proc-search"), html.index("data-top-procedures"))  # the field first (2026-11-23)
        self.assertIn('"upcoming": [{"id":', html)
        self.assertIn('"follow_up": [{"days": 7', html)
        self.assertNotIn('name="unit_price"', html)  # no price field for the doctor


class PhaseBTests(M2TestCase):
    """Phase B (Saged, 2026-11-13): Today's "just out of the visit" strip, a plan booked session by session, and the
    plan sent to the patient by WhatsApp."""

    def setUp(self):
        super().setUp()
        self.visit = self.in_room()
        self.patient = self.visit["patient"]["id"]
        self.day = today(self.a)
        self.doctor_id = self.a.membership("doctor").id
        root_canal = self.procedure("Root canal treatment, molar")
        r = call(self.doc, "POST", f"/api/v1/appointments/{self.visit['id']}/follow-ups", {"recipe_of": root_canal["id"]})
        self.ids = [u["id"] for u in r.json()["upcoming"]]

    def finish(self):
        self.assertEqual(self.act(self.visit["id"], "finish", "in_room", client=self.doc).status_code, 200)

    def queue(self, client=None, url="/today/queue", **extra):
        client = client or self.desk
        return client.get(url, HTTP_X_MEMBERSHIP=str(client.membership_id), **extra)  # as the page's script asks

    def strip(self, client=None, url="/today/queue"):
        html = self.queue(client, url).content.decode()
        return html.split("data-just-out", 1)[1].split("</section>", 1)[0] if "data-just-out" in html else ""

    def slot(self, hhmm, client=None, **body):
        return self.book(client=client, patient_id=self.patient, doctor_id=self.doctor_id,
                         starts_at=self.slot_iso(self.a, self.day, hhmm), **body)

    def count(self):
        from scheduling.models import Appointment

        with clinic_transaction(self.a.id, self.a.shard):
            return Appointment.objects.filter(clinic_id=self.a.id).count()

    def owner_visit(self):
        """The owner's own patient, with a follow-up from the owner's finished visit: (appointment, item id)."""
        owner = self.a.client("owner")
        theirs = self.book(new_patient=new_patient(full_name="Owner Patient", phone="01077777777")).json()["appointment"]
        self.assertEqual(self.act(theirs["id"], "arrive", "booked").status_code, 200)
        self.assertEqual(self.act(theirs["id"], "call", "waiting").status_code, 200)
        r = call(owner, "POST", f"/api/v1/appointments/{theirs['id']}/follow-ups", {"items": [{"label": "متابعة", "days": 7}]})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self.act(theirs["id"], "finish", "in_room").status_code, 200)
        return theirs, r.json()["upcoming"][0]["id"]

    # --- the strip ---

    def test_a_finished_visit_with_unbooked_follow_ups_shows_on_today_until_booked_or_put_off(self):
        self.assertEqual(self.strip(), "")  # still with the doctor
        before = self.queue()
        self.finish()
        for url in ("/today/queue", "/today"):
            strip = self.strip(url=url)
            self.assertIn("خارج من الكشف دلوقتي", strip)
            self.assertIn("Mohamed Ahmed", strip)
            self.assertIn("جلسة عصب تانية", strip)
            self.assertIn("جلسة عصب تالتة", strip)
            self.assertIn("data-fu-book", strip)
            self.assertIn("data-fu-later", strip)
            self.assertIn(f'data-item="{self.ids[0]}"', strip)
            self.assertIn(f'data-due="{(self.day + dt.timedelta(days=7)).isoformat()}"', strip)
        # Booking the first session leaves the second; the polling screens are told (the day's version moved).
        etag = self.queue()["ETag"]
        self.assertNotEqual(etag, before["ETag"])
        self.slot("10:00", follow_up_id=self.ids[0])
        strip = self.strip()
        self.assertNotIn("جلسة عصب تانية", strip)
        self.assertIn("جلسة عصب تالتة", strip)
        # "Later": off the strip, still in Follow-ups.
        etag = self.queue()["ETag"]
        r = call(self.desk, "POST", "/api/v1/follow-ups/snooze", {"item_ids": [self.ids[1]], "keep_listed": True})
        self.assertEqual(r.status_code, 200, r.content)
        r = self.queue(HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(r.status_code, 200)  # not a 304: the strip changed
        self.assertEqual(self.strip(), "")
        self.assertIn("جلسة عصب تالتة", self.desk.get("/follow-ups").content.decode())
        self.assertEqual(call(self.desk, "POST", "/api/v1/follow-ups/snooze",
                              {"item_ids": [self.ids[1]], "keep_listed": "yes"}).status_code, 400)

    def test_the_strip_is_only_todays_and_only_for_those_who_book_and_see_the_visit(self):
        self.finish()
        self.assertIn("Mohamed Ahmed", self.strip())
        self.assertIn("Mohamed Ahmed", self.strip(self.doc))  # the doctor's own
        tomorrow = (self.day + dt.timedelta(days=1)).isoformat()
        self.assertEqual(self.strip(url=f"/today/queue?day={tomorrow}"), "")
        self.assertEqual(self.strip(self.b.client("assistant")), "")  # another place
        self.owner_visit()
        self.assertIn("Owner Patient", self.strip())
        self.assertNotIn("Owner Patient", self.strip(self.doc))  # another doctor's finished visit
        # No booking permission: no strip (its actions would be refused).
        self.set_codes("doctor", revoked=["appointments.book"])
        self.assertEqual(self.strip(self.doc), "")

    def test_the_queue_poll_stays_a_fixed_number_of_statements(self):
        self.finish()
        one = self.queries(lambda: self.queue())
        self.owner_visit()
        self.assertIn("Owner Patient", self.strip())
        self.assertEqual(self.queries(lambda: self.queue()), one)

    # --- booking a plan's sessions ---

    def test_a_booking_links_its_follow_up_in_the_same_request_exactly_once(self):
        self.finish()
        had = self.count()
        key = "0f1b6c9e-7a55-4a44-8a43-0d4f7f1c2b10"
        body = {"patient_id": self.patient, "doctor_id": self.doctor_id, "service_id": self.services_a["consultation"].id,
                "starts_at": self.slot_iso(self.a, self.day, "10:00"), "follow_up_id": self.ids[0]}
        first = call(self.desk, "POST", "/api/v1/appointments", body, key=key)
        self.assertEqual(first.status_code, 201, first.content)
        again = call(self.desk, "POST", "/api/v1/appointments", body, key=key)  # the resend after a dropped connection
        self.assertEqual(again.json()["appointment"]["id"], first.json()["appointment"]["id"])
        self.assertEqual(self.count(), had + 1)
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertEqual(PlanItem.objects.get(clinic_id=self.a.id, id=self.ids[0]).appointment_id,
                             first.json()["appointment"]["id"])
        # A second person booking the same session (another key, another day): refused whole, no second appointment.
        tomorrow = self.day + dt.timedelta(days=1)
        r = self.book(patient_id=self.patient, doctor_id=self.doctor_id, day=tomorrow.isoformat(),
                      starts_at=self.slot_iso(self.a, tomorrow, "11:00"), follow_up_id=self.ids[0], expect=None)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "follow_up_booked"), r.content)
        self.assertEqual(self.count(), had + 1)
        # Once that booking is cancelled, the session can be booked again.
        self.assertEqual(self.act(first.json()["appointment"]["id"], "cancel", "booked").status_code, 200)
        self.assertEqual(self.slot("11:00", follow_up_id=self.ids[0], expect=None).status_code, 201)

    def test_a_session_booked_late_moves_the_later_ones_by_as_many_days(self):
        """Saged, 2026-11-24: the gaps the doctor set stay when a session is booked after its suggested day."""
        self.finish()

        def dues():
            with clinic_transaction(self.a.id, self.a.shard):
                return [PlanItem.objects.get(clinic_id=self.a.id, id=i).due_on for i in self.ids]

        first, second = dues()
        self.assertEqual(second - first, dt.timedelta(days=7))
        # Booked early (before its day): nothing moves.
        early = self.day + dt.timedelta(days=2)
        r = self.book(patient_id=self.patient, doctor_id=self.doctor_id, day=early.isoformat(),
                      starts_at=self.slot_iso(self.a, early, "10:00"), follow_up_id=self.ids[0], expect=None)
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(dues(), [first, second])
        self.assertEqual(self.act(r.json()["appointment"]["id"], "cancel", "booked").status_code, 200)
        # Booked three days late: the next session moves three days on, the gap stays a week.
        late = first + dt.timedelta(days=3)
        r = self.book(patient_id=self.patient, doctor_id=self.doctor_id, day=late.isoformat(),
                      starts_at=self.slot_iso(self.a, late, "10:00"), follow_up_id=self.ids[0], expect=None)
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(dues(), [first, second + dt.timedelta(days=3)])
        # The list says how late an overdue one is.
        self.assertEqual(self.act(r.json()["appointment"]["id"], "cancel", "booked").status_code, 200)
        groups = FollowUpTests._to_book(self, first + dt.timedelta(days=5))
        overdue = [g for g in groups if g["key"] == "overdue"][0]["rows"][0]["items"]
        self.assertEqual([i["late"] for i in overdue], [5])
        from django.utils import translation

        from web.templatetags.hamada import late_days

        with translation.override("ar"):
            self.assertEqual([late_days(n) for n in (0, 1, 2, 5, 11)],
                             ["", "متأخرة يوم", "متأخرة يومين", "متأخرة 5 أيام", "متأخرة 11 يوم"])

    def test_a_follow_up_is_booked_only_for_its_patient_by_someone_who_sees_it(self):
        self.finish()
        tomorrow = (self.day + dt.timedelta(days=1)).isoformat()  # today stays free for this patient's booking below
        other = self.book(day=tomorrow, new_patient=new_patient(full_name="Someone Else", phone="01055555555")
                          ).json()["appointment"]["patient"]["id"]
        had = self.count()
        r = self.book(patient_id=other, follow_up_id=self.ids[0], expect=None)
        self.assertEqual(r.status_code, 400, r.content)  # another patient's session
        for bad in ("x", 0, [1]):
            self.assertEqual(self.slot("10:00", follow_up_id=bad, expect=None).status_code, 400, bad)
        self.assertEqual(self.slot("10:00", follow_up_id=999999, expect=None).status_code, 404)
        # Another place's follow-up is 404 there; a dropped one is refused.
        b = self.b.client("assistant")
        self.assertEqual(self.book(client=b, fixture=self.b, follow_up_id=self.ids[0], expect=None).status_code, 404)
        call(self.doc, "POST", f"/api/v1/follow-ups/{self.ids[1]}/drop", {})
        self.assertEqual(self.slot("10:00", follow_up_id=self.ids[1], expect=None).status_code, 409)
        self.assertEqual(self.count(), had)  # nothing was booked by any refused request
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertIsNone(PlanItem.objects.get(clinic_id=self.a.id, id=self.ids[0]).appointment_id)

    def test_a_doctor_cannot_book_another_doctors_follow_up(self):
        _theirs, item = self.owner_visit()
        self.finish()  # the doctor's own patient is out of the room: no open visit today stands in the way
        r = self.slot("10:00", client=self.doc, follow_up_id=item, expect=None)
        self.assertEqual(r.status_code, 404, r.content)

    # --- the plan by WhatsApp ---

    def test_the_plan_opens_whatsapp_with_the_sessions_and_when(self):
        from urllib.parse import parse_qs, urlsplit

        self.finish()
        url = f"/follow-ups/whatsapp/{self.patient}"
        self.assertIn(f'href="{url}"', self.strip())
        self.assertIn(f'href="{url}"', self.desk.get("/follow-ups").content.decode())
        booked = self.slot("10:00", follow_up_id=self.ids[0]).json()["appointment"]
        r = self.desk.get(url)
        self.assertEqual(r.status_code, 302, r.content)
        target = urlsplit(r["Location"])
        self.assertEqual((target.scheme, target.netloc, target.path), ("https", "wa.me", f"/2{self.visit['patient']['phone']}"))
        text = parse_qs(target.query)["text"][0]
        self.assertIn("a clinic", text)
        lines = text.splitlines()
        first = next(line for line in lines if "جلسة عصب تانية" in line)
        second = next(line for line in lines if "جلسة عصب تالتة" in line)
        self.assertIn("10:00", first.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")))  # booked: its day and time
        self.assertIn("لسه من غير حجز", second)  # not booked: the suggested day only, no time
        self.assertNotRegex(second, r"[0-9٠-٩]:[0-9٠-٩]")
        self.assertEqual(r["Cache-Control"], "no-store")
        # The doctor's visit screen offers it too: the booked session, arrived and called in (its one visit today).
        self.assertEqual(self.act(booked["id"], "arrive", "booked").status_code, 200)
        self.assertEqual(self.act(booked["id"], "call", "waiting").status_code, 200)
        again = booked
        self.assertIn(f'href="{url}"', self.doc.get(f"/visits/{again['id']}").content.decode())

    def test_whatsapp_needs_a_phone_sessions_and_a_patient_the_member_sees(self):
        from django.test import Client

        from patients.models import Patient

        self.finish()
        url = f"/follow-ups/whatsapp/{self.patient}"
        self.assertEqual(self.doc.get(url).status_code, 302)  # the doctor's own patient
        self.assertEqual(self.b.client("assistant").get(url).status_code, 404)  # another place
        self.assertEqual(self.desk.get("/follow-ups/whatsapp/999999").status_code, 404)
        theirs, _item = self.owner_visit()
        other = f"/follow-ups/whatsapp/{theirs['patient']['id']}"
        self.assertEqual(self.desk.get(other).status_code, 302)
        self.assertEqual(self.doc.get(other).status_code, 404)  # another doctor's patient
        # No phone: no button and no link.
        with clinic_transaction(self.a.id, self.a.shard):
            Patient.objects.filter(clinic_id=self.a.id, id=self.patient).update(phone="")
        self.assertEqual(self.desk.get(url).status_code, 404)
        self.assertNotIn(url, self.desk.get("/follow-ups").content.decode())
        self.assertNotIn(url, self.strip())
        # Signed out: to the sign-in page, never to WhatsApp.
        self.assertIn("/login", Client().get(url)["Location"])
