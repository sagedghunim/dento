"""Demo clinics for local use (DEBUG only). Never run on production data."""

import datetime as dt
import secrets

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from accounts.models import User
from clinics.models import Schedule
from core.routing import clinic_transaction
from directory.flows import add_member, create_clinic
from directory.models import DirectoryClinic

DEMO = [
    {
        "name": "مركز سمايل لطب الأسنان", "specialty": "dental", "city": "القاهرة", "shard": "shard_1",
        "owner": ("hamada", "د. حمادة عزيز"),
        "members": [("ahmed", "د. أحمد سامي", "doctor"), ("sara", "سارة محمود", "assistant")],
        # (username, weekdays, starts, ends, mode, slot minutes)
        "sessions": [("hamada", (5, 6, 0, 1, 2, 3), "17:00", "23:00", "queue", None),
                     ("ahmed", (5, 6, 0, 1, 2, 3), "12:00", "16:00", "slots", 20)],
    },
    {
        "name": "عيادة د. منى للأطفال", "specialty": "pediatrics", "city": "الجيزة", "shard": "shard_2",
        "owner": ("mona", "د. منى فتحي"),
        "members": [("sara", "سارة محمود", "assistant")],
        "sessions": [("mona", (5, 6, 0, 1, 2, 3), "18:00", "00:30", "queue", None)],
    },
]


class Command(BaseCommand):
    help = "Create two demo clinics on two shards, with an assistant who works in both."

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("seed_demo only runs with DJANGO_DEBUG=1.")
        if DirectoryClinic.objects.exists():
            self.stdout.write("Demo data already exists.")
            return
        missing = {spec["shard"] for spec in DEMO} - set(settings.SHARDS)
        if missing:
            raise CommandError(f"These shards aren't configured: {', '.join(sorted(missing))}")
        password = "Demo-" + secrets.token_urlsafe(6)
        users = {}

        def account(username, name):
            if username not in users:
                users[username] = User.objects.create_user(username, password=password, full_name=name)
            return users[username]

        for spec in DEMO:
            owner = account(*spec["owner"])
            entry = create_clinic(name=spec["name"], specialty_code=spec["specialty"], owner=owner, city=spec["city"],
                                  shard=spec["shard"])
            memberships = {spec["owner"][0]: None}
            for username, name, role in spec["members"]:
                memberships[username] = add_member(entry, account(username, name), role, name)
            with clinic_transaction(entry.id, entry.shard):
                from clinics.models import Membership

                by_user = {m.user_id: m for m in Membership.objects.filter(clinic_id=entry.id)}
                for username, weekdays, starts, ends, mode, minutes in spec["sessions"]:
                    doctor = by_user[users[username].id]
                    for weekday in weekdays:
                        Schedule.objects.create(clinic_id=entry.id, doctor=doctor, weekday=weekday,
                                                starts=dt.time.fromisoformat(starts), ends=dt.time.fromisoformat(ends),
                                                mode=mode, slot_minutes=minutes)
            self.stdout.write(f"{spec['name']} on {entry.shard} (clinic {entry.id})")

        self.stdout.write(self.style.SUCCESS(
            "Demo accounts: hamada and mona (owners: they set up two-step sign-in on first sign-in), "
            "ahmed (doctor), sara (assistant in both clinics).\n"
            f"Password for all of them: {password}"))
