"""Session packages (design F7): sell on a visit, use a session at no charge, void only while unused."""

import threading

from django.db import connections
from django.test import TransactionTestCase

from billing.models import PatientPackage
from clinical.testing import M2TestCase
from core.routing import clinic_transaction
from core.testing import ShardTestCase, call
from scheduling.testing import add_schedule, services_of


class PackageTests(M2TestCase):
    def package(self, name="Scaling x3", sessions=3, price=60000, procedure="Scaling and polishing", client=None):
        proc = self.procedure(procedure, client=client)
        r = call(client or self.owner, "POST", "/api/v1/packages",
                 {"procedure_id": proc["id"], "name": name, "sessions": sessions, "price": price})
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()["package"], proc

    def sell(self, visit, package, client=None):
        return call(client or self.desk, "POST", f"/api/v1/appointments/{visit['id']}/packages",
                    {"package_id": package["id"]})

    def record(self, visit, proc, **extra):
        return call(self.doc, "POST", f"/api/v1/appointments/{visit['id']}/procedures",
                    {"procedure_id": proc["id"], **extra})

    def test_selling_adds_a_line_and_later_sessions_are_free_until_used_up(self):
        package, proc = self.package(sessions=2)
        visit = self.in_room()
        patient = visit["patient"]["id"]
        r = self.sell(visit, package)
        self.assertEqual(r.status_code, 201, r.content)
        lines = r.json()["invoice"]["lines"]
        self.assertEqual([(line["kind"], line["amount"]) for line in lines][-1], ("package", 60000))
        self.assertEqual(r.json()["patient_package"]["left"], 2)
        for expected_left in (1, 0):
            r = self.record(visit, proc)
            self.assertEqual(r.status_code, 201, r.content)
            self.assertEqual(r.json()["procedure"]["amount"], 0)
            left = call(self.desk, "GET", f"/api/v1/patients/{patient}/packages").json()["items"][0]["left"]
            self.assertEqual(left, expected_left)
        r = self.record(visit, proc)  # none left: the list price again
        with clinic_transaction(self.a.id, self.a.shard):
            from clinical.models import VisitProcedure

            amounts = list(VisitProcedure.objects.filter(clinic_id=self.a.id, appointment_id=visit["id"])
                           .order_by("id").values_list("amount", "patient_package_id"))
        self.assertEqual([a for a, _ in amounts], [0, 0, proc["price"]])
        self.assertEqual(amounts[2][1], None)
        # The doctor may also charge a session normally.
        package2, _ = self.package(name="Scaling x1", sessions=1)
        self.sell(visit, package2)
        self.assertEqual(self.record(visit, proc, use_package=False).status_code, 201)
        self.assertEqual(call(self.desk, "GET", f"/api/v1/patients/{patient}/packages").json()["items"][0]["left"], 1)

    def test_a_package_line_goes_only_while_unused_and_voids_with_its_invoice(self):
        package, proc = self.package()
        visit = self.in_room()
        sold = self.sell(visit, package).json()
        line = next(line for line in sold["invoice"]["lines"] if line["kind"] == "package")
        invoice_id = sold["invoice"]["id"]
        self.record(visit, proc)
        r = call(self.owner, "DELETE", f"/api/v1/invoices/{invoice_id}/lines/{line['id']}")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "package_used"))
        r = call(self.owner, "POST", f"/api/v1/invoices/{invoice_id}/void")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "package_used"))
        other = self.in_room()
        sold = self.sell(other, package).json()
        line = next(line for line in sold["invoice"]["lines"] if line["kind"] == "package")
        r = call(self.owner, "DELETE", f"/api/v1/invoices/{sold['invoice']['id']}/lines/{line['id']}")
        self.assertEqual(r.status_code, 200, r.content)
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertIsNotNone(PatientPackage.objects.get(clinic_id=self.a.id, id=sold["patient_package"]["id"]).voided_at)
        # A package line can't be typed in by hand.
        r = call(self.owner, "POST", f"/api/v1/invoices/{invoice_id}/lines",
                 {"kind": "package", "description": "x", "qty": 1, "unit_price": 1})
        self.assertEqual(r.status_code, 400)

    def test_removing_a_procedure_line_that_used_a_session_gives_the_session_back(self):
        package, proc = self.package(sessions=3)
        visit = self.in_room()
        patient = visit["patient"]["id"]
        self.assertEqual(self.sell(visit, package).status_code, 201)
        r = self.record(visit, proc)
        self.assertEqual(r.status_code, 201, r.content)
        line_id, invoice_id = r.json()["procedure"]["invoice_line_id"], r.json()["invoice"]["id"]
        packages = f"/api/v1/patients/{patient}/packages"
        self.assertEqual(call(self.desk, "GET", packages).json()["items"][0]["left"], 2)
        r = call(self.doc, "DELETE", f"/api/v1/invoices/{invoice_id}/lines/{line_id}")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(call(self.desk, "GET", packages).json()["items"][0]["left"], 3)
        with clinic_transaction(self.a.id, self.a.shard):
            from audit.models import AuditChange
            from clinical.models import VisitProcedure

            procedure = VisitProcedure.objects.get(clinic_id=self.a.id, appointment_id=visit["id"])
            self.assertIsNone(procedure.patient_package_id)
            self.assertIsNone(procedure.invoice_line_id)
            self.assertTrue(AuditChange.objects.filter(clinic_id=self.a.id, action="package.session_return").exists())

    def test_codes_sections_and_another_place(self):
        r = call(self.desk, "POST", "/api/v1/packages", {"procedure_id": 1, "name": "x", "sessions": 1, "price": 1})
        self.assertEqual(r.status_code, 403)
        package, _ = self.package()
        dup = call(self.owner, "POST", "/api/v1/packages", {"procedure_id": package["procedure_id"],
                                                           "name": package["name"], "sessions": 1, "price": 1})
        self.assertEqual((dup.status_code, dup.json()["error"]["code"]), (409, "duplicate"))
        bad = call(self.owner, "POST", "/api/v1/packages", {"procedure_id": package["procedure_id"], "name": "y",
                                                           "sessions": 0, "price": 1})
        self.assertEqual(bad.status_code, 400)
        theirs, _ = self.package(client=self.b.client("owner"))
        visit = self.in_room()
        self.assertEqual(self.sell(visit, theirs).status_code, 404)
        self.assertEqual(call(self.owner, "PATCH", f"/api/v1/packages/{theirs['id']}", {"price": 1}).status_code, 404)
        b_visit = self.in_room(fixture=self.b)
        self.assertEqual(self.sell(b_visit, package).status_code, 404)
        self.assertEqual(call(self.desk, "GET", f"/api/v1/patients/{b_visit['patient']['id']}/packages").status_code, 404)
        r = call(self.owner, "PATCH", f"/api/v1/packages/{package['id']}", {"is_active": False})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.sell(visit, package).status_code, 400)


