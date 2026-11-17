"""No double booking of one patient on one day (Saged, 2026-11-17), through Book, Walk-in, plan sessions, moving a
booking, undoing "didn't come" and marking arrived. The two-desk races are in test_concurrency."""

import datetime as dt

from core.testing import call, new_patient

from .testing import M1TestCase, free_rooms, today


class SameDayTests(M1TestCase):
    def setUp(self):
        super().setUp()
        self.owner_id = self.a.membership("owner").id
        self.doctor_id = self.a.membership("doctor").id
        self.first = self.book().json()["appointment"]  # a new patient, booked with the owner's queue today
        self.pid = self.first["patient"]["id"]

    def error(self, r, status=409):
        self.assertEqual(r.status_code, status, r.content)
        return r.json()["error"]

    def walk_in(self, expect=201, **body):
        r = call(self.desk, "POST", "/api/v1/appointments/walk-in", {"patient_id": self.pid, **body})
        if expect is not None:
            self.assertEqual(r.status_code, expect, r.content)
        return r

    def at(self, hhmm, day=None):
        return self.slot_iso(self.a, day or today(self.a), hhmm)

    def test_a_second_booking_with_the_same_doctor_that_day_is_refused_and_names_the_turn(self):
        error = self.error(self.book(patient_id=self.pid, expect=None))
        self.assertEqual(error["code"], "already_booked")
        self.assertEqual((error["appointment"]["id"], error["appointment"]["queue_no"]), (self.first["id"], 1))
        self.assertIn("1", error["message"])
        self.assertEqual(self.error(self.walk_in(expect=None, doctor_id=self.owner_id))["code"], "already_booked")

    def test_a_slot_booking_names_its_time(self):
        slot = self.book(patient_id=self.pid, doctor_id=self.doctor_id, starts_at=self.at("11:00"),
                         confirm_other_doctor=True).json()["appointment"]
        error = self.error(self.book(patient_id=self.pid, doctor_id=self.doctor_id, starts_at=self.at("12:00"),
                                     confirm_other_doctor=True, expect=None))
        self.assertEqual((error["code"], error["appointment"]["id"]), ("already_booked", slot["id"]))
        self.assertIn("11:00", error["message"])

    def test_another_doctor_the_same_day_is_a_warning_the_desk_confirms(self):
        r = self.book(patient_id=self.pid, doctor_id=self.doctor_id, starts_at=self.at("11:00"), expect=None)
        error = self.error(r)
        self.assertEqual(error["code"], "other_doctor_today")
        self.assertEqual([v["id"] for v in error["appointments"]], [self.first["id"]])
        self.book(patient_id=self.pid, doctor_id=self.doctor_id, starts_at=self.at("11:00"), confirm_other_doctor=True)

    def test_another_day_is_free(self):
        tomorrow = (today(self.a) + dt.timedelta(days=1)).isoformat()
        self.book(patient_id=self.pid, day=tomorrow)

    def test_after_the_visit_is_finished_or_cancelled_booking_again_is_allowed(self):
        self.assertEqual(self.act(self.first["id"], "cancel", "booked").status_code, 200)
        second = self.book(patient_id=self.pid, arrive=True).json()["appointment"]
        free_rooms(self.a)
        self.assertEqual(self.act(second["id"], "call", "waiting").status_code, 200)
        self.assertEqual(self.act(second["id"], "finish", "in_room").status_code, 200)
        self.walk_in()  # finished: a new walk-in the same day is fine

    def test_a_patient_who_is_here_now_gets_no_second_visit_with_anyone(self):
        self.assertEqual(self.act(self.first["id"], "arrive", "booked").status_code, 200)
        for status, action in (("waiting", None), ("waiting", "assist"), ("with_assistant", "ready"), ("ready", "call")):
            if action:
                self.assertEqual(self.act(self.first["id"], action, status).status_code, 200)
            for r in (self.book(patient_id=self.pid, doctor_id=self.doctor_id, starts_at=self.at("13:00"),
                                confirm_other_doctor=True, expect=None),
                      self.walk_in(expect=None, doctor_id=self.doctor_id, confirm_other_doctor=True),
                      self.walk_in(expect=None)):
                error = self.error(r)
                self.assertEqual((error["code"], error["appointment"]["id"]), ("already_booked", self.first["id"]))

    def test_a_double_tap_under_two_keys_books_once(self):
        new = self.book(new_patient=new_patient("Huda Samir Ali"), doctor_id=self.doctor_id,
                        starts_at=self.at("10:00")).json()["appointment"]
        body = {"patient_id": new["patient"]["id"], "doctor_id": self.owner_id, "confirm_other_doctor": True}
        first = call(self.desk, "POST", "/api/v1/appointments/walk-in", body)
        second = call(self.desk, "POST", "/api/v1/appointments/walk-in", body)  # a new key: not a replay
        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(self.error(second)["code"], "already_booked")

    def test_the_finish_dialogs_next_visit_the_same_day_is_allowed_once_this_one_is_done(self):
        second = self.book(new_patient=new_patient("Next Visit Patient"), arrive=True).json()["appointment"]
        free_rooms(self.a)
        self.assertEqual(self.act(second["id"], "call", "waiting").status_code, 200)
        r = self.act(second["id"], "finish", "in_room", next_visit={
            "doctor_id": self.owner_id, "service_id": self.services_a["consultation"].id,
            "day": today(self.a).isoformat()})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIsNotNone(r.json()["next_appointment"])

    def test_moving_a_booking_onto_another_open_visit_is_refused(self):
        tomorrow = today(self.a) + dt.timedelta(days=1)
        later = self.book(patient_id=self.pid, day=tomorrow.isoformat()).json()["appointment"]
        r = call(self.desk, "POST", f"/api/v1/appointments/{later['id']}/reschedule",
                 {"status": "booked", "doctor_id": self.owner_id, "day": today(self.a).isoformat()})
        self.assertEqual(self.error(r)["code"], "already_booked")

    def test_undoing_didnt_come_is_refused_once_another_visit_took_its_place(self):
        self.assertEqual(self.act(self.first["id"], "no-show", "booked").status_code, 200)
        self.walk_in()
        r = self.act(self.first["id"], "undo-no-show", "no_show")
        self.assertEqual(self.error(r)["code"], "already_booked")

    def test_a_second_visit_cant_be_marked_arrived_while_the_first_is_here(self):
        other = self.book(patient_id=self.pid, doctor_id=self.doctor_id, starts_at=self.at("13:00"),
                          confirm_other_doctor=True).json()["appointment"]
        self.assertEqual(self.act(self.first["id"], "arrive", "booked").status_code, 200)
        error = self.error(self.act(other["id"], "arrive", "booked"))
        self.assertEqual((error["code"], error["appointment"]["id"]), ("already_here", self.first["id"]))
        self.assertEqual(self.act(self.first["id"], "arrive", "booked").status_code, 200)  # a repeat stays harmless

    def test_the_quote_shows_the_patients_open_visits_that_day(self):
        q = call(self.desk, "GET", f"/api/v1/appointments/quote?doctor={self.doctor_id}"
                                   f"&service={self.services_a['consultation'].id}&patient={self.pid}").json()
        self.assertEqual([(v["id"], v["same_doctor"], v["here"]) for v in q["same_day"]], [(self.first["id"], False, False)])

    def test_another_clinics_visits_never_count(self):
        self.book(fixture=self.b)  # B's own patient
        self.walk_in(expect=None)
        r = call(self.b.client("assistant"), "POST", "/api/v1/appointments", {
            "patient_id": self.pid, "doctor_id": self.b.membership("owner").id,
            "service_id": self.services_b["consultation"].id})
        self.assertEqual(r.status_code, 404)  # A's patient is unknown in B
