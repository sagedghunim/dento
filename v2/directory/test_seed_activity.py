"""seed_activity fills the demo places so the owner's Overview shows visits and revenue."""

import io

from django.core.management import call_command
from django.test import override_settings

from accounts.models import User
from clinics.models import Membership
from core.routing import clinic_transaction
from core.testing import ShardTestCase, enable_totp
from directory.models import DirectoryClinic


@override_settings(DEBUG=True)
class SeedActivityTests(ShardTestCase):
    def test_the_owner_overview_shows_visits_and_revenue(self):
        call_command("seed_demo", stdout=io.StringIO())
        out = io.StringIO()
        call_command("seed_activity", "--days", "30", stdout=out)
        self.assertIn("recompute differences 0", out.getvalue())
        entry = DirectoryClinic.objects.get(name="مركز سمايل لطب الأسنان")
        owner = User.objects.get(username="hamada")
        enable_totp(owner)
        with clinic_transaction(entry.id, entry.shard):
            membership = Membership.objects.get(clinic_id=entry.id, user_id=owner.id)
        client = self.client_for(owner, entry, membership)
        r = client.get("/overview?period=week")
        self.assertEqual(r.status_code, 200)
        clinic = r.context["clinic"]
        self.assertGreater(clinic["visits"], 0)
        self.assertGreater(clinic["revenue"], 0)
        again = io.StringIO()
        call_command("seed_activity", "--days", "30", stdout=again)
        self.assertEqual(again.getvalue().count("already has activity"), 3)
