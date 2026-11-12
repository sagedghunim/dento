"""Patient tags, insights and lists (design F6), and the performance reports (F8)."""

from audit.models import AuditAccess
from clinical.testing import M2TestCase
from core.routing import clinic_transaction
from core.testing import call
from scheduling.testing import today


class TagTests(M2TestCase):
    def tag(self, name, client=None):
        r = call(client or self.owner, "POST", "/api/v1/patient-tags", {"name": name, "color": "#aa0000"})
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()["tag"]

    def test_tags_are_editable_per_place_and_filter_the_search(self):
        vip, insured = self.tag("VIP"), self.tag("Insurance")
        dup = call(self.owner, "POST", "/api/v1/patient-tags", {"name": "VIP"})
        self.assertEqual((dup.status_code, dup.json()["error"]["code"]), (409, "duplicate"))
        self.assertEqual(call(self.owner, "POST", "/api/v1/patient-tags", {"name": "x", "color": "red"}).status_code, 400)
        self.assertEqual(call(self.desk, "POST", "/api/v1/patient-tags", {"name": "Desk"}).status_code, 403)
        one = self.book().json()["appointment"]["patient"]["id"]
        self.book(new_patient={"full_name": "Mohamed Ali", "phone": "01099999999"})
        r = call(self.desk, "PUT", f"/api/v1/patients/{one}/tags", {"tag_ids": [vip["id"], insured["id"]]})
        self.assertEqual([t["name"] for t in r.json()["tags"]], ["Insurance", "VIP"])
        r = call(self.desk, "PUT", f"/api/v1/patients/{one}/tags", {"tag_ids": [vip["id"]]})
        self.assertEqual([t["name"] for t in r.json()["tags"]], ["VIP"])
        self.assertEqual([p["id"] for p in call(self.desk, "GET", f"/api/v1/patients?tag={vip['id']}").json()["items"]],
                         [one])
        self.assertEqual([p["id"] for p in call(self.desk, "GET", f"/api/v1/patients?q=Mohamed&tag={vip['id']}")
                          .json()["items"]], [one])
        # Removing a tag hides it from the patient; another place's tag or patient is 404.
        call(self.owner, "PATCH", f"/api/v1/patient-tags/{vip['id']}", {"is_active": False})
        self.assertEqual(call(self.desk, "GET", f"/api/v1/patients/{one}/tags").json()["tags"], [])
        theirs = self.tag("Theirs", client=self.b.client("owner"))
        self.assertEqual(call(self.desk, "PUT", f"/api/v1/patients/{one}/tags", {"tag_ids": [theirs["id"]]}).status_code,
                         404)
        self.assertEqual(call(self.owner, "PATCH", f"/api/v1/patient-tags/{theirs['id']}", {"name": "z"}).status_code, 404)
        self.assertEqual(call(self.desk, "GET", f"/api/v1/patients?tag={theirs['id']}").status_code, 404)
        b_patient = self.book(fixture=self.b).json()["appointment"]["patient"]["id"]
        self.assertEqual(call(self.desk, "PUT", f"/api/v1/patients/{b_patient}/tags", {"tag_ids": []}).status_code, 404)


