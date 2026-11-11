"""Demo places for local use (DEBUG only). Never run on production data.

Safe to run again: it creates only the demo places that don't exist yet (by name), reuses demo
accounts that exist without touching their passwords, and never deletes anything. That is how a
database seeded before the center existed gets the center.
"""

import datetime as dt
import secrets

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from accounts.models import User
from clinics.models import Schedule, Section
from core.routing import clinic_transaction
from directory.flows import add_member, create_clinic
from directory.models import DirectoryClinic

DEMO = [
    {
        "name": "مركز سمايل لطب الأسنان", "specialty": "dental", "city": "القاهرة", "shard": "shard_1",
        "owner": ("hamada", "د. حمادة عزيز"),
        "members": [("ahmed", "د. أحمد سامي", "doctor"), ("sara", "سارة محمود", "assistant")],
        # (username, section index, weekdays, starts, ends, mode, slot minutes)
        "sessions": [("hamada", 0, (5, 6, 0, 1, 2, 3), "17:00", "23:00", "queue", None),
                     ("ahmed", 0, (5, 6, 0, 1, 2, 3), "12:00", "16:00", "slots", 20)],
    },
    {
        "name": "عيادة د. منى للأطفال", "specialty": "pediatrics", "city": "الجيزة", "shard": "shard_2",
        "owner": ("mona", "د. منى فتحي"),
        "members": [("sara", "سارة محمود", "assistant")],
        "sessions": [("mona", 0, (5, 6, 0, 1, 2, 3), "18:00", "00:30", "queue", None)],
    },
    {
        # A medical center (design D1): one front desk and one patient file across three sections.
        "name": "مركز الشفاء الطبي", "kind": "center", "city": "الإسكندرية", "shard": "shard_2",
        "sections": [{"specialty": "dental", "name": "الأسنان"}, {"specialty": "pediatrics", "name": "الأطفال"},
                     {"specialty": "dermatology", "name": "الجلدية"}],
        "owner": ("karim", "د. كريم يوسف"),
        "members": [("nour", "د. نور حسن", "doctor"), ("omar", "د. عمر خالد", "doctor"),
                    ("laila", "ليلى إبراهيم", "assistant")],
        "sessions": [("karim", 0, (5, 6, 0, 1, 2, 3), "17:00", "22:00", "queue", None),
                     ("omar", 1, (5, 6, 0, 1, 2, 3), "16:00", "21:00", "slots", 15),
                     ("nour", 2, (5, 0, 2), "18:00", "23:00", "queue", None),
                     ("nour", 0, (6, 1, 3), "12:00", "15:00", "queue", None)],
    },
]


class Command(BaseCommand):
    help = ("Create the demo places that don't exist yet: two clinics on two shards, with an assistant who works in "
            "both, and a center with three sections. Existing places and accounts are left as they are.")

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("seed_demo only runs with DJANGO_DEBUG=1.")
        missing = {spec["shard"] for spec in DEMO} - set(settings.SHARDS)
        if missing:
            raise CommandError(f"These shards aren't configured: {', '.join(sorted(missing))}")
        todo = [spec for spec in DEMO if not DirectoryClinic.objects.filter(name=spec["name"]).exists()]
        if not todo:
            self.stdout.write("Demo data already exists.")
            return
        password = "Demo-" + secrets.token_urlsafe(6)
        users, created = {}, []

        def account(username, name):
            if username not in users:
                users[username] = User.objects.filter(username=username).first()
                if users[username] is None:
                    users[username] = User.objects.create_user(username, password=password, full_name=name)
                    created.append(username)
            return users[username]

        for spec in todo:
            owner = account(*spec["owner"])
            entry = create_clinic(name=spec["name"], owner=owner, specialty_code=spec.get("specialty"),
                                  kind=spec.get("kind", "clinic"), sections=spec.get("sections"), city=spec["city"],
                                  shard=spec["shard"])
            for username, name, role in spec["members"]:
                add_member(entry, account(username, name), role, name)
            with clinic_transaction(entry.id, entry.shard):
                from clinics.models import Membership

                by_user = {m.user_id: m for m in Membership.objects.filter(clinic_id=entry.id)}
                sections = list(Section.objects.filter(clinic_id=entry.id).order_by("sort", "id"))
                for username, section, weekdays, starts, ends, mode, minutes in spec["sessions"]:
                    doctor = by_user[users[username].id]
                    for weekday in weekdays:
                        Schedule.objects.create(clinic_id=entry.id, doctor=doctor, section=sections[section],
                                                weekday=weekday, starts=dt.time.fromisoformat(starts),
                                                ends=dt.time.fromisoformat(ends), mode=mode, slot_minutes=minutes)
            self.stdout.write(f"{spec['name']} on {entry.shard} (clinic {entry.id})")

        self.stdout.write(
            "Demo accounts: hamada, mona and karim (owners: they set up two-step sign-in on first sign-in), "
            "ahmed, nour and omar (doctors), sara (assistant in both clinics), laila (the center's assistant).")
        if created:
            self.stdout.write(self.style.SUCCESS(f"New accounts: {', '.join(created)}. Their password: {password}"))
        self.stdout.write("Accounts that already existed keep their passwords.")