class PackageConcurrencyTests(TransactionTestCase):
    """F7 on real PostgreSQL: two recordings racing for a package's last session use it once."""

    databases = "__all__"

    def setUp(self):
        self.a = ShardTestCase().clinic("pk", "shard_1")
        add_schedule(self.a, "owner")
        self.services = services_of(self.a)

    def test_the_last_session_is_used_once(self):
        owner, desk = self.a.client("owner"), self.a.client("assistant")
        proc = next(p for p in call(owner, "GET", "/api/v1/procedures").json()["items"]
                    if p["name_en"] == "Scaling and polishing")
        package = call(owner, "POST", "/api/v1/packages", {"procedure_id": proc["id"], "name": "One", "sessions": 1,
                                                          "price": 100}).json()["package"]
        visits = []
        patient = None
        for _ in range(2):
            body = {"doctor_id": self.a.membership("owner").id, "service_id": self.services["procedure"].id}
            body.update({"patient_id": patient} if patient else {"new_patient": {"full_name": "Race Patient"}})
            visit = call(desk, "POST", "/api/v1/appointments", body).json()["appointment"]
            patient = visit["patient"]["id"]
            call(desk, "POST", f"/api/v1/appointments/{visit['id']}/arrive", {"status": "booked"})
            visits.append(visit)
        self.assertEqual(call(desk, "POST", f"/api/v1/appointments/{visits[0]['id']}/packages",
                              {"package_id": package["id"]}).status_code, 201)
        barrier = threading.Barrier(2)
        results = [None, None]

        def worker(index, visit, role):
            try:
                client = self.a.client(role)
                barrier.wait()
                results[index] = call(client, "POST", f"/api/v1/appointments/{visit['id']}/procedures",
                                      {"procedure_id": proc["id"]}).status_code
            except Exception as error:
                results[index] = repr(error)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker, args=(0, visits[0], "owner")),
                   threading.Thread(target=worker, args=(1, visits[1], "owner"))]  # the owner's visits
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(results, [201, 201])
        with clinic_transaction(self.a.id, self.a.shard):
            from clinical.models import VisitProcedure

            self.assertEqual(PatientPackage.objects.get(clinic_id=self.a.id).used, 1)
            amounts = sorted(VisitProcedure.objects.filter(clinic_id=self.a.id).values_list("amount", flat=True))
        self.assertEqual(amounts, [0, proc["price"]])


class PackageAccessTests(M2TestCase):
    def test_reading_a_patients_packages_writes_one_access_event(self):
        from audit.models import AuditAccess

        patient = self.in_room()["patient"]["id"]
        with clinic_transaction(self.a.id, self.a.shard):
            before = AuditAccess.objects.filter(clinic_id=self.a.id, patient_id=patient).count()
        self.assertEqual(call(self.desk, "GET", f"/api/v1/patients/{patient}/packages").status_code, 200)
        with clinic_transaction(self.a.id, self.a.shard):
            self.assertEqual(AuditAccess.objects.filter(clinic_id=self.a.id, patient_id=patient,
                                                        action=AuditAccess.Action.HISTORY).count() - before, 1)
