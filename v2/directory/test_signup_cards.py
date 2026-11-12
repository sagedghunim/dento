"""The sign-up cards (design E2): counts per specialty, one place or separate branches, for sign-up, adding a place
and the platform admin's form."""

from django.test import Client

from accounts.models import User
from clinics.models import Section
from core.routing import clinic_transaction
from core.testing import ShardTestCase, call
from directory.flows import plan_places
from directory.models import UserClinic

from .test_signup import body, signup


def places_of(username):
    user = User.objects.get(username=username)
    found = []
    for link in UserClinic.objects.select_related("clinic").filter(user=user, role="owner").order_by("clinic_id"):
        with clinic_transaction(link.clinic_id, link.clinic.shard):
            sections = list(Section.objects.filter(clinic_id=link.clinic_id).order_by("sort", "id")
                            .values_list("name", "specialty_code"))
        found.append((link.clinic.name, link.clinic.kind, sections))
    return found


class PlanTests(ShardTestCase):
    sql_check = False

    def test_a_count_names_the_sections_and_a_count_of_one_keeps_the_specialty_name(self):
        self.assertEqual(plan_places("center", [{"specialty": "dental", "count": 3}, {"specialty": "pediatrics"}]), [[
            {"specialty": "dental", "name": "أسنان 1", "name_en": "Dental 1"},
            {"specialty": "dental", "name": "أسنان 2", "name_en": "Dental 2"},
            {"specialty": "dental", "name": "أسنان 3", "name_en": "Dental 3"},
            {"specialty": "pediatrics", "name": "أطفال", "name_en": "Pediatrics"}]])  # defaults in both languages (G3)

    def test_branches_group_by_specialty_in_both_shapes(self):
        from clinics.specialties import SPECIALTIES

        ent_en = SPECIALTIES["ent"]["name"]["en"]
        counted = plan_places("center", [{"specialty": "dental", "count": 2}, {"specialty": "ent", "count": 1}],
                              layout="branches")
        per_section = plan_places("center", [{"specialty": "dental", "count": 1, "name": "أسنان 1"},
                                             {"specialty": "ent", "count": 1},
                                             {"specialty": "dental", "count": 1, "name": "أسنان 2"}], layout="branches")
        expected = [[{"specialty": "dental", "name": "أسنان 1", "name_en": "Dental 1"},
                     {"specialty": "dental", "name": "أسنان 2", "name_en": "Dental 2"}],
                    [{"specialty": "ent", "name": "أنف وأذن وحنجرة", "name_en": ent_en}]]
        self.assertEqual(counted, expected)
        # Typed names stay as typed, with no English (design G3).
        for place in expected:
            for section in place:
                if section["specialty"] == "dental":
                    section["name_en"] = ""
        self.assertEqual(per_section, expected)

    def test_limits_are_clear_400s(self):
        from core.errors import ApiError

        cases = [
            ("center", [{"specialty": "dental", "count": 21}], "one_place", "sections.0.count"),
            ("center", [{"specialty": "dental", "count": 0}], "one_place", "sections.0.count"),
            ("center", [{"specialty": "dental", "count": True}], "one_place", "sections.0.count"),
            ("center", [{"specialty": "dental", "count": 15}, {"specialty": "ent", "count": 6}], "one_place", "sections"),
            ("center", [{"specialty": "dental"}], "one_place", "sections"),
            ("clinic", [{"specialty": "dental", "count": 2}], "one_place", "sections"),
            ("center", [{"specialty": "dental", "count": 2}], "elsewhere", "layout"),
            ("center", [{"specialty": "nope", "count": 2}], "one_place", "sections.0.specialty"),
        ]
        for kind, sections, layout, field in cases:
            with self.assertRaises(ApiError, msg=(kind, sections, layout)) as caught:
                plan_places(kind, sections, layout=layout)
            self.assertIn(field, caught.exception.fields, (kind, sections, layout))
        # Branches may hold 20 per place: 15 dental and 6 ENT are two places.
        self.assertEqual([len(p) for p in plan_places("center", [{"specialty": "dental", "count": 15},
                                                                 {"specialty": "ent", "count": 6}], layout="branches")],
                         [15, 6])


class SignUpCardsTests(ShardTestCase):
    def test_one_place_with_counted_cards(self):
        data = body(kind="center", layout="one_place", sections=[{"specialty": "dental", "count": 8},
                                                                 {"specialty": "pediatrics", "count": 2}])
        self.assertEqual(signup(data=data).status_code, 201)
        [(name, kind, sections)] = places_of("yasmine")
        self.assertEqual((kind, len(sections), sections[0], sections[-1]),
                         ("center", 10, ("أسنان 1", "dental"), ("أطفال 2", "pediatrics")))

    def test_separate_branches_make_one_place_per_specialty_under_one_owner(self):
        data = body(kind="center", layout="branches", sections=[
            {"specialty": "dental", "count": 1, "name": "أسنان أ"}, {"specialty": "pediatrics", "count": 1},
            {"specialty": "dental", "count": 1, "name": "أسنان ب"}])
        client = Client()
        self.assertEqual(signup(client, data=data).status_code, 201)
        places = places_of("yasmine")
        self.assertEqual([(kind, sections) for _name, kind, sections in places], [
            ("center", [("أسنان أ", "dental"), ("أسنان ب", "dental")]), ("clinic", [("أطفال", "pediatrics")])])
        self.assertEqual([name for name, *_ in places], ["عيادة ياسمين - أسنان", "عيادة ياسمين - أطفال"])

    def test_adding_a_place_takes_the_same_cards(self):
        self.assertEqual(signup(data=body()).status_code, 201)
        user = User.objects.get(username="yasmine")
        client = Client()
        client.force_login(user)
        place = {k: v for k, v in body(kind="center", layout="branches", name="فرع",
                                       sections=[{"specialty": "ent", "count": 2}, {"specialty": "cardiology"}]).items()
                 if k != "owner"}
        r = call(client, "POST", "/api/v1/me/places", place)
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual([c["kind"] for c in r.json()["clinics"]], ["center", "clinic"])
        self.assertEqual(len(places_of("yasmine")), 3)

    def test_the_platform_admin_creates_branches_too(self):
        admin = self.user("platform_admin_2", is_platform_admin=True, with_totp=True)
        client = Client()
        client.force_login(admin)
        session = client.session
        from core.api import SESSION_MFA

        session[SESSION_MFA] = True
        session.save()
        r = call(client, "POST", "/api/v1/platform/clinics", {
            "name": "مجمع النور", "kind": "center", "layout": "branches", "owner_username": "nour_owner",
            "owner_name": "د. نور", "sections": [{"specialty": "dental", "count": 3}, {"specialty": "ent", "count": 1}]})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual([(c["kind"], c["name"]) for c in r.json()["clinics"]],
                         [("center", "مجمع النور - أسنان"), ("clinic", "مجمع النور - أنف وأذن وحنجرة")])
        self.assertEqual(r.json()["clinic"], r.json()["clinics"][0])