class InsightTests(M2TestCase):
    def test_counts_lists_and_export(self):
        visit = self.owed_visit()
        patient = visit["patient"]["id"]
        body = call(self.owner, "GET", "/api/v1/reports/insights").json()
        self.assertEqual((body["active"], body["new_this_month"], body["patients"]), (1, 1, 1))
        listed = call(self.owner, "GET", "/api/v1/reports/patients/outstanding_balance").json()
        self.assertEqual([(i["patient"]["id"], i["owed"]) for i in listed["items"]],
                         [(patient, self.services_a["procedure"].price)])
        self.assertEqual(call(self.owner, "GET", "/api/v1/reports/patients/never_came_back").json()["items"], [])
        self.assertEqual(call(self.owner, "GET", "/api/v1/reports/patients/nothing").status_code, 404)
        r = call(self.owner, "GET", "/api/v1/reports/patients/outstanding_balance/export")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/csv", r["Content-Type"])
        self.assertIn(str(visit["patient"]["file_no"]), r.content.decode("utf-8-sig"))
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertEqual(AuditAccess.objects.filter(clinic_id=self.a.id, patient_id=patient,
                                                        action=AuditAccess.Action.EXPORT).count(), 1)
        # The export is the owner's unless given; the lists need reports.view.
        self.assertEqual(call(self.doc, "GET", "/api/v1/reports/patients/outstanding_balance/export").status_code, 403)
        self.assertEqual(call(self.desk, "GET", "/api/v1/reports/insights").status_code, 403)
        # Another place sees none of it.
        self.assertEqual(call(self.b.client("owner"), "GET", "/api/v1/reports/patients/outstanding_balance")
                         .json()["items"], [])

    def test_money_shows_only_with_finances_view(self):
        visit = self.in_room()
        call(self.desk, "POST", f"/api/v1/appointments/{visit['id']}/payments", {"amount": 700, "method": "cash"})
        self.act(visit["id"], "finish", "in_room")
        money = {"revenue_this_month", "revenue_per_patient", "collected", "refunded", "revenue"}
        urls = ("/api/v1/reports/insights", "/api/v1/reports/performance", "/api/v1/reports/staff")
        owner = [call(self.owner, "GET", url).json() for url in urls]
        self.assertEqual(owner[0]["revenue_this_month"], 700)
        self.assertEqual(owner[2]["items"][0]["revenue"], 700)
        self.assertFalse(owner[0]["money_hidden"])
        self.set_codes("doctor", granted=["reports.view"])
        for url in urls:
            body = call(self.doc, "GET", url).json()
            self.assertTrue(body["money_hidden"], url)
            for row in [body, *body.get("items", [])]:
                self.assertFalse(money & set(row), (url, row))
        self.assertEqual(call(self.doc, "GET", urls[2]).json()["items"][0]["visits"], 1)

    def test_an_export_over_the_cap_is_refused_not_cut(self):
        from unittest import mock

        self.owed_visit()
        second = self.book(new_patient={"full_name": "Second Patient", "phone": "01055555555"}).json()["appointment"]
        self.owed_visit(second["patient"]["id"])
        url = "/api/v1/reports/patients/outstanding_balance/export"
        with mock.patch("reports.selectors.EXPORT_LIMIT", 1):
            r = call(self.owner, "GET", url)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "export_too_large"), r.content)
        with clinic_transaction(self.a.id, self.a.shard):  # nothing was exported, so nothing was logged
            self.assertFalse(AuditAccess.objects.filter(clinic_id=self.a.id, action=AuditAccess.Action.EXPORT).exists())
        with mock.patch("reports.selectors.EXPORT_LIMIT", 2):
            r = call(self.owner, "GET", url)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.content.decode("utf-8-sig").strip().splitlines()), 3)  # the header and both rows

    def test_unfinished_treatment_and_unused_sessions(self):
        visit = self.in_room()
        patient = visit["patient"]["id"]
        crown = self.procedure("Zirconia crown")
        r = call(self.doc, "POST", f"/api/v1/patients/{patient}/plans", {"items": [{"procedure_id": crown["id"], "tooth": 11}]})
        self.assertEqual(r.status_code, 201, r.content)
        scaling = self.procedure("Scaling and polishing")
        package = call(self.owner, "POST", "/api/v1/packages", {"procedure_id": scaling["id"], "name": "x3", "sessions": 3,
                                                               "price": 100}).json()["package"]
        call(self.desk, "POST", f"/api/v1/appointments/{visit['id']}/packages", {"package_id": package["id"]})
        lists = {name: call(self.owner, "GET", f"/api/v1/reports/patients/{name}").json()["items"]
                 for name in ("unfinished_treatment", "unused_sessions")}
        self.assertEqual([(i["patient"]["id"], i["open_items"]) for i in lists["unfinished_treatment"]], [(patient, 1)])
        self.assertEqual([(i["patient"]["id"], i["sessions_left"]) for i in lists["unused_sessions"]], [(patient, 3)])


class PerformanceTests(M2TestCase):
    def test_clinic_and_staff_performance_read_the_summaries(self):
        visit = self.in_room()
        self.act(visit["id"], "finish", "in_room")
        body = call(self.owner, "GET", "/api/v1/reports/performance").json()
        day = today(self.a).isoformat()
        row = next(i for i in body["items"] if i["key"] == day)
        self.assertEqual(row["visits"], 1)
        self.assertEqual(row["timed"], 1)
        self.assertIsNotNone(row["avg_doctor_secs"])
        staff = call(self.owner, "GET", "/api/v1/reports/staff").json()["items"]
        self.assertEqual([(s["membership_id"], s["visits"]) for s in staff], [(self.a.membership("owner").id, 1)])
        month = call(self.owner, "GET", "/api/v1/reports/performance?by=month").json()
        self.assertEqual(month["items"][0]["key"], day[:7])
        self.assertEqual(call(self.desk, "GET", "/api/v1/reports/staff").status_code, 403)
