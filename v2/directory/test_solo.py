"""The solo option (Saged, 2026-11-13): a single clinic whose owner runs everything alone. Never a center."""

from clinics.models import Clinic
from core.routing import clinic_transaction
from core.testing import ShardTestCase, call

from .test_signup import body, place_of, signup


def stored_settings(entry):
    with clinic_transaction(entry.id, entry.shard):
        return Clinic.objects.get(id=entry.id).settings


class SoloSignUpTests(ShardTestCase):
    def test_a_clinic_that_says_yes_is_solo(self):
        r = signup(data=body(solo=True))
        self.assertEqual(r.status_code, 201, r.content)
        _user, link = place_of("yasmine")
        self.assertTrue(stored_settings(link.clinic)["solo"])

    def test_no_answer_or_no_is_not_solo(self):
        self.assertEqual(signup(data=body(solo=False)).status_code, 201)
        _user, link = place_of("yasmine")
        self.assertNotIn("solo", stored_settings(link.clinic))

    def test_a_center_is_refused(self):
        sections = [{"specialty": "dental", "name": "Teeth"}, {"specialty": "pediatrics", "name": "Kids"}]
        for layout in ("one_place", "branches"):
            r = signup(data=body(kind="center", sections=sections, layout=layout, solo=True))
            self.assertEqual((r.status_code, r.json()["error"]["fields"]), (400, {"solo": "center"}), layout)
        self.assertEqual(signup(data=body(solo="yes")).status_code, 400)

    def test_the_signup_page_asks_for_a_clinic(self):
        html = self.client.get("/signup").content.decode()
        self.assertIn('name="solo" value="yes"', html)


class SoloSettingTests(ShardTestCase):
    def setUp(self):
        super().setUp()
        self.a = self.clinic("a")

    def put(self, fixture, changes):
        return call(fixture.client("owner"), "PUT", "/api/v1/settings/clinic", {"changes": changes, "expected": {}})

    def test_a_clinic_can_turn_it_on_and_off_later(self):
        r = self.put(self.a, {"solo": True})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.json()["settings"]["solo"])
        self.assertFalse(self.put(self.a, {"solo": False}).json()["settings"]["solo"])

    def test_a_center_never_gets_it(self):
        center = self.clinic("c", sections=[{"specialty": "dental", "name": "Teeth"},
                                            {"specialty": "pediatrics", "name": "Kids"}])
        r = self.put(center, {"solo": True})
        self.assertEqual((r.status_code, r.json()["error"]["fields"]), (400, {"solo": "center"}))
        self.assertEqual(self.put(center, {"solo": False}).status_code, 200)
