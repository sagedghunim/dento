"""The owner's alerts (Saged, 2026-11-19): what the team did with money and records, from the change log."""

from core.testing import call

from clinical.testing import M2TestCase, put


class AlertTests(M2TestCase):
    def remove_a_procedure(self):
        visit = self.in_room()
        exam = self.procedure("Full examination")
        r = call(self.doc, "POST", f"/api/v1/appointments/{visit['id']}/procedures", {"procedure_id": exam["id"]})
        self.assertEqual(r.status_code, 201, r.content)
        record = r.json()["procedure"]
        r = call(self.doc, "DELETE", f"/api/v1/appointments/{visit['id']}/procedures/{record['id']}")
        self.assertEqual(r.status_code, 200, r.content)
        return visit, exam

    def remove_after_finish(self):
        """The doctor finishes the visit with a procedure on it, goes back in ("رجوع للكشف") and takes it off."""
        visit = self.in_room()
        url = f"/api/v1/appointments/{visit['id']}"
        exam = self.procedure("Full examination")
        r = call(self.doc, "POST", f"{url}/procedures", {"procedure_id": exam["id"]})
        self.assertEqual(r.status_code, 201, r.content)
        record = r.json()["procedure"]
        put(self.doc, f"{url}/note", {"notes": "Seen"}, 0)
        self.assertEqual(call(self.doc, "POST", f"{url}/finish", {"status": "in_room", "check_record": True}).status_code, 200)
        self.assertEqual(call(self.doc, "POST", f"{url}/reopen", {"status": "done"}).status_code, 200)
        r = call(self.doc, "DELETE", f"{url}/procedures/{record['id']}")
        self.assertEqual(r.status_code, 200, r.content)
        return visit, exam

    def test_each_alert_says_in_whose_shift_it_happened(self):
        # Who opened the register and who closed it around the alert (Saged, 2026-11-22).
        r = call(self.desk, "POST", "/api/v1/register/open", {"opening_float": 0})
        self.assertIn(r.status_code, (201, 409), r.content)  # the fixture's cash may have opened one already
        self.remove_after_finish()
        page = self.owner.get("/alerts").content.decode()
        line = page.split("data-shift")[1].split("</p>")[0]
        self.assertIn("فتحها", line)
        self.assertIn("لسه مفتوحة", line)

    def test_the_owner_only(self):
        self.assertEqual(self.owner.get("/alerts").status_code, 200)
        self.assertEqual(self.doc.get("/alerts").status_code, 403)
        self.assertEqual(self.desk.get("/alerts").status_code, 403)
        self.assertNotIn('href="/alerts"', self.desk.get("/patients").content.decode())

    def test_a_removed_procedure_shows_with_who_and_the_patient_and_is_counted_until_seen(self):
        visit, exam = self.remove_after_finish()
        side = self.owner.get("/patients").content.decode()
        self.assertIn('class="nav-count', side)
        page = self.owner.get("/alerts").content.decode()
        self.assertIn(exam["name_ar"], page)
        self.assertIn(f'href="/patients/{visit["patient"]["id"]}"', page)
        self.assertIn("is-new", page)
        # Opened: no count any more, and the row is no longer new.
        self.assertNotIn('class="nav-count', self.owner.get("/patients").content.decode())
        self.assertNotIn("is-new", self.owner.get("/alerts").content.decode())


class AlertChoiceTests(AlertTests):
    """Saged, 2026-11-24: drawer money in and out, and the owner turning kinds of alerts off. 2026-11-25: a procedure
    taken off while the visit is on is a fix and no alert; taken off after the visit was finished, one alert."""

    def rows(self):
        page = self.owner.get("/alerts").content.decode()
        return page.split('class="alert-list"', 1)[1].split("</ul>", 1)[0] if 'class="alert-list"' in page else ""

    def test_a_procedure_taken_off_during_the_visit_is_no_alert(self):
        self.remove_a_procedure()
        rows = self.rows()
        self.assertNotIn("شال إجراء", rows)
        self.assertNotIn("شال بند من فاتورة", rows)
        self.assertNotIn('class="nav-count', self.owner.get("/patients").content.decode())

    def test_a_procedure_taken_off_after_the_visit_ended_is_one_alert_not_two(self):
        self.remove_after_finish()
        rows = self.rows()
        self.assertEqual(rows.count("شال إجراء بعد ما الكشف خلص"), 1)
        self.assertNotIn("شال بند من فاتورة", rows)

    def test_alerts_come_in_four_groups(self):
        self.remove_after_finish()
        page = self.owner.get("/alerts").content.decode()
        for key in ("drawer", "bills", "files", "stock"):
            self.assertIn(f'data-group-head="{key}"', page, key)
            self.assertIn(f'href="?group={key}"', page, key)
        for name in ("الخزنة", "الفواتير والأسعار", "المرضى والملفات", "المخزن"):
            self.assertIn(name, page)
        self.assertIn("شال إجراء", self.owner.get("/alerts?group=bills").content.decode().split('class="alert-list"')[1])
        self.assertNotIn('class="alert-list"', self.owner.get("/alerts?group=stock").content.decode())
        self.assertEqual(self.owner.get("/alerts?group=nonsense").status_code, 200)

    def test_money_put_in_or_taken_out_of_the_drawer_is_an_alert_with_its_reason(self):
        call(self.desk, "POST", "/api/v1/register/open", {"opening_float": 0})
        r = call(self.desk, "POST", "/api/v1/register/movements", {"kind": "in", "amount": "200", "reason": "فكة من البنك"})
        self.assertEqual(r.status_code, 201, r.content)
        rows = self.rows()
        self.assertIn("حط فلوس في الخزنة", rows)
        self.assertIn("فكة من البنك", rows)
        page = self.desk.get("/finances?tab=register").content.decode()
        self.assertIn('data-cash="in"', page)
        self.assertIn('data-cash="out"', page)  # the spare way out
        r = call(self.desk, "POST", "/api/v1/register/movements", {"kind": "out", "amount": "50", "reason": "تصليح طارئ"})
        self.assertEqual(r.status_code, 201, r.content)
        rows = self.rows()
        self.assertIn("سحب فلوس من الخزنة", rows)
        self.assertIn("تصليح طارئ", rows)

    def test_the_owner_turns_a_kind_off_and_it_is_neither_listed_nor_counted(self):
        self.remove_after_finish()
        self.owner.get("/alerts")  # seen
        r = call(self.owner, "PUT", "/api/v1/alerts/settings", {"off": ["procedure"]})
        self.assertEqual((r.status_code, r.json()["off"]), (200, ["procedure"]), r.content)
        self.remove_after_finish()
        self.assertNotIn('class="nav-count', self.owner.get("/patients").content.decode())
        page = self.owner.get("/alerts").content.decode()
        self.assertNotIn("شال إجراء", self.rows())
        self.assertIn('value="procedure">', page)  # its switch shows off
        self.assertIn('value="line" checked', page)
        # Back on: they are listed again.
        self.assertEqual(call(self.owner, "PUT", "/api/v1/alerts/settings", {"off": []}).status_code, 200)
        self.assertIn("شال إجراء", self.rows())
        # The owner only, and only known kinds.
        self.assertEqual(call(self.desk, "PUT", "/api/v1/alerts/settings", {"off": []}).status_code, 403)
        self.assertEqual(call(self.owner, "PUT", "/api/v1/alerts/settings", {"off": ["nope"]}).status_code, 400)
